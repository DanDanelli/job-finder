"""Batch scoring of scheduled runs, with a stand-in for the Message Batches API."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from sqlalchemy import Engine, select
from test_llm import HAIKU, JOBS, NOW, Response, Usage, client, fit, matches, prepare

from job_finder.config import Settings
from job_finder.db.models import LLMCostDB, MatchDB
from job_finder.db.session import session
from job_finder.domain import FitResult, MatchStatus
from job_finder.llm.batch import CANCEL_WAIT_SECONDS, score_batch
from job_finder.llm.client import LLMClient, cost
from job_finder.llm.scorer import INSTRUCTIONS, Prepared
from job_finder.llm.scorer import prepare as prepare_scoring
from job_finder.service import full_run

Result = SimpleNamespace


def succeeded(score: int = 80, *, stop_reason: str = "end_turn", text: str | None = None) -> Result:
    body = fit(score).model_dump_json() if text is None else text
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=body)],
        stop_reason=stop_reason,
        usage=Usage(),
    )
    return Result(type="succeeded", message=message)


def failed(kind: str) -> Result:
    return Result(type=kind)


def title_of(request: dict[str, Any]) -> str:
    content: str = request["params"]["messages"][0]["content"]
    return content.split("Title: ")[1].split("\n")[0]


class Batches:
    """`ended` after `polls` retrieves, or on the first retrieve after a cancel."""

    def __init__(
        self,
        answer: Callable[[str], Result | None],
        *,
        polls: int = 1,
        ends_on_cancel: bool = True,
        create_error: Exception | None = None,
    ) -> None:
        self.answer = answer
        self.polls = polls
        self.ends_on_cancel = ends_on_cancel
        self.create_error = create_error
        self.requests: list[dict[str, Any]] = []
        self.retrieves = 0
        self.canceled = False
        self.results_read = False

    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        if self.create_error is not None:
            raise self.create_error
        self.requests = requests
        return SimpleNamespace(id="msgbatch_1", processing_status="in_progress")

    def retrieve(self, batch_id: str) -> Any:
        self.retrieves += 1
        ended = self.retrieves >= self.polls or (self.canceled and self.ends_on_cancel)
        return SimpleNamespace(processing_status="ended" if ended else "in_progress")

    def cancel(self, batch_id: str) -> Any:
        self.canceled = True
        return SimpleNamespace(processing_status="canceling")

    def results(self, batch_id: str) -> list[Any]:
        self.results_read = True
        out = []
        for r in reversed(self.requests):  # never in request order
            result = self.answer(title_of(r))
            if result is not None:
                out.append(SimpleNamespace(custom_id=r["custom_id"], result=result))
        return out


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def setup(
    engine: Engine,
    batches: Batches,
    direct: Callable[[dict[str, Any]], Response] = lambda kw: Response(fit(50)),
    cap: float = 5.0,
) -> tuple[LLMClient, Prepared]:
    profile_id = prepare(engine, JOBS)
    c = client(engine, direct, cap=cap)
    c.anthropic.messages.batches = batches
    return c, prepare_scoring(engine, profile_id, top_n=10, now=NOW)


def run_batch(
    engine: Engine, c: LLMClient, p: Prepared, clock: Clock | None = None, max_wait_s: float = 60
) -> dict[int, list[Any]]:
    clock = clock or Clock()
    return score_batch(
        engine,
        c,
        [p],
        model=HAIKU,
        language="en",
        max_wait_s=max_wait_s,
        poll_s=30,
        sleep=clock.sleep,
        clock=clock,
    )


SCORES = {"Cloud Architect": 91, "Brand Manager": 15, "Senior Cloud Engineer": 77}


def test_one_request_per_job_with_the_direct_prompt_and_the_schema(engine: Engine) -> None:
    batches = Batches(lambda title: succeeded(SCORES[title]))
    c, p = setup(engine, batches)

    run_batch(engine, c, p)

    assert len(batches.requests) == 3
    ids = {r["custom_id"] for r in batches.requests}
    assert ids == {f"match-{item.match_id}" for item in p.items}
    params = batches.requests[0]["params"]
    assert params["model"] == HAIKU
    assert params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert "score" in params["output_config"]["format"]["schema"]["properties"]
    assert params["system"][0]["text"] == INSTRUCTIONS.format(language="English")
    assert params["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "stream" not in params


def test_results_are_matched_by_custom_id_and_stored_at_half_price(engine: Engine) -> None:
    batches = Batches(lambda title: succeeded(SCORES[title]))
    c, p = setup(engine, batches)

    leftovers = run_batch(engine, c, p)

    assert leftovers == {}
    assert c.anthropic.messages.calls == []  # nothing direct
    m = matches(engine)
    for title, score in SCORES.items():
        assert (m[title].score, m[title].status, m[title].model) == (score, MatchStatus.NEW, HAIKU)
        assert m[title].attempts == 1
    assert p.summary.scored == 3
    with session(engine) as s:
        costs = s.scalars(select(LLMCostDB)).all()
    assert {(x.operation, x.batch) for x in costs} == {("scoring", True)}
    assert sum(x.cost_usd for x in costs) == pytest.approx(3 * cost(HAIKU, Usage()) / 2)


def test_refusal_or_bad_output_uses_an_attempt_and_stays_pending(engine: Engine) -> None:
    answers = {
        "Cloud Architect": succeeded(stop_reason="refusal"),
        "Brand Manager": succeeded(text='{"score": 500, "summary": "x"}'),
        "Senior Cloud Engineer": succeeded(77),
    }
    c, p = setup(engine, Batches(answers.get))

    leftovers = run_batch(engine, c, p)

    assert leftovers == {}
    m = matches(engine)
    for title in ("Cloud Architect", "Brand Manager"):
        assert (m[title].status, m[title].attempts) == (MatchStatus.PENDING, 1)
    assert m["Senior Cloud Engineer"].score == 77
    assert len(p.summary.failures) == 2
    with session(engine) as s:  # a refusal is still billed
        assert len(s.scalars(select(LLMCostDB)).all()) == 3


def test_errored_expired_and_missing_results_go_back_for_direct_scoring(engine: Engine) -> None:
    # Senior Cloud Engineer has no result at all.
    answers = {"Cloud Architect": failed("errored"), "Brand Manager": failed("expired")}
    c, p = setup(engine, Batches(answers.get))

    leftovers = run_batch(engine, c, p)

    titles = sorted(item.job.title for item in leftovers[p.profile_id])
    assert titles == ["Brand Manager", "Cloud Architect", "Senior Cloud Engineer"]
    m = matches(engine)
    assert all(x.attempts == 0 and x.status == MatchStatus.PENDING for x in m.values())


def test_timeout_cancels_keeps_what_came_back_and_returns_the_rest(engine: Engine) -> None:
    def answer(title: str) -> Result:
        return succeeded(91) if title == "Cloud Architect" else failed("canceled")

    batches = Batches(answer, polls=1_000)
    c, p = setup(engine, batches)
    clock = Clock()

    leftovers = run_batch(engine, c, p, clock, max_wait_s=90)

    assert batches.canceled
    assert clock.now == 120  # canceled at the 90 s poll; ended at the next one
    assert matches(engine)["Cloud Architect"].score == 91
    assert sorted(i.job.title for i in leftovers[p.profile_id]) == [
        "Brand Manager",
        "Senior Cloud Engineer",
    ]


def test_a_batch_that_never_ends_after_the_cancel_is_given_up(engine: Engine) -> None:
    batches = Batches(lambda title: succeeded(), polls=10_000, ends_on_cancel=False)
    c, p = setup(engine, batches)
    clock = Clock()

    leftovers = run_batch(engine, c, p, clock, max_wait_s=60)

    assert not batches.results_read
    assert len(leftovers[p.profile_id]) == 3
    assert clock.now == 60 + CANCEL_WAIT_SECONDS


def _api_error(cls: type[anthropic.APIStatusError], status: int, msg: str) -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    return cls(msg, response=httpx.Response(status, request=request), body=None)


def test_a_create_failure_returns_everything_for_direct_scoring(engine: Engine) -> None:
    error = _api_error(anthropic.InternalServerError, 500, "overloaded")
    c, p = setup(engine, Batches(lambda title: succeeded(), create_error=error))

    leftovers = run_batch(engine, c, p)

    assert len(leftovers[p.profile_id]) == 3


def test_an_account_error_on_create_is_raised(engine: Engine) -> None:
    error = _api_error(anthropic.AuthenticationError, 401, "invalid x-api-key")
    c, p = setup(engine, Batches(lambda title: succeeded(), create_error=error))
    with pytest.raises(anthropic.AuthenticationError):
        run_batch(engine, c, p)


def test_the_cap_stops_the_batch_before_it_is_sent(engine: Engine) -> None:
    batches = Batches(lambda title: succeeded())
    c, p = setup(engine, batches, cap=0.0)

    leftovers = run_batch(engine, c, p)

    assert leftovers == {}
    assert batches.requests == []
    assert p.summary.interrupted and "cap" in p.summary.interrupted


# --- full run -------------------------------------------------------------------------


def run(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, kind: str, batches: Batches, **settings: Any
) -> Any:
    c = client(engine, lambda kw: Response(fit(50)))
    c.anthropic.messages.batches = batches
    clock = Clock()

    def instant(*args: Any, **kw: Any) -> Any:
        return score_batch(*args, **kw, sleep=clock.sleep, clock=clock)

    monkeypatch.setattr("job_finder.service.score_batch", instant)
    everything = {"relevance_min_title": 0, "relevance_min_text": 0} | settings
    report = full_run(
        engine,
        Settings(connectors=[], **everything),
        kind=kind,
        connectors=[],
        client=c,
        now=NOW,
    )
    return c, report


def test_scheduled_runs_use_the_batch_and_score_leftovers_directly(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(engine, JOBS)
    batches = Batches(
        lambda title: failed("errored") if title == "Brand Manager" else succeeded(SCORES[title])
    )

    c, report = run(engine, monkeypatch, "scheduled", batches)

    assert len(batches.requests) == 3
    assert [title_of({"params": kw}) for kw in c.anthropic.messages.calls] == ["Brand Manager"]
    m = matches(engine)
    assert (m["Cloud Architect"].score, m["Brand Manager"].score) == (91, 50)
    assert report.profiles[0].scoring.scored == 3


def test_on_demand_runs_and_a_disabled_batch_score_directly(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare(engine, JOBS)
    batches = Batches(lambda title: succeeded())

    c, _ = run(engine, monkeypatch, "on_demand", batches)
    assert batches.requests == [] and len(c.anthropic.messages.calls) == 3

    with session(engine) as s:  # back to pending for the second run
        for m in s.scalars(select(MatchDB)):
            m.status, m.attempts = MatchStatus.PENDING, 0
    c, _ = run(engine, monkeypatch, "scheduled", batches, batch_scheduled=False)
    assert batches.requests == [] and len(c.anthropic.messages.calls) == 3


def test_fitresult_schema_is_built_like_parse_builds_it() -> None:
    from job_finder.llm.client import json_format

    fmt = json_format(FitResult)
    assert fmt["schema"]["additionalProperties"] is False
