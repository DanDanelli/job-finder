"""One pipeline run: collect -> normalize -> deduplicate -> store -> filter per profile.

Collection happens once for all profiles (saves quota); the filters and the scoring are
applied per profile.
"""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from job_finder.connectors.base import Connector
from job_finder.db import repo
from job_finder.db.models import MatchDB, ProfileDB, RunDB
from job_finder.domain import Job, MatchStatus, Query, RawJob
from job_finder.pipeline.dedup import dedup_key
from job_finder.pipeline.filters import discard_reason
from job_finder.pipeline.normalize import normalize
from job_finder.quota import Quota, QuotaExhausted

log = logging.getLogger(__name__)

# Called with an i18n key ("progress.*") and its parameters as a run moves along.
Progress = Callable[[str, dict[str, Any]], None]


def no_progress(key: str, params: dict[str, Any]) -> None:
    pass


@dataclass
class ProfileSummary:
    name: str
    evaluated: int = 0
    approved: list[Job] = field(default_factory=list)
    discards: Counter[str] = field(default_factory=Counter)


@dataclass
class RunSummary:
    run_id: int
    collected: int = 0
    new: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    profiles: list[ProfileSummary] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _queries(profiles: Sequence[ProfileDB]) -> list[Query]:
    """Union of the active queries of every profile, without repeats."""
    seen: dict[tuple[str, tuple[str, ...]], Query] = {}
    for p in profiles:
        for q in repo.profile_from(p).queries:
            if q.active:
                seen.setdefault((q.term.casefold(), tuple(sorted(q.countries))), q)
    return list(seen.values())


def collect(
    engine: Engine,
    connectors: Sequence[Connector],
    queries: list[Query],
    now: datetime,
    progress: Progress = no_progress,
) -> tuple[list[RawJob], list[str]]:
    raws: list[RawJob] = []
    errors: list[str] = []
    for i, connector in enumerate(connectors, 1):
        progress("progress.collect", {"source": connector.name, "i": i, "n": len(connectors)})
        quota = Quota(
            engine,
            connector.name,
            connector.daily_limit,
            now.date(),
            monthly_limit=getattr(connector, "monthly_limit", None),
        )
        try:
            result = connector.search(queries, quota)
        except QuotaExhausted as e:
            errors.append(str(e))
            continue
        except Exception as e:  # one broken source does not take down the others
            log.exception("connector %s failed", connector.name)
            errors.append(f"{connector.name}: {type(e).__name__}: {e}")
            continue
        log.info("%s: %d jobs", connector.name, len(result))
        raws.extend(result)
    return raws, errors


def execute_run(
    s: Session,
    engine: Engine,
    connectors: Sequence[Connector],
    profiles: Sequence[ProfileDB],
    now: datetime,
    kind: str = "on_demand",
    progress: Progress = no_progress,
) -> RunSummary:
    # Collection (network + quota, which writes in its own transaction) comes before any
    # write in this session: SQLite takes one writer at a time, and slow HTTP calls must
    # not hold the database lock.
    raws, errors = collect(engine, connectors, _queries(profiles), now, progress)

    run = RunDB(kind=kind, status="running", started_at=now)
    s.add(run)
    s.flush()
    summary = RunSummary(run_id=run.id, errors=list(errors))

    progress("progress.store", {"n": len(raws)})
    for raw in raws:
        try:
            job = normalize(raw)
            _, is_new = repo.record_job(s, raw, job, dedup_key(job))
        except Exception as e:
            log.exception("failed to normalize %s/%s", raw.source, raw.source_id)
            summary.errors.append(f"{raw.source}/{raw.source_id}: {type(e).__name__}: {e}")
            continue
        summary.collected += 1
        summary.by_source[raw.source] += 1
        summary.new += int(is_new)

    for profile_db in profiles:
        progress("progress.filter", {"profile": profile_db.name})
        summary.profiles.append(_filter_for_profile(s, profile_db, now))

    run.finished_at = datetime.now(now.tzinfo)
    run.jobs_collected = summary.collected
    run.jobs_new = summary.new
    run.matches_new = sum(len(p.approved) for p in summary.profiles)
    run.errors = summary.errors
    if not summary.errors:
        run.status = "ok"
    elif summary.collected:
        run.status = "partial"
    else:
        run.status = "error"
    return summary


def _filter_for_profile(s: Session, profile_db: ProfileDB, now: datetime) -> ProfileSummary:
    profile = repo.profile_from(profile_db)
    summary = ProfileSummary(name=profile.name)
    since = now - timedelta(days=profile.filters.max_age_days)
    for job_db in repo.candidates(s, profile_db.id, since):
        job = repo.job_from(job_db)
        summary.evaluated += 1
        reason = discard_reason(job, profile.filters, now)
        if reason:
            summary.discards[reason] += 1
            continue
        # Passed the filters: stays pending until scoring.
        s.add(MatchDB(job_id=job_db.id, profile_id=profile_db.id, status=MatchStatus.PENDING))
        summary.approved.append(job)
    s.flush()
    return summary
