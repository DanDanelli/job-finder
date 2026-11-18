"""Client, profile extraction, BM25 and scoring, with a stand-in for the Claude SDK (no network)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.db import repo
from job_finder.db.models import FeedbackDB, LLMCostDB, MatchDB, ProfileDB
from job_finder.db.session import session
from job_finder.domain import (
    Filters,
    FitResult,
    Job,
    Language,
    MatchStatus,
    Profile,
    ProfileData,
    Query,
    RawJob,
    Skill,
)
from job_finder.llm.client import CostCapReached, InvalidResponse, LLMClient, cost
from job_finder.llm.profile_extractor import ProfileExtraction, extract_profile
from job_finder.llm.scorer import INSTRUCTIONS, MAX_ATTEMPTS, score_pending
from job_finder.pipeline.prerank import (
    Relevance,
    RelevanceCutoff,
    bm25,
    idf_table,
    job_document,
    prerank,
    relevance,
)
from job_finder.pipeline.run import execute_run
from job_finder.quota import Quota

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"


@dataclass
class Usage:
    input_tokens: int = 1_000
    output_tokens: int = 200
    cache_read_input_tokens: int | None = 0
    cache_creation_input_tokens: int | None = 0


@dataclass
class Response:
    parsed_output: Any
    stop_reason: str = "end_turn"
    usage: Usage | None = None

    def __post_init__(self) -> None:
        self.usage = self.usage or Usage()


class Messages:
    def __init__(self, responder: Callable[[dict[str, Any]], Response]) -> None:
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kw: Any) -> Response:
        self.calls.append(kw)
        return self.responder(kw)


class Anthropic:
    def __init__(self, responder: Callable[[dict[str, Any]], Response]) -> None:
        self.messages = Messages(responder)


def client(engine: Engine, responder: Callable[..., Response], cap: float = 5.0) -> LLMClient:
    return LLMClient(engine, Anthropic(responder), monthly_cap_usd=cap, clock=lambda: NOW)


def fit(score: int = 80, **kw: Any) -> FitResult:
    return FitResult(score=score, summary="ok", **kw)


# --- cost and cap --------------------------------------------------------------


def test_cost_per_model_with_cache_and_batch() -> None:
    u = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    assert cost(HAIKU, u) == pytest.approx(0.10 + 0.50 + 0.01 + 0.125)
    assert cost(OPUS, Usage(input_tokens=1_000_000, output_tokens=0)) == pytest.approx(4.0)
    assert cost(HAIKU, u, batch=True) == pytest.approx((0.10 + 0.50 + 0.01 + 0.125) / 2)
    assert cost("unknown-model", u) == 0.0


def test_records_cost_and_respects_the_monthly_cap(engine: Engine) -> None:
    c = client(engine, lambda kw: Response(fit()), cap=0.0005)
    c.structured(FitResult, operation="scoring", model=HAIKU, max_tokens=10, messages=[])
    # 1000 in x 0.10 + 200 out x 0.50 = 0.0002 per call
    assert c.spent_this_month() == pytest.approx(0.0002)
    c.structured(FitResult, operation="scoring", model=HAIKU, max_tokens=10, messages=[])
    c.structured(FitResult, operation="scoring", model=HAIKU, max_tokens=10, messages=[])
    with pytest.raises(CostCapReached):
        c.structured(FitResult, operation="scoring", model=HAIKU, max_tokens=10, messages=[])
    assert len(c.anthropic.messages.calls) == 3  # the fourth was never made


def test_last_months_spend_does_not_count(engine: Engine) -> None:
    past = LLMClient(
        engine,
        None,
        monthly_cap_usd=1,
        clock=lambda: datetime(2026, 10, 31, 23, 59, tzinfo=UTC),
    )
    past.record("scoring", OPUS, Usage(input_tokens=1_000_000, output_tokens=0))
    assert past.spent_this_month() == pytest.approx(4.0)
    assert client(engine, lambda kw: Response(fit())).spent_this_month() == 0.0


@pytest.mark.parametrize(
    "response",
    [
        Response(None, stop_reason="refusal"),
        Response(None, stop_reason="max_tokens"),
        Response(None),
    ],
)
def test_invalid_response_raises_but_records_the_cost(engine: Engine, response: Response) -> None:
    c = client(engine, lambda kw: response)
    with pytest.raises(InvalidResponse):
        c.structured(FitResult, operation="scoring", model=HAIKU, max_tokens=10, messages=[])
    assert c.spent_this_month() > 0


# --- profile extraction -------------------------------------------------------


def test_extract_profile_sends_the_pdf_and_returns_the_data(engine: Engine, tmp_path: Path) -> None:
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    expected = ProfileExtraction(
        data=ProfileData(summary="Cloud architect", roles=["Cloud Architect"]),
        search_terms=["Cloud Architect"],
    )
    c = client(engine, lambda kw: Response(expected))

    assert extract_profile(c, pdf, model=OPUS, language="pt-BR") == expected

    call = c.anthropic.messages.calls[0]
    assert call["model"] == OPUS
    assert call["output_format"] is ProfileExtraction
    assert call["output_config"] == {"effort": "high"}
    document = call["messages"][0]["content"][0]
    assert document["type"] == "document"
    assert document["source"]["media_type"] == "application/pdf"
    with session(engine) as s:
        assert s.scalars(select(LLMCostDB.operation)).all() == ["extract_profile"]


# --- BM25 ---------------------------------------------------------------------


def test_bm25_prefers_the_document_with_more_query_terms() -> None:
    docs = [["aws", "terraform", "kubernetes"], ["excel", "sales"], ["aws", "sales"]]
    s = bm25(["aws", "terraform"], docs)
    assert s[0] > s[2] > s[1] == 0.0
    assert bm25(["aws"], []) == []


def test_prerank_uses_title_skills_and_queries() -> None:
    profile = Profile(
        name="d",
        data=ProfileData(roles=["Cloud Architect"], skills=[Skill(name="AWS", weight=5)]),
        queries=[Query(term="DevOps Lead")],
    )
    jobs = [
        Job(title="Brand Manager", url="https://x/1", description="fashion marketing"),
        Job(title="Cloud Architect", url="https://x/2", description="AWS, landing zones"),
        Job(title="DevOps Lead", url="https://x/3", description="CI/CD"),
    ]
    s = prerank(profile, jobs)
    assert s[1] > s[2] > s[0]


def test_relevance_weighs_rare_terms_and_ignores_common_words() -> None:
    profile = Profile(
        name="d",
        data=ProfileData(roles=["Cloud Architect"], skills=[Skill(name="AWS", weight=5)]),
    )
    jobs = [
        Job(title="Cloud Architect", url="https://x/1", description="AWS landing zones"),
        Job(title="Senior Nurse", url="https://x/2", description="senior care, remote"),
        Job(title="Senior Brand Manager", url="https://x/3", description="senior, remote"),
        Job(title="Senior Data Engineer", url="https://x/4", description="AWS pipelines"),
    ]
    idf = idf_table(job_document(j) for j in jobs)
    r = relevance(profile, jobs, idf)
    cutoff = RelevanceCutoff()
    assert [cutoff.passes(x) for x in r] == [True, False, False, True]
    assert r[0].title > r[3].title == 0.0 < r[3].text


def test_profile_without_terms_makes_everything_relevant() -> None:
    jobs = [Job(title="Anything", url="https://x/1")]
    assert relevance(Profile(name="empty"), jobs, idf_table([])) == [Relevance(1.0, 1.0)]


# --- scoring ------------------------------------------------------------------


def prepare(engine: Engine, jobs: list[RawJob]) -> int:
    with session(engine) as s:
        repo.save_profile(
            s,
            Profile(
                name="alice",
                data=ProfileData(
                    summary="Senior cloud architect",
                    roles=["Cloud Architect"],
                    skills=[Skill(name="AWS", weight=5), Skill(name="Terraform", weight=4)],
                    languages=[Language(code="pt", level="native")],
                ),
                queries=[Query(term="Cloud Architect")],
                filters=Filters(remote_from=["PT"], languages=["pt", "en"]),
                preferences="No body shop consultancies.",
            ),
        )

    class Source:
        name = "f"
        daily_limit = None

        def search(self, queries: object, quota: Quota) -> list[RawJob]:
            return jobs

    with session(engine) as s:
        execute_run(s, engine, [Source()], repo.list_profiles(s), NOW)
        profile = repo.find_profile(s, "alice")
        assert profile is not None
        return profile.id


def raw(source_id: str, title: str, description: str = "", **kw: Any) -> RawJob:
    base: dict[str, Any] = {
        "source": "f",
        "source_id": source_id,
        "title": title,
        "company": f"Company {source_id}",
        "description": description,
        "location": "Europe",
        "work_mode": "remote",
        "url": f"https://f.example/{source_id}",
        "posted_at": NOW - timedelta(days=1),
    }
    return RawJob.model_validate(base | kw)


JOBS = [
    raw("1", "Cloud Architect", "AWS Terraform landing zones"),
    raw("2", "Brand Manager", "Fashion marketing"),
    raw("3", "Senior Cloud Engineer", "AWS"),
]


def score(engine: Engine, c: LLMClient, profile_id: int, top_n: int = 10) -> Any:
    return score_pending(engine, c, profile_id, model=HAIKU, language="en", top_n=top_n, now=NOW)


def test_jobs_below_the_relevance_cutoff_never_reach_the_llm(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS)
    c = client(engine, lambda kw: Response(fit(70)))

    r = score_pending(
        engine,
        c,
        profile_id,
        model=HAIKU,
        language="en",
        top_n=10,
        now=NOW,
        cutoff=RelevanceCutoff(),
    )

    assert (r.pending, r.skipped, r.scored) == (3, 1, 2)
    m = matches(engine)
    assert m["Brand Manager"].status == MatchStatus.SKIPPED
    assert m["Brand Manager"].score is None
    sent = [call["messages"][0]["content"] for call in c.anthropic.messages.calls]
    assert not any("Brand Manager" in text for text in sent)
    # Skipped jobs are not picked up again.
    assert (
        score_pending(
            engine,
            c,
            profile_id,
            model=HAIKU,
            language="en",
            top_n=10,
            now=NOW,
            cutoff=RelevanceCutoff(),
        ).pending
        == 0
    )


def matches(engine: Engine) -> dict[str, MatchDB]:
    with session(engine) as s:
        everything = s.scalars(select(MatchDB)).all()
        for m in everything:
            _ = m.job.title
        s.expunge_all()
        return {m.job.title: m for m in everything}


def test_scores_the_bm25_top_n_and_stores_the_result(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS)
    c = client(
        engine,
        lambda kw: Response(fit(91, strengths=["AWS"], gaps=["Azure"])),
    )

    r = score(engine, c, profile_id, top_n=2)

    assert (r.pending, r.scored, r.failures) == (3, 2, [])
    m = matches(engine)
    # BM25 sends the two cloud jobs to the LLM and leaves the marketing one pending.
    assert m["Cloud Architect"].status == MatchStatus.NEW
    assert m["Cloud Architect"].score == 91
    assert m["Cloud Architect"].strengths == ["AWS"]
    assert m["Cloud Architect"].model == HAIKU
    assert m["Senior Cloud Engineer"].status == MatchStatus.NEW
    assert m["Brand Manager"].status == MatchStatus.PENDING
    assert m["Brand Manager"].prerank_score is not None
    assert m["Cloud Architect"].prerank_score > m["Brand Manager"].prerank_score  # type: ignore[operator]

    with session(engine) as s:
        costs = s.scalars(select(LLMCostDB)).all()
        assert [(x.operation, x.profile_id) for x in costs] == [("scoring", profile_id)] * 2


def test_prompt_prefix_is_stable_and_marked_for_caching(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS)
    c = client(engine, lambda kw: Response(fit()))
    score(engine, c, profile_id)

    calls = c.anthropic.messages.calls
    assert len(calls) == 3
    assert all(ch["system"] == calls[0]["system"] for ch in calls)  # same prefix
    instructions, profile = calls[0]["system"]
    assert instructions["text"] == INSTRUCTIONS.format(language="English")
    assert profile["cache_control"] == {"type": "ephemeral"}
    assert "Terraform" in profile["text"] and "body shop" in profile["text"]
    assert calls[0]["output_config"] == {"effort": "low"}
    # Only the job changes, in the user message, delimited as data.
    assert len({ch["messages"][0]["content"] for ch in calls}) == 3
    assert calls[0]["messages"][0]["content"].startswith("<job>")


def test_similar_past_decisions_go_after_the_cache_breakpoint(engine: Engine) -> None:
    profile_id = prepare(engine, [*JOBS, raw("4", "Senior Cloud Architect", "AWS consultancy")])
    with session(engine) as s:
        m = s.scalars(
            select(MatchDB).join(MatchDB.job).where(MatchDB.job.has(title="Senior Cloud Architect"))
        ).one()
        m.status = MatchStatus.DISMISSED
        m.score = 82
        s.add(FeedbackDB(match_id=m.id, action="dismiss", origin="telegram", comment="body shop"))
    c = client(engine, lambda kw: Response(fit()))
    score(engine, c, profile_id)

    calls = {
        ch["messages"][0]["content"].split("Title: ")[1].split("\n")[0]: ch
        for ch in c.anthropic.messages.calls
    }
    assert set(calls) == {
        "Cloud Architect",
        "Brand Manager",
        "Senior Cloud Engineer",
    }  # the dismissed one is not rescored
    cloud = calls["Cloud Architect"]["messages"][0]["content"]
    assert cloud.startswith("<past_decisions>")
    assert "DISMISSED (not interested): Senior Cloud Architect" in cloud
    assert "your score then: 82" in cloud and "their note: body shop" in cloud
    assert cloud.index("</past_decisions>") < cloud.index("<job>")
    # Titles that share only a generic word, or nothing, get no examples.
    assert calls["Brand Manager"]["messages"][0]["content"].startswith("<job>")
    assert calls["Senior Cloud Engineer"]["messages"][0]["content"].startswith("<job>")
    # The cached prefix is still the same for every job.
    systems = [ch["system"] for ch in c.anthropic.messages.calls]
    assert systems[0] == systems[1] == systems[2]


def test_few_shot_similarity_is_on_titles_and_normalized() -> None:
    from job_finder.domain import WorkMode
    from job_finder.llm.fewshot import Example, similar, title_terms

    def ex(i: int, title: str) -> Example:
        return Example(i, False, title, None, None, "remote", None, None, title_terms(title))

    pool = [
        ex(1, "DevOps Engineer"),
        ex(2, "Senior Data Engineer"),
        ex(3, "Salesforce Pardot Consultant"),
        ex(4, "Remote Office Assistant"),
    ]

    def titles(title: str, **kw: Any) -> list[str]:
        job = Job(title=title, url="https://x.example", work_mode=WorkMode.REMOTE, **kw)
        return [e.title for e in similar(pool, job)]

    assert titles("Azure DevOps Engineer") == ["DevOps Engineer"]
    assert titles("[Job - 32643] Data Engineer") == ["Senior Data Engineer"]
    # A long description full of the other jobs' words does not make them similar.
    assert titles("Cloud Architect", description="Salesforce office assistant remote") == []
    assert similar(pool, Job(title="DevOps Engineer", url="https://x.example"), exclude=1) == []


def test_approved_learned_rules_go_into_the_profile_block(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS[:1])
    with session(engine) as s:
        s.get_one(ProfileDB, profile_id).learned_rules = ["Dismisses consultancies"]
    c = client(engine, lambda kw: Response(fit()))
    score(engine, c, profile_id)
    assert (
        "learned_from_feedback:\n- Dismisses consultancies"
        in c.anthropic.messages.calls[0]["system"][1]["text"]
    )


def test_failure_leaves_it_pending_and_does_not_stop_the_others(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS)

    def responder(kw: dict[str, Any]) -> Response:
        content = kw["messages"][0]["content"]
        if "Brand Manager" in content:
            return Response(None, stop_reason="refusal")
        if "Senior Cloud Engineer" in content:
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api"))
        return Response(fit())

    r = score(engine, client(engine, responder), profile_id)

    assert r.scored == 1
    assert len(r.failures) == 2
    m = matches(engine)
    assert m["Cloud Architect"].status == MatchStatus.NEW
    assert (m["Brand Manager"].status, m["Brand Manager"].attempts) == (MatchStatus.PENDING, 1)
    assert m["Senior Cloud Engineer"].attempts == 1


def test_does_not_rescore_and_gives_up_after_max_attempts(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS[:1])
    refusal = client(engine, lambda kw: Response(None, stop_reason="refusal"))
    for _ in range(MAX_ATTEMPTS):
        score(engine, refusal, profile_id)
    assert len(refusal.anthropic.messages.calls) == MAX_ATTEMPTS

    after = client(engine, lambda kw: Response(fit()))
    assert score(engine, after, profile_id).pending == 0
    assert after.anthropic.messages.calls == []


def test_scored_job_does_not_go_back_to_the_llm(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS)
    score(engine, client(engine, lambda kw: Response(fit())), profile_id)
    second = client(engine, lambda kw: Response(fit()))
    assert score(engine, second, profile_id).scored == 0
    assert second.anthropic.messages.calls == []


def test_cost_cap_interrupts_scoring(engine: Engine) -> None:
    profile_id = prepare(engine, JOBS)
    c = client(engine, lambda kw: Response(fit()), cap=0.0003)  # room for 2 calls
    r = score(engine, c, profile_id)
    assert r.scored == 2
    assert r.interrupted and "monthly Claude cap" in r.interrupted
    assert sum(m.status == MatchStatus.PENDING for m in matches(engine).values()) == 1


def _error(cls: type[anthropic.APIStatusError], status: int, msg: str) -> Exception:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api"))
    return cls(msg, response=response, body={"error": {"message": msg}})


@pytest.mark.parametrize(
    "error",
    [
        _error(anthropic.AuthenticationError, 401, "invalid x-api-key"),
        _error(anthropic.BadRequestError, 400, "Your credit balance is too low to access"),
    ],
)
def test_account_error_interrupts_without_using_attempts(engine: Engine, error: Exception) -> None:
    profile_id = prepare(engine, JOBS)

    def responder(kw: dict[str, Any]) -> Response:
        raise error

    c = client(engine, responder)
    with pytest.raises(type(error)):
        score(engine, c, profile_id)
    assert len(c.anthropic.messages.calls) == 1
    assert all(m.attempts == 0 for m in matches(engine).values())


def test_cli_shows_api_error_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from job_finder.config import get_settings
    from job_finder.db.session import upgrade
    from job_finder.llm import client as client_mod

    upgrade(get_settings().resolved_database_url)

    def responder(kw: dict[str, Any]) -> Response:
        raise _error(anthropic.BadRequestError, 400, "Your credit balance is too low")

    monkeypatch.setattr(
        client_mod,
        "make_client",
        lambda eng: LLMClient(eng, Anthropic(responder), monthly_cap_usd=5),
    )
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    r = CliRunner().invoke(cli_app, ["profile", "import", str(pdf), "-p", "x"])
    assert r.exit_code == 1
    assert "credit balance is too low" in r.output
    assert "Traceback" not in r.output


# --- CLI ----------------------------------------------------------------------


def test_cli_import_fills_in_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from job_finder.config import get_settings
    from job_finder.db.session import make_engine, upgrade
    from job_finder.llm import client as client_mod

    url = get_settings().resolved_database_url
    upgrade(url)
    engine = make_engine(url)
    extraction = ProfileExtraction(
        data=ProfileData(summary="Brand architect", roles=["Brand Manager"]),
        search_terms=["Brand Manager", "Marketing Manager"],
    )
    monkeypatch.setattr(
        client_mod,
        "make_client",
        lambda eng: LLMClient(eng, Anthropic(lambda kw: Response(extraction)), monthly_cap_usd=5),
    )
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    runner = CliRunner()
    r = runner.invoke(cli_app, ["profile", "import", str(pdf), "--profile", "bob"])
    assert r.exit_code == 0, r.output

    with session(engine) as s:
        db = repo.find_profile(s, "bob")
        assert db is not None
        profile = repo.profile_from(db)
        assert db.resume_path and Path(db.resume_path).exists()
    assert profile.data.roles == ["Brand Manager"]
    assert [(q.term, q.active) for q in profile.queries] == [
        ("Brand Manager", True),
        ("Marketing Manager", True),
    ]

    # Export and load back keeps everything; a YAML without `data` keeps the extracted data.
    exported = runner.invoke(cli_app, ["profile", "export", "bob"])
    assert exported.exit_code == 0, exported.output
    yaml_without_data = tmp_path / "bob.yaml"
    yaml_without_data.write_text("name: bob\nqueries:\n  - term: Brand Manager\n")
    assert runner.invoke(cli_app, ["profile", "load", str(yaml_without_data)]).exit_code == 0
    with session(engine) as s:
        db = repo.find_profile(s, "bob")
        assert db is not None
        assert repo.profile_from(db).data.roles == ["Brand Manager"]
    assert "Brand architect" in exported.output


def test_cli_score_without_key_fails_with_a_message() -> None:
    r = CliRunner().invoke(cli_app, ["score"])
    assert r.exit_code != 0
    assert "ANTHROPIC_API_KEY" in r.output


def test_profile_block_has_the_minimum_per_contract() -> None:
    from job_finder.domain import Salary
    from job_finder.llm.scorer import profile_block

    filters = Filters(
        min_salary_by_contract={"contract": Salary(minimum=30_000, currency="BRL", period="month")}
    )
    block = profile_block(Profile(name="x", filters=filters))
    assert "contractor (PJ in Brazil)" in block and "30000" in block
