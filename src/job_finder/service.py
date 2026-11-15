"""Full run: collection, filters and scoring. Used by the CLI, the scheduler and the bot."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base
from job_finder.connectors.base import Connector
from job_finder.db import repo
from job_finder.db.session import session
from job_finder.i18n import t
from job_finder.llm.client import LLMClient, is_fatal, make_client
from job_finder.llm.scorer import ScoringSummary, score_pending
from job_finder.pipeline.run import ProfileSummary, RunSummary, execute_run

log = logging.getLogger(__name__)


class NoProfiles(Exception):
    pass


@dataclass
class ProfileReport:
    profile_id: int
    name: str
    filters: ProfileSummary
    scoring: ScoringSummary | None = None


@dataclass
class Report:
    run: RunSummary
    language: str
    profiles: list[ProfileReport] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # apply to every profile

    def warnings_for(self, profile_id: int) -> list[str]:
        warnings = list(self.warnings)
        for p in self.profiles:
            if p.profile_id == profile_id and p.scoring:
                if p.scoring.failures:
                    warnings.append(t(self.language, "warn.unscored", n=len(p.scoring.failures)))
                if p.scoring.interrupted:
                    warnings.append(p.scoring.interrupted)
        return warnings


def full_run(
    engine: Engine,
    settings: Settings,
    *,
    names: Sequence[str] | None = None,
    kind: str = "on_demand",
    dry_run: bool = False,
    score: bool = True,
    connectors: Sequence[Connector] | None = None,
    client: LLMClient | None = None,
    now: datetime | None = None,
) -> Report:
    """Runs the pipeline. Scheduled runs skip paused profiles.

    Source and Claude errors become warnings in the report; only a missing profile raises.
    """
    now = now or datetime.now(UTC)
    lang = settings.language
    with base.http_client() as http, session(engine, commit=not dry_run) as s:
        profiles = repo.list_profiles(
            s, list(names) if names else None, include_paused=kind != "scheduled"
        )
        if not profiles:
            raise NoProfiles("no profile found; use `job-finder profile load`")
        ids = {p.name: p.id for p in profiles}
        if connectors is None:
            connectors = [base.create(name, http) for name in settings.connectors]
        run = execute_run(
            s, engine, connectors, profiles, now=now, kind="dry_run" if dry_run else kind
        )

    report = Report(
        run=run,
        language=lang,
        profiles=[ProfileReport(ids[ps.name], ps.name, ps) for ps in run.profiles],
        warnings=list(run.errors),
    )
    if dry_run or not score:
        return report
    if client is None:
        if settings.anthropic_api_key is None:
            report.warnings.append(t(lang, "warn.no_key"))
            return report
        client = make_client(engine)

    for pr in report.profiles:
        try:
            pr.scoring = score_pending(
                engine,
                client,
                pr.profile_id,
                model=settings.scoring_model,
                language=lang,
                top_n=settings.prerank_top_n,
                cutoff=settings.relevance_cutoff,
                now=now,
                run_id=run.run_id,
            )
        except Exception as e:
            if not is_fatal(e):
                raise
            log.error("scoring stopped: %s", e)
            report.warnings.append(t(lang, "warn.claude_down", error=f"{type(e).__name__}: {e}"))
            break
        if pr.scoring.interrupted:
            break
    return report
