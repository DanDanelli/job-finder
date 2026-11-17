"""What the site reads and writes, with no HTTP dependency (testable directly).

Every function takes the signed-in profile's id and touches only that profile's data.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Engine, exists, func, or_, select, true
from sqlalchemy.orm import selectinload

from job_finder.db import repo
from job_finder.db.models import FeedbackDB, JobDB, MatchDB, ProfileDB
from job_finder.db.session import session
from job_finder.domain import Filters, MatchStatus, ProfileData, Query
from job_finder.pipeline.filters import discard_reason
from job_finder.telegram.digest import DigestItem, digest_item

TABS: dict[str, MatchStatus] = {
    "new": MatchStatus.NEW,
    "saved": MatchStatus.SAVED,
    "applied": MatchStatus.APPLIED,
    "dismissed": MatchStatus.DISMISSED,
}
# Site action -> (new status, feedback action recorded)
ACTIONS: dict[str, tuple[MatchStatus, str]] = {
    "save": (MatchStatus.SAVED, "save"),
    "apply": (MatchStatus.APPLIED, "apply"),
    "dismiss": (MatchStatus.DISMISSED, "dismiss"),
    "restore": (MatchStatus.NEW, "restore"),
}
# Sort orders of the jobs list: by fit or by posting date (collection date when the
# source gave none), each both ways. Ties go to the newest match.
SORTS = ("score_desc", "score_asc", "date_desc", "date_asc")
# Matches the site may drop when a filter is tightened: never shown to the person.
_UNSEEN = (MatchStatus.PENDING, MatchStatus.NEW, MatchStatus.SKIPPED)


@dataclass(frozen=True)
class JobCard:
    item: DigestItem
    status: str
    posted_at: datetime | None
    note: str | None = None


def _shown(dismissed_since: datetime | None) -> ColumnElement[bool]:
    """Matches the site lists: every status, except dismissals older than
    `dismissed_since` (None: all of them)."""
    if dismissed_since is None:
        return true()
    return or_(MatchDB.status != MatchStatus.DISMISSED, MatchDB.updated_at >= dismissed_since)


def counts(
    engine: Engine, profile_id: int, *, dismissed_since: datetime | None = None
) -> dict[str, int]:
    """Matches per tab, plus "pending" (waiting for scoring)."""
    with session(engine, commit=False) as s:
        rows = s.execute(
            select(MatchDB.status, func.count())
            .where(MatchDB.profile_id == profile_id, _shown(dismissed_since))
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
    sort: str = "score_desc",
    limit: int = 100,
    dismissed_since: datetime | None = None,
) -> list[JobCard]:
    status = TABS.get(tab, MatchStatus.NEW)
    posted = func.coalesce(JobDB.posted_at, JobDB.created_at)
    orders: dict[str, tuple[ColumnElement[Any], ...]] = {
        "score_desc": (MatchDB.score.desc(), posted.desc()),
        "score_asc": (MatchDB.score.asc(), posted.desc()),
        "date_desc": (posted.desc(), MatchDB.score.desc()),
        "date_asc": (posted.asc(), MatchDB.score.desc()),
    }
    order = orders.get(sort, orders["score_desc"])
    q = (
        select(MatchDB)
        .join(MatchDB.job)
        .where(
            MatchDB.profile_id == profile_id,
            MatchDB.status == status,
            MatchDB.score.is_not(None),
            MatchDB.score >= min_score,
            _shown(dismissed_since),
        )
        .options(
            selectinload(MatchDB.job).selectinload(JobDB.sources),
            selectinload(MatchDB.feedback),
        )
        .order_by(*order, MatchDB.id.desc())
        .limit(limit)
    )
    if work_mode:
        q = q.where(JobDB.work_mode == work_mode)
    with session(engine, commit=False) as s:
        return [
            JobCard(digest_item(m), m.status, m.job.posted_at, repo.latest_note(m.feedback))
            for m in s.scalars(q)
        ]


def set_status(
    engine: Engine, profile_id: int, match_id: int, action: str, note: str | None = None
) -> bool:
    """Applies save / apply / dismiss / restore, or "note" (only the note). `note` is the note
    field as sent (None: the form had none); it is recorded when it changed. False if
    the match is not this profile's."""
    if action not in ACTIONS and action != "note":
        return False
    with session(engine) as s:
        match = s.get(MatchDB, match_id)
        if match is None or match.profile_id != profile_id:
            return False
        if match.status not in (*TABS.values(),):
            return False  # pending or skipped: not on the site yet
        note = repo.clean_note(note)
        changed = note is not None and note != (repo.latest_note(match.feedback) or "")
        if action == "note":
            if changed:
                repo.add_note(s, match, note or "", "web")
            return True
        status, feedback = ACTIONS[action]
        match.status = status
        s.add(
            FeedbackDB(
                match_id=match.id, action=feedback, origin="web", comment=note if changed else None
            )
        )
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


def profile_data(engine: Engine, profile_id: int) -> tuple[ProfileData, list[Query]]:
    with session(engine, commit=False) as s:
        p = repo.profile_from(s.get_one(ProfileDB, profile_id))
        return p.data, p.queries


def new_terms(terms: list[str], queries: list[Query]) -> list[str]:
    """Suggested search terms the profile does not have yet (case-insensitive)."""
    known = {q.term.casefold() for q in queries}
    result: dict[str, str] = {}
    for term in terms:
        term = term.strip()
        if term and term.casefold() not in known:
            result.setdefault(term.casefold(), term)
    return list(result.values())


def save_profile_data(
    engine: Engine,
    profile_id: int,
    data: ProfileData,
    *,
    add_terms: Sequence[str] = (),
    resume_path: str | None = None,
) -> int:
    """Saves the reviewed professional data; adds the chosen search terms as active
    searches and, after an upload, records the new CV. Returns how many terms were added.
    """
    with session(engine) as s:
        db = s.get_one(ProfileDB, profile_id)
        profile = repo.profile_from(db)
        db.data = data.model_dump(mode="json")
        terms = new_terms(list(add_terms), profile.queries)
        if terms:
            db.queries = [
                q.model_dump(mode="json")
                for q in [*profile.queries, *(Query(term=t) for t in terms)]
            ]
        if resume_path is not None:
            db.resume_path = resume_path
        return len(terms)
