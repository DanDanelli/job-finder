"""What the site reads and writes, with no HTTP dependency (testable directly).

Every function takes the signed-in profile's id and touches only that profile's data.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, exists, func, select
from sqlalchemy.orm import selectinload

from job_finder.db import repo
from job_finder.db.models import FeedbackDB, JobDB, MatchDB, ProfileDB
from job_finder.db.session import session
from job_finder.domain import Filters, MatchStatus, Query
from job_finder.pipeline.filters import discard_reason
from job_finder.telegram.digest import DigestItem, digest_item

TABS: dict[str, MatchStatus] = {
    "new": MatchStatus.NEW,
    "saved": MatchStatus.SAVED,
    "dismissed": MatchStatus.DISMISSED,
}
# Site action -> (new status, feedback action recorded)
ACTIONS: dict[str, tuple[MatchStatus, str]] = {
    "save": (MatchStatus.SAVED, "save"),
    "dismiss": (MatchStatus.DISMISSED, "dismiss"),
    "restore": (MatchStatus.NEW, "restore"),
}
# Matches the site may drop when a filter is tightened: never shown to the person.
_UNSEEN = (MatchStatus.PENDING, MatchStatus.NEW, MatchStatus.SKIPPED)


@dataclass(frozen=True)
class JobCard:
    item: DigestItem
    status: str
    posted_at: datetime | None


def counts(engine: Engine, profile_id: int) -> dict[str, int]:
    """Matches per tab, plus "pending" (waiting for scoring)."""
    with session(engine, commit=False) as s:
        rows = s.execute(
            select(MatchDB.status, func.count())
            .where(MatchDB.profile_id == profile_id)
            .group_by(MatchDB.status)
        ).all()
    by_status = {str(status): n for status, n in rows}
    result = {tab: by_status.get(status, 0) for tab, status in TABS.items()}
    result["pending"] = by_status.get(MatchStatus.PENDING, 0)
    return result


def job_cards(
    engine: Engine,
    profile_id: int,
    tab: str,
    *,
    min_score: int = 0,
    work_mode: str | None = None,
    limit: int = 100,
) -> list[JobCard]:
    status = TABS.get(tab, MatchStatus.NEW)
    q = (
        select(MatchDB)
        .join(MatchDB.job)
        .where(
            MatchDB.profile_id == profile_id,
            MatchDB.status == status,
            MatchDB.score.is_not(None),
            MatchDB.score >= min_score,
        )
        .options(selectinload(MatchDB.job).selectinload(JobDB.sources))
        .order_by(MatchDB.score.desc(), MatchDB.id.desc())
        .limit(limit)
    )
    if work_mode:
        q = q.where(JobDB.work_mode == work_mode)
    with session(engine, commit=False) as s:
        return [JobCard(digest_item(m), m.status, m.job.posted_at) for m in s.scalars(q)]


def set_status(engine: Engine, profile_id: int, match_id: int, action: str) -> bool:
    """Applies save / dismiss / restore. False if the match is not this profile's."""
    if action not in ACTIONS:
        return False
    status, feedback = ACTIONS[action]
    with session(engine) as s:
        match = s.get(MatchDB, match_id)
        if match is None or match.profile_id != profile_id:
            return False
        if match.status not in (*TABS.values(),):
            return False  # pending or skipped: not on the site yet
        match.status = status
        s.add(FeedbackDB(match_id=match.id, action=feedback, origin="web"))
        return True


@dataclass(frozen=True)
class ProfileForm:
    name: str
    queries: list[Query]
    filters: Filters
    preferences: str
    paused: bool


def load_profile(engine: Engine, profile_id: int) -> ProfileForm:
    with session(engine, commit=False) as s:
        db = s.get_one(ProfileDB, profile_id)
        p = repo.profile_from(db)
        return ProfileForm(p.name, p.queries, p.filters, p.preferences, db.paused)


def save_profile(
    engine: Engine,
    profile_id: int,
    *,
    queries: list[Query],
    filters: Filters,
    preferences: str,
    paused: bool,
    now: datetime,
) -> int:
    """Saves the profile and drops the unseen matches the new filters reject.

    "Unseen" means waiting for scoring, below the relevance cutoff, or scored but never
    sent in a digest and never saved or dismissed. A loosened filter needs nothing here:
    discarded jobs are not stored, so the next run evaluates them again. Age is left
    alone (jobs grow old on their own). Returns how many matches were dropped.
    """
    with session(engine) as s:
        db = s.get_one(ProfileDB, profile_id)
        db.queries = [q.model_dump(mode="json") for q in queries]
        db.filters = filters.model_dump(mode="json")
        db.preferences = preferences
        db.paused = paused
        unseen = s.scalars(
            select(MatchDB)
            .where(
                MatchDB.profile_id == profile_id,
                MatchDB.status.in_(_UNSEEN),
                MatchDB.notified_at.is_(None),
                ~exists().where(FeedbackDB.match_id == MatchDB.id),
            )
            .options(selectinload(MatchDB.job))
        ).all()
        dropped = 0
        for match in unseen:
            reason = discard_reason(repo.job_from(match.job), filters, now)
            if reason and reason != "too_old":
                s.delete(match)
                dropped += 1
        return dropped
