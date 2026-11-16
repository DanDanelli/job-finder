"""Dynamic few-shot: how the person reacted to jobs like the one being scored.

The jobs the person saved or dismissed (Telegram 👍/👎, the site's buttons) that are most
similar to the current job go into the user message, after the cache breakpoint: the
cached system prompt stays the same for every job.

Similarity is BM25 over titles only, divided by the job's score against itself (0-1).
Descriptions share so much generic vocabulary that, on real data, a "Salesforce Pardot
Consultant" came out close to a "Cloud Architect"; titles did not have that problem.
Measured on 86 real jobs and 7 decisions: related titles scored 0.34-0.87 ("Azure
DevOps Engineer" ~ "DevOps Engineer": 0.60), unrelated ones at most ~0.31.
"""

from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from job_finder.db import repo
from job_finder.db.models import FeedbackDB, MatchDB
from job_finder.domain import Job, MatchStatus
from job_finder.pipeline.prerank import bm25, tokens

MAX_EXAMPLES = 4
POOL = 300  # most recent decisions considered
MIN_SIMILARITY = 0.35  # normalized; below it the titles share only generic words


def title_terms(title: str) -> tuple[str, ...]:
    """Title tokens without numbers (job codes such as "[Job - 32643]")."""
    return tuple(t for t in tokens(title) if not t.isdigit())


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
    document: tuple[str, ...]  # title terms


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
                document=title_terms(job.title),
            )
        )
    return examples


def similar(
    examples: list[Example], job: Job, k: int = MAX_EXAMPLES, exclude: int | None = None
) -> list[Example]:
    """The `k` examples most similar to `job`, most similar first. `exclude` leaves one
    match out (the eval scores jobs that are examples themselves)."""
    pool = [e for e in examples if e.match_id != exclude]
    query = title_terms(job.title)
    if not pool or not query:
        return []
    *scores, itself = bm25(query, [e.document for e in pool] + [query])
    if itself <= 0:
        return []
    ranked = sorted(zip(scores, pool, strict=True), key=lambda pair: pair[0], reverse=True)
    return [e for score, e in ranked[:k] if score / itself >= MIN_SIMILARITY]


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
