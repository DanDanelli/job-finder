"""Per-connector quota: daily and monthly (calendar month) limits.

Each use is recorded right away, in its own transaction: the HTTP call happened even if
the run fails later or is a dry run that rolls back.
"""

from datetime import date

from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.sqlite import insert

from job_finder.db.models import QuotaUsageDB
from job_finder.db.session import session


class QuotaExhausted(Exception):
    def __init__(
        self, connector: str, daily_limit: int | None, monthly_limit: int | None = None
    ) -> None:
        limits = []
        if daily_limit is not None:
            limits.append(f"{daily_limit}/day")
        if monthly_limit is not None:
            limits.append(f"{monthly_limit}/month")
        super().__init__(f"{connector} quota exhausted ({', '.join(limits)})")
        self.connector = connector


class Quota:
    def __init__(
        self,
        engine: Engine,
        connector: str,
        daily_limit: int | None,
        today: date,
        monthly_limit: int | None = None,
    ):
        self.engine = engine
        self.connector = connector
        self.daily_limit = daily_limit
        self.monthly_limit = monthly_limit
        self.today = today

    def used_this_month(self) -> int:
        with session(self.engine) as s:
            query = select(func.coalesce(func.sum(QuotaUsageDB.calls), 0)).where(
                QuotaUsageDB.connector == self.connector,
                QuotaUsageDB.day >= self.today.replace(day=1),
                QuotaUsageDB.day <= self.today,
            )
            return int(s.execute(query).scalar_one())

    def used_today(self) -> int:
        with session(self.engine) as s:
            query = select(func.coalesce(func.sum(QuotaUsageDB.calls), 0)).where(
                QuotaUsageDB.connector == self.connector, QuotaUsageDB.day == self.today
            )
            return int(s.execute(query).scalar_one())

    def available(self) -> int | None:
        """Calls left today under both the daily and the monthly limit; None if unlimited."""
        remaining = []
        if self.daily_limit is not None:
            remaining.append(self.daily_limit - self.used_today())
        if self.monthly_limit is not None:
            remaining.append(self.monthly_limit - self.used_this_month())
        return max(min(remaining), 0) if remaining else None

    def consume(self, n: int = 1) -> None:
        """Records `n` calls; raises QuotaExhausted when there is not enough left."""
        left = self.available()
        if left is not None and left < n:
            raise QuotaExhausted(self.connector, self.daily_limit, self.monthly_limit)
        with session(self.engine) as s:
            stmt = insert(QuotaUsageDB).values(connector=self.connector, day=self.today, calls=n)
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["connector", "day"],
                    set_={"calls": QuotaUsageDB.calls + n},
                )
            )
