"""One-time codes that link a Telegram chat to a profile from the site.

The person asks for a code on the site (Account) and sends it to the bot as
`/start <code>`, usually through a t.me deep link. The site and the bot run in the same
process, so the codes live in memory: after a restart, the person makes a new one."""

import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

CODE_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class LinkCode:
    code: str
    expires: datetime


class TelegramLinks:
    """At most one live code per profile; a new one replaces the old. Thread-safe: the
    site's handlers run in a thread pool, the bot's in the event loop."""

    def __init__(self, ttl: timedelta = CODE_TTL) -> None:
        self._ttl = ttl
        self._codes: dict[int, LinkCode] = {}
        self._lock = threading.Lock()
        # Set once the bot has started (getMe); None means there is no bot to link to.
        self.bot_username: str | None = None

    def new(self, profile_id: int, now: datetime) -> LinkCode:
        # 72 random bits, in the characters a t.me start parameter accepts ([A-Za-z0-9_-]).
        code = LinkCode(secrets.token_urlsafe(9), now + self._ttl)
        with self._lock:
            self._codes[profile_id] = code
        return code

    def pending(self, profile_id: int, now: datetime) -> LinkCode | None:
        with self._lock:
            code = self._codes.get(profile_id)
            if code is not None and code.expires <= now:
                del self._codes[profile_id]
                return None
            return code

    def discard(self, profile_id: int) -> None:
        with self._lock:
            self._codes.pop(profile_id, None)

    def redeem(self, code: str, now: datetime) -> int | None:
        """The profile the code belongs to, if it is live; it works once."""
        given = code.strip().encode()
        with self._lock:
            for profile_id, live in list(self._codes.items()):
                if live.expires <= now:
                    del self._codes[profile_id]
                elif hmac.compare_digest(live.code.encode(), given):
                    del self._codes[profile_id]
                    return profile_id
        return None

    def deep_link(self, code: LinkCode) -> str | None:
        if self.bot_username is None:
            return None
        return f"https://t.me/{self.bot_username}?start={code.code}"
