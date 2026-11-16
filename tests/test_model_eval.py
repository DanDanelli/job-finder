"""Model eval with real labels: metrics, leave-one-out few-shot, nothing written to matches."""

from typing import Any

import pytest
from sqlalchemy import Engine, select
from test_llm import HAIKU, NOW, OPUS, Response, client, fit, prepare, raw
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.db.models import FeedbackDB, LLMCostDB, MatchDB
from job_finder.db.session import session
from job_finder.domain import MatchStatus
from job_finder.llm.client import LLMClient
from job_finder.llm.model_eval import (
    STORED,
    auc,
    estimate,
    load_labeled,
    metrics,
    precision_at,
    run_eval,
)

JOBS = [
    raw("1", "Cloud Architect", "AWS Terraform landing zones"),
    raw("2", "Senior Cloud Architect", "AWS consultancy"),
    raw("3", "Brand Manager", "Fashion marketing"),
    raw("4", "Cloud Engineer", "AWS"),
    raw("5", "Data Analyst", "SQL"),  # undecided: not part of the eval
]
DECISIONS = {
    "Cloud Architect": (True, 85, "product company"),
    "Senior Cloud Architect": (False, 80, "body shop"),
    "Brand Manager": (False, 20, None),
    "Cloud Engineer": (True, 70, None),
}


def label(engine: Engine) -> int:
    profile_id = prepare(engine, JOBS)
    with session(engine) as s:
        for m in s.scalars(select(MatchDB)):
            if m.job.title not in DECISIONS:
                continue
            saved, score, note = DECISIONS[m.job.title]
            m.status = MatchStatus.SAVED if saved else MatchStatus.DISMISSED
            m.score = score
            m.model = HAIKU
            s.add(
                FeedbackDB(
                    match_id=m.id,
                    action="save" if saved else "dismiss",
                    origin="site",
                    comment=note,
                )
            )
    return profile_id


def title_of(kw: dict[str, Any]) -> str:
    content: str = kw["messages"][0]["content"]
    return content.split("Title: ")[1].split("\n")[0]


def by_model(scores: dict[str, dict[str, int]]) -> Any:
    return lambda kw: Response(fit(scores[kw["model"]][title_of(kw)]))


PERFECT = {
    "Cloud Architect": 90,
    "Cloud Engineer": 75,
    "Senior Cloud Architect": 50,
    "Brand Manager": 10,
}
INVERTED = {
    "Cloud Architect": 30,
    "Cloud Engineer": 40,
    "Senior Cloud Architect": 80,
    "Brand Manager": 90,
}


def snapshot(engine: Engine) -> list[tuple[Any, ...]]:
    with session(engine) as s:
        return [
            (m.id, m.status, m.score, m.model, m.summary, m.attempts, m.updated_at)
            for m in s.scalars(select(MatchDB).order_by(MatchDB.id))
        ]


# --- metrics ------------------------------------------------------------------------


def test_auc_perfect_inverted_and_ties() -> None:
    assert auc([90, 80], [10, 20]) == 1.0
    assert auc([10], [90]) == 0.0
    assert auc([50], [50]) == 0.5
    assert auc([], [1]) is None


def test_precision_at_k_shares_ties_at_the_cut() -> None:
    pairs = [(90.0, True), (70.0, True), (70.0, False), (10.0, False)]
    assert precision_at(pairs, 1) == 1.0
    # Second place is a tie between a saved and a dismissed job: half a hit.
    assert precision_at(pairs, 2) == pytest.approx(0.75)
    assert precision_at(pairs, 5) is None


def test_metrics_gap_and_digest_counts() -> None:
    m = metrics([(90.0, True), (50.0, True), (70.0, False), (10.0, False)], digest_min_score=60)
    assert (m.n, m.saved) == (4, 2)
    assert m.mean_gap == pytest.approx(30.0)
    assert m.auc == pytest.approx(0.75)
    assert (m.saved_in_digest, m.dismissed_in_digest) == (1, 1)


# --- run ------------------------------------------------------------------------------


def test_scores_every_label_with_each_model_and_writes_nothing_to_matches(engine: Engine) -> None:
    profile_id = label(engine)
    before = snapshot(engine)
    c = client(engine, by_model({HAIKU: INVERTED, OPUS: PERFECT}))

    report = run_eval(engine, c, profile_id, models=[HAIKU, OPUS], language="en")

    assert snapshot(engine) == before
    calls = c.anthropic.messages.calls
    assert [ch["model"] for ch in calls] == [HAIKU] * 4 + [OPUS] * 4  # one model at a time
    assert "Data Analyst" not in {title_of(ch) for ch in calls}
    assert report.metrics(OPUS, 60).auc == 1.0
    assert report.metrics(HAIKU, 60).auc == 0.0
    stored = report.metrics(STORED, 60)
    assert (stored.n, stored.auc) == (4, 0.75)
    with session(engine) as s:
        costs = s.scalars(select(LLMCostDB)).all()
        assert {(x.operation, x.profile_id) for x in costs} == {("eval", profile_id)}
    assert report.calls_by_model == {HAIKU: 4, OPUS: 4}
    assert report.cost_by_model[OPUS] > report.cost_by_model[HAIKU] > 0


def test_a_job_never_sees_its_own_decision(engine: Engine) -> None:
    profile_id = label(engine)
    c = client(engine, by_model({HAIKU: PERFECT}))

    run_eval(engine, c, profile_id, models=[HAIKU], language="en")

    content = {title_of(ch): ch["messages"][0]["content"] for ch in c.anthropic.messages.calls}
    cloud = content["Cloud Architect"]
    examples = cloud.split("</past_decisions>")[0]
    assert "DISMISSED (not interested): Senior Cloud Architect" in examples
    assert "SAVED (interested): Cloud Architect (" not in examples
    assert "product company" not in examples  # its own note
    senior = content["Senior Cloud Architect"].split("</past_decisions>")[0]
    assert "SAVED (interested): Cloud Architect" in senior
    assert "body shop" not in senior


def test_a_failure_is_reported_and_left_out_of_that_models_metrics(engine: Engine) -> None:
    profile_id = label(engine)

    def responder(kw: dict[str, Any]) -> Response:
        if title_of(kw) == "Brand Manager":
            return Response(None, stop_reason="refusal")
        return Response(fit(PERFECT[title_of(kw)]))

    report = run_eval(engine, client(engine, responder), profile_id, models=[HAIKU], language="en")

    assert report.metrics(HAIKU, 60).n == 3
    assert len(report.failures) == 1 and "Brand Manager" in report.failures[0]
    assert report.interrupted is None


def test_the_cost_cap_stops_the_eval(engine: Engine) -> None:
    profile_id = label(engine)
    c = client(engine, by_model({HAIKU: PERFECT, OPUS: PERFECT}), cap=0.0)

    report = run_eval(engine, c, profile_id, models=[HAIKU, OPUS], language="en")

    assert report.interrupted and "cap" in report.interrupted
    assert c.anthropic.messages.calls == []


def test_limit_takes_the_first_labeled_jobs(engine: Engine) -> None:
    profile_id = label(engine)
    c = client(engine, by_model({HAIKU: PERFECT}))
    report = run_eval(engine, c, profile_id, models=[HAIKU], language="en", limit=2)
    assert len(report.rows) == 2 == len(c.anthropic.messages.calls)


def test_estimate_uses_past_scoring_tokens(engine: Engine) -> None:
    profile_id = label(engine)
    fallback = estimate(engine, [HAIKU, OPUS], 10)
    assert (fallback.calls, fallback.basis) == (20, 0)
    assert fallback.cost_usd > 0

    with session(engine) as s:
        s.add(
            LLMCostDB(
                profile_id=profile_id,
                operation="scoring",
                model=HAIKU,
                input_tokens=1_000_000,
                output_tokens=0,
                created_at=NOW,
            )
        )
    measured = estimate(engine, [HAIKU, OPUS], 1)
    # 1M input tokens per call: US$ 0.10 on Haiku + US$ 4.00 on Opus.
    assert measured.basis == 1
    assert measured.cost_usd == pytest.approx(4.10)


def test_load_labeled_only_takes_decisions_with_feedback(engine: Engine) -> None:
    profile_id = label(engine)
    with session(engine) as s:
        m = s.scalars(select(MatchDB).where(MatchDB.job.has(title="Data Analyst"))).one()
        m.status = MatchStatus.DISMISSED  # no feedback row: not a decision by the person
    titles = {item.job.title for item in load_labeled(engine, profile_id)}
    assert titles == set(DECISIONS)


# --- CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_engine(monkeypatch: pytest.MonkeyPatch) -> Any:
    from job_finder.config import get_settings
    from job_finder.db.session import make_engine, upgrade
    from job_finder.llm import client as client_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    url = get_settings().resolved_database_url
    upgrade(url)
    engine = make_engine(url)
    holder: dict[str, LLMClient] = {}

    def fake(eng: Engine) -> LLMClient:
        holder["client"] = client(eng, by_model({HAIKU: INVERTED, OPUS: PERFECT}))
        return holder["client"]

    monkeypatch.setattr(client_mod, "make_client", fake)
    yield engine, holder
    engine.dispose()


def test_cli_shows_the_estimate_and_asks_before_spending(cli_engine: Any) -> None:
    engine, holder = cli_engine
    label(engine)

    r = CliRunner().invoke(cli_app, ["eval", "-p", "daniel"], input="n\n")

    assert r.exit_code == 1
    assert "4 labeled jobs (2 saved) x 2 models" in r.output
    assert "8 calls, about US$" in r.output
    assert holder["client"].anthropic.messages.calls == []


def test_cli_runs_and_prints_the_comparison(cli_engine: Any) -> None:
    engine, holder = cli_engine
    label(engine)

    r = CliRunner().invoke(cli_app, ["eval", "-p", "daniel", "--yes"])

    assert r.exit_code == 0, r.output
    assert len(holder["client"].anthropic.messages.calls) == 8
    assert "production" in r.output and "AUC" in r.output
    assert "opus-5-5" in r.output and "1.00" in r.output


def test_cli_refuses_a_profile_without_both_labels(cli_engine: Any) -> None:
    engine, _ = cli_engine
    prepare(engine, JOBS)
    r = CliRunner().invoke(cli_app, ["eval", "-p", "daniel", "--yes"])
    assert r.exit_code != 0
    assert "needs both saved and dismissed" in r.output
