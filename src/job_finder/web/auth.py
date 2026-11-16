"""Site login: password hashing, sessions and a throttle on failed attempts.

- Passwords: scrypt (stdlib) with a random salt per password, stored as
  "scrypt$<n>$<r>$<p>$<salt>$<hash>" (base64). Set only from the CLI
  (`job-finder profile password <name>`); the site has no sign-up.
- Sessions: the cookie carries a random token; the database stores its SHA-256, so a
  copy of the database does not let anyone in. Logging out deletes the row.
- Throttle: after MAX_FAILURES failed logins for a username within WINDOW, that username
  is refused for WINDOW. Kept in memory; a restart clears it.
"""

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from job_finder.db.models import ProfileDB, SessionDB

MIN_PASSWORD_LENGTH = 8  # NIST SP 800-63B minimum for user-chosen passwords
_N, _R, _P = 2**14, 8, 1
_MAX_MEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAX_MEM)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Same work as a real check, so timing does not reveal which usernames exist.
        hashlib.scrypt(password.encode(), salt=b"0" * 16, n=_N, r=_R, p=_P, maxmem=_MAX_MEM)
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAX_MEM,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(s: Session, profile_id: int, now: datetime, days: int) -> str:
    """Creates a session and returns the token for the cookie."""
    s.execute(delete(SessionDB).where(SessionDB.expires_at <= now))
    token = secrets.token_urlsafe(32)
    s.add(
        SessionDB(
            token_hash=_token_hash(token),
            profile_id=profile_id,
            created_at=now,
            expires_at=now + timedelta(days=days),
        )
    )
    s.flush()
    return token


def profile_for_token(s: Session, token: str | None, now: datetime) -> ProfileDB | None:
    if not token:
        return None
    row = s.scalar(select(SessionDB).where(SessionDB.token_hash == _token_hash(token)))
    if row is None or row.expires_at <= now:
        return None
    return s.get(ProfileDB, row.profile_id)


def end_session(s: Session, token: str | None) -> None:
    if token:
        s.execute(delete(SessionDB).where(SessionDB.token_hash == _token_hash(token)))


def end_all_sessions(s: Session, profile_id: int, keep: str | None = None) -> None:
    """Signs the profile out everywhere, except the session whose token is `keep`."""
    query = delete(SessionDB).where(SessionDB.profile_id == profile_id)
    if keep:
        query = query.where(SessionDB.token_hash != _token_hash(keep))
    s.execute(query)


class LoginThrottle:
    MAX_FAILURES = 5
    WINDOW = 15 * 60  # seconds

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and q[0] <= now - self.WINDOW:
            q.popleft()
        return q

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return len(self._recent(key, now)) >= self.MAX_FAILURES

    def failed(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._recent(key, now).append(now)

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)
