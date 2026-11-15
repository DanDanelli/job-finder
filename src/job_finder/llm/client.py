"""Claude client that records the cost of each call and enforces a monthly cap.

Each response has its `usage` stored in `llm_costs` in its own transaction, so the spend
counts even if the operation that called the LLM fails later. Before each call the
month's spend (UTC) is checked against the configured cap.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy import Engine, func, select

from job_finder.db.models import LLMCostDB
from job_finder.db.session import session

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute TTL (1.25x input)


# List prices as of 2026-11. Haiku 5.5 charges 5x above 100K prompt tokens; the prompts
# here stay far below that.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00),
}
BATCH_DISCOUNT = 0.5


class CostCapReached(Exception):
    def __init__(self, spent: float, cap: float) -> None:
        super().__init__(f"monthly Claude cap reached (US$ {spent:.2f} of US$ {cap:.2f})")
        self.spent = spent
        self.cap = cap


class InvalidResponse(Exception):
    """The model answered, but without usable structured output."""


class _Usage(Protocol):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None


def is_fatal(e: Exception) -> bool:
    """Account errors (key, permission, balance) that apply to every call.

    There is no point moving on to the next job or spending the match's attempts.
    """
    import anthropic

    if isinstance(e, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return True
    return isinstance(e, anthropic.BadRequestError) and "credit balance" in str(e).lower()


def cost(model: str, usage: _Usage, *, batch: bool = False) -> float:
    price = PRICES.get(model)
    if price is None:
        log.warning("no price for model %s; cost recorded as 0", model)
        return 0.0
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + (usage.cache_read_input_tokens or 0) * price.cache_read
        + (usage.cache_creation_input_tokens or 0) * price.cache_write
    ) / 1_000_000
    return total * BATCH_DISCOUNT if batch else total


def start_of_month(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class LLMClient:
    def __init__(
        self,
        engine: Engine,
        anthropic: Any,  # anthropic.Anthropic; Any so tests can pass fakes
        *,
        monthly_cap_usd: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.engine = engine
        self.anthropic = anthropic
        self.monthly_cap_usd = monthly_cap_usd
        self.clock = clock

    def spent_this_month(self) -> float:
        with session(self.engine, commit=False) as s:
            total = s.scalar(
                select(func.coalesce(func.sum(LLMCostDB.cost_usd), 0.0)).where(
                    LLMCostDB.created_at >= start_of_month(self.clock())
                )
            )
        return float(total or 0.0)

    def check_cap(self) -> None:
        spent = self.spent_this_month()
        if spent >= self.monthly_cap_usd:
            raise CostCapReached(spent, self.monthly_cap_usd)

    def record(
        self,
        operation: str,
        model: str,
        usage: _Usage,
        *,
        profile_id: int | None = None,
        run_id: int | None = None,
        batch: bool = False,
    ) -> float:
        amount = cost(model, usage, batch=batch)
        with session(self.engine) as s:
            s.add(
                LLMCostDB(
                    run_id=run_id,
                    profile_id=profile_id,
                    operation=operation,
                    model=model,
                    batch=batch,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_input_tokens or 0,
                    cache_write_tokens=usage.cache_creation_input_tokens or 0,
                    cost_usd=amount,
                    created_at=self.clock(),
                )
            )
        return amount

    def structured(
        self,
        output_format: type[T],
        *,
        operation: str,
        model: str,
        profile_id: int | None = None,
        run_id: int | None = None,
        **params: Any,
    ) -> T:
        """One call with structured output (`messages.parse`), with its cost recorded.

        Raises `InvalidResponse` on refusal, truncation or missing output.
        """
        self.check_cap()
        response = self.anthropic.messages.parse(model=model, output_format=output_format, **params)
        self.record(operation, model, response.usage, profile_id=profile_id, run_id=run_id)
        u = response.usage
        log.info(
            "%s %s: in=%d out=%d cache_read=%d cache_write=%d stop=%s",
            operation,
            model,
            u.input_tokens,
            u.output_tokens,
            u.cache_read_input_tokens or 0,
            u.cache_creation_input_tokens or 0,
            response.stop_reason,
        )
        if response.stop_reason == "refusal":
            raise InvalidResponse("the model refused the request")
        if response.stop_reason == "max_tokens":
            raise InvalidResponse("truncated response (max_tokens)")
        output = response.parsed_output
        if output is None:
            raise InvalidResponse(f"no structured output (stop_reason={response.stop_reason})")
        return output


def make_client(engine: Engine) -> LLMClient:
    """Real client from the settings. Requires ANTHROPIC_API_KEY."""
    import anthropic

    from job_finder.config import get_settings

    settings = get_settings()
    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (put it in .env)")
    return LLMClient(
        engine,
        anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value()),
        monthly_cap_usd=settings.monthly_cost_cap_usd,
    )
