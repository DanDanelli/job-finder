"""What the Status page shows: sources and their quota, recent runs and Claude's cost.

Runs, quotas and the month's total are shared by everyone (one collection serves every
profile); the cost per operation is shown for the whole house and for the signed-in
profile.
"""

from dataclasses import dataclass
from datetime import date, datetime

import httpx
from sqlalchemy import Engine, func, select

from job_finder.config import Settings
from job_finder.connectors import base
from job_finder.db.models import LLMCostDB, QuotaUsageDB, RunDB
from job_finder.db.session import session
from job_finder.llm.client import start_of_month
from job_finder.telegram.digest import SOURCE_NAMES


@dataclass(frozen=True)
class SourceUsage:
    name: str
    label: str
    today: int
    daily_limit: int | None
    month: int
    monthly_limit: int | None


@dataclass(frozen=True)
class RunRow:
    started_at: datetime
    kind: str
    status: str
    seconds: int | None
    collected: int
    new: int
    matches: int
    errors: list[str]


@dataclass(frozen=True)
class OperationCost:
    operation: str
    calls: int
    cost: float
    mine: float


@dataclass(frozen=True)
class Spend:
    total: float
    cap: float
    mine: float
    operations: list[OperationCost]

    @property
    def percent(self) -> int:
        return min(100, round(100 * self.total / self.cap)) if self.cap else 0


def _limits(name: str) -> tuple[int | None, int | None]:
    """The connector's own limits (from its settings). Nothing is fetched."""
    try:
        with httpx.Client() as http:
            connector = base.create(name, http)
    except Exception:
        return None, None
    return connector.daily_limit, getattr(connector, "monthly_limit", None)


def sources(engine: Engine, settings: Settings, today: date) -> list[SourceUsage]:
    month_start = today.replace(day=1)
    with session(engine, commit=False) as s:
        rows = s.execute(
            select(
                QuotaUsageDB.connector,
                func.sum(QuotaUsageDB.calls).filter(QuotaUsageDB.day == today),
                func.sum(QuotaUsageDB.calls),
            )
            .where(QuotaUsageDB.day >= month_start, QuotaUsageDB.day <= today)
            .group_by(QuotaUsageDB.connector)
        ).all()
    used = {name: (int(day or 0), int(month or 0)) for name, day, month in rows}
    result = []
    for name in settings.connectors:
        daily, monthly = _limits(name)
        day_calls, month_calls = used.get(name, (0, 0))
        result.append(
            SourceUsage(name, SOURCE_NAMES.get(name, name), day_calls, daily, month_calls, monthly)
        )
    return result


def recent_runs(engine: Engine, limit: int = 10) -> list[RunRow]:
    with session(engine, commit=False) as s:
        runs = s.scalars(
            select(RunDB).where(RunDB.kind != "dry_run").order_by(RunDB.id.desc()).limit(limit)
        ).all()
        return [
            RunRow(
                r.started_at,
                r.kind,
                r.status,
                int((r.finished_at - r.started_at).total_seconds()) if r.finished_at else None,
                r.jobs_collected,
                r.jobs_new,
                r.matches_new,
                [str(e) for e in r.errors or []],
            )
            for r in runs
        ]


def spend(engine: Engine, settings: Settings, profile_id: int, now: datetime) -> Spend:
    with session(engine, commit=False) as s:
        rows = s.execute(
            select(
                LLMCostDB.operation,
                func.count(),
                func.coalesce(func.sum(LLMCostDB.cost_usd), 0.0),
                func.coalesce(
                    func.sum(LLMCostDB.cost_usd).filter(LLMCostDB.profile_id == profile_id), 0.0
                ),
            )
            .where(LLMCostDB.created_at >= start_of_month(now))
            .group_by(LLMCostDB.operation)
            .order_by(func.sum(LLMCostDB.cost_usd).desc())
        ).all()
    operations = [OperationCost(op, n, float(cost), float(mine)) for op, n, cost, mine in rows]
    return Spend(
        total=sum(o.cost for o in operations),
        cap=settings.monthly_cost_cap_usd,
        mine=sum(o.mine for o in operations),
        operations=operations,
    )
