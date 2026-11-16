"""Model eval with real labels: how well each model separates saved from dismissed jobs.

Every job a person saved or dismissed is scored again by each model, with the same
prompt as production. The job's own decision is left out of its few-shot examples
(`similar(..., exclude=match_id)`), so the model never sees the answer. Nothing goes to
`matches`: the new scores live only in the report, and the spend is recorded in
`llm_costs` under the `eval` operation (so it counts against the monthly cap and shows
on the Status page).

Known bias: the other examples come from every decision, including ones made after the
job was first scored, and carry the score production gave them then (Haiku's).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

import anthropic
from sqlalchemy import Engine, exists, func, select

from job_finder.db import repo
from job_finder.db.models import FeedbackDB, LLMCostDB, MatchDB, ProfileDB
from job_finder.db.session import session
from job_finder.domain import Job, MatchStatus
from job_finder.llm.client import (
    PRICES,
    CostCapReached,
    InvalidResponse,
    LLMClient,
    is_fatal,
)
from job_finder.llm.fewshot import load_examples, similar
from job_finder.llm.scorer import OPERATION as SCORING
from job_finder.llm.scorer import evaluate

log = logging.getLogger(__name__)

OPERATION = "eval"
STORED = "stored"  # the score production gave when the job was first scored


@dataclass(frozen=True)
class Labeled:
    match_id: int
    saved: bool
    job: Job
    stored_score: int | None


def load_labeled(engine: Engine, profile_id: int) -> list[Labeled]:
    """The profile's saved and dismissed jobs that have feedback, oldest first."""
    with session(engine, commit=False) as s:
        matches = s.scalars(
            select(MatchDB)
            .where(
                MatchDB.profile_id == profile_id,
                MatchDB.status.in_((MatchStatus.SAVED, MatchStatus.DISMISSED)),
                exists().where(FeedbackDB.match_id == MatchDB.id),
            )
            .order_by(MatchDB.id)
        ).all()
        return [
            Labeled(m.id, m.status == MatchStatus.SAVED, repo.job_from(m.job), m.score)
            for m in matches
        ]


# --- cost estimate ----------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    calls: int
    cost_usd: float
    basis: int  # past scoring calls the token counts came from (0: a fixed guess)


# Used when there is no scoring history yet: a typical call in production.
DEFAULT_TOKENS = {"input": 2_000, "output": 400, "cache_read": 1_500, "cache_write": 0}


def estimate(engine: Engine, models: Sequence[str], jobs: int) -> Estimate:
    """Cost of scoring `jobs` with each model, from the average tokens of past scoring
    calls. The prompt is the same for every model; the output is an approximation."""
    with session(engine, commit=False) as s:
        n, *averages = s.execute(
            select(
                func.count(),
                func.avg(LLMCostDB.input_tokens),
                func.avg(LLMCostDB.output_tokens),
                func.avg(LLMCostDB.cache_read_tokens),
                func.avg(LLMCostDB.cache_write_tokens),
            ).where(LLMCostDB.operation == SCORING)
        ).one()
    tokens = (
        dict(zip(DEFAULT_TOKENS, (float(a or 0) for a in averages), strict=True))
        if n
        else {k: float(v) for k, v in DEFAULT_TOKENS.items()}
    )
    total = 0.0
    for model in models:
        price = PRICES.get(model)
        if price is None:
            continue
        per_call = (
            tokens["input"] * price.input
            + tokens["output"] * price.output
            + tokens["cache_read"] * price.cache_read
            + tokens["cache_write"] * price.cache_write
        ) / 1_000_000
        total += per_call * jobs
    return Estimate(calls=jobs * len(models), cost_usd=total, basis=int(n or 0))


# --- metrics ----------------------------------------------------------------------


def auc(saved: Sequence[float], dismissed: Sequence[float]) -> float | None:
    """Chance that a random saved job scores above a random dismissed one (ties: half).
    0.5 is a coin toss; 1.0 separates them perfectly."""
    if not saved or not dismissed:
        return None
    wins = sum(1.0 if s > d else 0.5 if s == d else 0.0 for s in saved for d in dismissed)
    return wins / (len(saved) * len(dismissed))


def precision_at(pairs: Sequence[tuple[float, bool]], k: int) -> float | None:
    """Share of saved jobs among the `k` best scored. Ties at the cut count by their
    share of saved jobs, so the result does not depend on how ties are ordered."""
    if k <= 0 or len(pairs) < k:
        return None
    ordered = sorted(pairs, key=lambda p: p[0], reverse=True)
    cut = ordered[k - 1][0]
    above = [saved for score, saved in ordered if score > cut]
    tied = [saved for score, saved in ordered if score == cut]
    slots = k - len(above)
    return (sum(above) + slots * sum(tied) / len(tied)) / k


@dataclass(frozen=True)
class Metrics:
    n: int
    saved: int
    mean_saved: float | None
    mean_dismissed: float | None
    auc: float | None
    precision_at_saved: float | None  # k = number of saved jobs
    saved_in_digest: int  # saved jobs at or above the digest minimum
    dismissed_in_digest: int

    @property
    def mean_gap(self) -> float | None:
        if self.mean_saved is None or self.mean_dismissed is None:
            return None
        return self.mean_saved - self.mean_dismissed


def metrics(pairs: Sequence[tuple[float, bool]], digest_min_score: int) -> Metrics:
    saved = [score for score, s in pairs if s]
    dismissed = [score for score, s in pairs if not s]
    return Metrics(
        n=len(pairs),
        saved=len(saved),
        mean_saved=sum(saved) / len(saved) if saved else None,
        mean_dismissed=sum(dismissed) / len(dismissed) if dismissed else None,
        auc=auc(saved, dismissed),
        precision_at_saved=precision_at(pairs, len(saved)),
        saved_in_digest=sum(score >= digest_min_score for score in saved),
        dismissed_in_digest=sum(score >= digest_min_score for score in dismissed),
    )


# --- run ----------------------------------------------------------------------------


@dataclass
class Row:
    labeled: Labeled
    scores: dict[str, int] = field(default_factory=dict)  # model -> new score


@dataclass
class EvalReport:
    profile: str
    models: list[str]
    rows: list[Row]
    cost_by_model: dict[str, float] = field(default_factory=dict)
    calls_by_model: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    interrupted: str | None = None

    def pairs(self, column: str) -> list[tuple[float, bool]]:
        """(score, saved) for one model, or STORED for production's original score."""
        out = []
        for r in self.rows:
            score = r.labeled.stored_score if column == STORED else r.scores.get(column)
            if score is not None:
                out.append((float(score), r.labeled.saved))
        return out

    def metrics(self, column: str, digest_min_score: int) -> Metrics:
        return metrics(self.pairs(column), digest_min_score)


Progress = Callable[[str, int, int], None]  # model, i, n


def run_eval(
    engine: Engine,
    client: LLMClient,
    profile_id: int,
    *,
    models: Sequence[str],
    language: str,
    limit: int | None = None,
    progress: Progress | None = None,
) -> EvalReport:
    """Scores every labeled job of the profile with each model, one model at a time (so
    the cached prompt prefix stays warm). Writes only to `llm_costs`.

    A failure on one job is reported and the job is left out of that model's metrics.
    Account errors are raised; the cost cap stops the eval with what it has so far."""
    started = client.clock()
    with session(engine, commit=False) as s:
        profile = repo.profile_from(s.get_one(ProfileDB, profile_id))
        decisions = load_examples(s, profile_id)
    labeled = load_labeled(engine, profile_id)
    if limit is not None:
        labeled = labeled[:limit]
    report = EvalReport(profile.name, list(models), [Row(item) for item in labeled])

    for model in models:
        for i, row in enumerate(report.rows, 1):
            if progress is not None:
                progress(model, i, len(report.rows))
            job = row.labeled.job
            try:
                fit = evaluate(
                    client,
                    profile,
                    job,
                    model=model,
                    language=language,
                    profile_id=profile_id,
                    examples=similar(decisions, job, exclude=row.labeled.match_id),
                    operation=OPERATION,
                )
            except CostCapReached as e:
                report.interrupted = str(e)
                break
            except (InvalidResponse, anthropic.APIError, ValueError) as e:
                if is_fatal(e):
                    raise
                log.warning("eval failed for %s with %s: %s", job.title, model, e)
                report.failures.append(f"{model} · {job.title}: {type(e).__name__}: {e}")
                continue
            row.scores[model] = fit.score
        if report.interrupted:
            break

    report.cost_by_model, report.calls_by_model = _spend(engine, profile_id, started)
    return report


def _spend(
    engine: Engine, profile_id: int, since: datetime
) -> tuple[dict[str, float], dict[str, int]]:
    with session(engine, commit=False) as s:
        rows = s.execute(
            select(LLMCostDB.model, func.sum(LLMCostDB.cost_usd), func.count())
            .where(
                LLMCostDB.operation == OPERATION,
                LLMCostDB.profile_id == profile_id,
                LLMCostDB.created_at >= since,
            )
            .group_by(LLMCostDB.model)
        ).all()
    return (
        {model: float(cost) for model, cost, _ in rows},
        {model: int(n) for model, _, n in rows},
    )
