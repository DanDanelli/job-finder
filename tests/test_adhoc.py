"""Ad-hoc job evaluation (link or text sent to the bot), without network or the real Claude."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, select

from job_finder import adhoc
from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import LLMCostDB, MatchDB
from job_finder.db.session import session
from job_finder.domain import Filters, FitResult, MatchStatus, Profile
from job_finder.i18n import t
from job_finder.llm.client import LLMClient
from job_finder.pipeline.run import execute_run
from job_finder.telegram import actions
from job_finder.telegram.bot import Batcher

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
URL = "https://jobs.example/job/123"
PAGE = """<html><head><title>Job</title><script>var x = 1;</script></head><body>
<h1>Arquiteto Cloud Sênior</h1><p>Acme Ltda · Remoto - Brasil</p>
<p>Desenhar arquiteturas AWS multi-conta, landing zone e governança.</p></body></html>"""
PASTED_TEXT = "Arquiteto Cloud Sênior na Acme. " + "Requisitos: AWS, Terraform, EKS. " * 12


def public(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def http() -> httpx.Client:
    return httpx.Client()


class Messages:
    def __init__(self, data: adhoc.JobData, fit: FitResult) -> None:
        self.data, self.fit = data, fit
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kw: Any) -> Any:
        self.calls.append(kw)
        output = self.data if kw["output_format"] is adhoc.JobData else self.fit

        class Response:
            parsed_output = output
            stop_reason = "end_turn"

            class usage:
                input_tokens = 1_000
                output_tokens = 100
                cache_read_input_tokens = 0
                cache_creation_input_tokens = 0

        return Response()


class Anthropic:
    def __init__(self, messages: Messages) -> None:
        self.messages = messages


def job_data(**kw: Any) -> adhoc.JobData:
    base: dict[str, Any] = {
        "is_job": True,
        "title": "Arquiteto Cloud Sênior",
        "company": "Acme",
        "location": "Remoto - Brasil",
        "work_mode": "remote",
    }
    return adhoc.JobData.model_validate(base | kw)


def client(
    engine: Engine, d: adhoc.JobData | None = None, score: int = 82
) -> tuple[LLMClient, Messages]:
    m = Messages(d or job_data(), FitResult(score=score, strengths=["AWS"], summary="Good."))
    return LLMClient(engine, Anthropic(m), monthly_cap_usd=5, clock=lambda: NOW), m


def profiles(engine: Engine, filters: Filters | None = None) -> int:
    with session(engine) as s:
        p = repo.save_profile(s, Profile(name="alice", filters=filters or Filters()))
        repo.save_profile(s, Profile(name="bob"))
        return p.id


def evaluate(
    engine: Engine,
    profile_id: int,
    message: str,
    c: LLMClient,
    http: httpx.Client,
    language: str = "en",
) -> tuple[str, list[Any]]:
    return actions.evaluate_in_chat(
        engine,
        Settings(anthropic_api_key="sk-test", language=language),
        profile_id,
        message,
        client=c,
        http=http,
        now=NOW,
        resolver=public,
    )


# --- input and download -------------------------------------------------------


def test_parses_link_and_text() -> None:
    assert adhoc.parse_message(f"look at this one: {URL}.") == adhoc.Input(URL, None)
    entry = adhoc.parse_message(PASTED_TEXT)
    assert entry.url is None and entry.text == PASTED_TEXT.strip()
    with pytest.raises(adhoc.InvalidInput, match=r"input\.empty"):
        adhoc.parse_message("hi, how are you?")


@respx.mock
def test_fetches_the_page_as_text(http: httpx.Client) -> None:
    respx.get(URL).respond(200, html=PAGE)
    text = adhoc.fetch_page(http, URL, public)
    assert "Arquiteto Cloud Sênior" in text and "var x" not in text


@respx.mock
def test_blocking_site_asks_for_the_text(http: httpx.Client) -> None:
    respx.get(URL).respond(403, html="Security Check")
    with pytest.raises(adhoc.InvalidInput) as e:
        adhoc.fetch_page(http, URL, public)
    assert e.value.notice == adhoc.Notice("input.http_status", {"status": 403})


@pytest.mark.parametrize(
    "url", ["http://localhost:8000/health", "http://192.168.1.10/", "ftp://jobs.example/x"]
)
def test_does_not_open_local_network_addresses(http: httpx.Client, url: str) -> None:
    def resolver(host: str) -> list[str]:
        return {"localhost": ["127.0.0.1"], "192.168.1.10": ["192.168.1.10"]}.get(
            host, ["93.184.216.34"]
        )

    with pytest.raises(adhoc.InvalidInput):
        adhoc.fetch_page(http, url, resolver)


@respx.mock
def test_redirect_to_the_local_network_is_blocked(http: httpx.Client) -> None:
    respx.get(URL).respond(302, headers={"location": "http://router.local/admin"})
    internal = respx.get("http://router.local/admin").respond(200, html="admin")

    def resolver(host: str) -> list[str]:
        return ["192.168.0.1"] if host == "router.local" else ["93.184.216.34"]

    with pytest.raises(adhoc.InvalidInput, match=r"input\.not_public"):
        adhoc.fetch_page(http, URL, resolver)
    assert not internal.called


# --- end-to-end evaluation ----------------------------------------------------


@respx.mock
def test_link_becomes_a_scored_match_with_buttons(engine: Engine, http: httpx.Client) -> None:
    respx.get(URL).respond(200, html=PAGE)
    pid = profiles(engine)
    c, msgs = client(engine)

    text, buttons = evaluate(engine, pid, f"what do you think? {URL}", c, http)

    assert "<b>82</b>" in text and "Arquiteto Cloud Sênior" in text
    with session(engine) as s:
        m = s.scalars(select(MatchDB)).one()
        assert (m.status, m.score, m.notified_at is not None) == (MatchStatus.NEW, 82, True)
        assert m.job.url == URL
        operations = sorted(s.scalars(select(LLMCostDB.operation)))
    assert [b.callback for b in buttons[:2]] == [f"fb:s:{m.id}", f"fb:d:{m.id}"]
    assert buttons[2].url == URL
    assert operations == ["extract_job", "scoring"]
    # The page text goes to the LLM as data, inside <page>.
    assert "<page>" in msgs.calls[0]["messages"][0]["content"]


@respx.mock
def test_same_link_is_not_evaluated_again(engine: Engine, http: httpx.Client) -> None:
    route = respx.get(URL).respond(200, html=PAGE)
    pid = profiles(engine)
    c, msgs = client(engine)
    evaluate(engine, pid, URL, c, http)

    text, buttons = evaluate(engine, pid, URL, c, http)

    assert t("en", "evaluate.already") in text and buttons
    assert route.call_count == 1 and len(msgs.calls) == 2


def test_pasted_text_without_link_uses_a_google_search(engine: Engine, http: httpx.Client) -> None:
    pid = profiles(engine)
    c, _ = client(engine)
    _, buttons = evaluate(engine, pid, PASTED_TEXT, c, http)
    assert buttons[2].url is not None and buttons[2].url.startswith("https://www.google.com/search")


@respx.mock
def test_page_that_is_not_a_job_asks_for_the_text(engine: Engine, http: httpx.Client) -> None:
    respx.get(URL).respond(200, html="<p>Log in to continue</p>")
    pid = profiles(engine)
    c, msgs = client(engine, job_data(is_job=False, title=""))
    text, buttons = evaluate(engine, pid, URL, c, http)
    assert text == t("en", "input.not_a_job") and buttons == []
    assert len(msgs.calls) == 1  # never got to scoring


def test_warns_when_the_filters_would_discard(engine: Engine, http: httpx.Client) -> None:
    pid = profiles(engine, Filters(blocked_keywords=["estágio"]))
    c, _ = client(engine, job_data(title="Estágio em Cloud", closed=True))
    text, _ = evaluate(engine, pid, PASTED_TEXT, c, http)
    assert t("en", "reason.blocked_keyword") in text and t("en", "evaluate.closed") in text


def test_notices_follow_the_configured_language(engine: Engine, http: httpx.Client) -> None:
    pid = profiles(engine, Filters(blocked_keywords=["estágio"]))
    c, msgs = client(engine, job_data(title="Estágio em Cloud"))
    text, buttons = evaluate(engine, pid, PASTED_TEXT, c, http, language="pt-BR")
    assert "título com palavra bloqueada" in text
    assert buttons[0].text == t("pt-BR", "button.save")
    # Claude is asked to answer in the same language.
    assert "Brazilian Portuguese" in str(msgs.calls[-1]["system"])


def test_adhoc_job_does_not_enter_the_other_profiles_runs(
    engine: Engine, http: httpx.Client
) -> None:
    pid = profiles(engine)
    c, _ = client(engine)
    evaluate(engine, pid, PASTED_TEXT, c, http)
    with session(engine) as s:
        summary = execute_run(s, engine, [], repo.list_profiles(s), NOW)
    assert all(p.evaluated == 0 for p in summary.profiles)


def test_without_a_claude_key(engine: Engine) -> None:
    pid = profiles(engine)
    text, buttons = actions.evaluate_in_chat(engine, Settings(), pid, PASTED_TEXT)
    assert "ANTHROPIC_API_KEY" in text and buttons == []


# --- bot ----------------------------------------------------------------------


def test_batcher_joins_messages_split_by_telegram() -> None:
    processed: list[tuple[int, str]] = []

    async def process(chat_id: int, text: str) -> None:
        processed.append((chat_id, text))

    async def scenario(add: Callable[[int, str], None]) -> None:
        add(1, "part 1")
        add(2, "another chat")
        await asyncio.sleep(0.01)
        add(1, "part 2")
        await asyncio.sleep(0.2)

    async def run() -> None:
        batcher = Batcher(process, wait_s=0.05)
        await scenario(batcher.add)

    asyncio.run(run())
    assert sorted(processed) == [(1, "part 1\npart 2"), (2, "another chat")]
