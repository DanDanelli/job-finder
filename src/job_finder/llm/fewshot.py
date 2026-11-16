"""Dynamic few-shot: how the person reacted to jobs like the one being scored.

The jobs the person saved or dismissed (Telegram 👍/👎, the site's buttons) that are most
similar to the current job (BM25 over title and description) go into the user message,
after the cache breakpoint: the cached system prompt stays the same for every job.
"""

from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from job_finder.db import repo
from job_finder.db.models import FeedbackDB, MatchDB
from job_finder.domain import Job, MatchStatus
from job_finder.pipeline.prerank import bm25, job_document

MAX_EXAMPLES = 4
POOL = 300  # most recent decisions considered
MIN_SIMILARITY = 1.0  # BM25 below this shares too little with the job to teach anything


@dataclass(frozen=True)
class Example:
    match_id: int
    saved: bool
    title: str
    company: str | None
    location: str | None
    work_mode: str
    score: int | None
    comment: str | None
    document: tuple[str, ...]


def load_examples(s: Session, profile_id: int, limit: int = POOL) -> list[Example]:
    """The profile's saved and dismissed jobs that have feedback, newest first."""
    matches = s.scalars(
        select(MatchDB)
        .where(
            MatchDB.profile_id == profile_id,
            MatchDB.status.in_((MatchStatus.SAVED, MatchStatus.DISMISSED)),
            exists().where(FeedbackDB.match_id == MatchDB.id),
        )
        .order_by(MatchDB.updated_at.desc())
        .limit(limit)
    ).all()
    examples = []
    for m in matches:
        job = repo.job_from(m.job)
        comments = [f.comment for f in m.feedback if f.comment]
        examples.append(
            Example(
                match_id=m.id,
                saved=m.status == MatchStatus.SAVED,
                title=job.title,
                company=job.company,
                location=job.location_text,
                work_mode=job.work_mode.value,
                score=m.score,
                comment=comments[-1] if comments else None,
                document=tuple(job_document(job)),
            )
        )
    return examples


def similar(
    examples: list[Example], job: Job, k: int = MAX_EXAMPLES, exclude: int | None = None
) -> list[Example]:
    """The `k` examples most similar to `job`, most similar first. `exclude` leaves one
    match out (the eval scores jobs that are examples themselves)."""
    pool = [e for e in examples if e.match_id != exclude]
    if not pool:
        return []
    scores = bm25(job_document(job), [e.document for e in pool])
    ranked = sorted(zip(scores, pool, strict=True), key=lambda pair: pair[0], reverse=True)
    return [e for score, e in ranked[:k] if score >= MIN_SIMILARITY]


def examples_block(examples: list[Example]) -> str:
    if not examples:
        return ""
    lines = ["<past_decisions>"]
    for e in examples:
        decision = "SAVED (interested)" if e.saved else "DISMISSED (not interested)"
        where = ", ".join(x for x in (e.company, e.location, e.work_mode) if x)
        line = f"- {decision}: {e.title} ({where})"
        if e.score is not None:
            line += f"; your score then: {e.score}"
        if e.comment:
            line += f"; their note: {e.comment}"
        lines.append(line)
    lines.append("</past_decisions>")
    return "\n".join(lines)
