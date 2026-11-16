"""Learning from feedback: rules proposed by Claude, approved on the site."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from test_llm import Response
from test_llm import client as fake_client
from test_site import PASSWORD, add_matches, add_profile, login, raw

from job_finder.config import Settings
from job_finder.db.models import FeedbackDB, LLMCostDB, MatchDB, ProfileDB
from job_finder.db.session import session
from job_finder.domain import MatchStatus
from job_finder.llm import learner
from job_finder.llm.learner import RulesProposal
from job_finder.scheduler import Scheduler
from job_finder.web.app import create_app

NOW = datetime.now(UTC)


def decide(engine: Engine, name: str, n: int, *, saved: int = 0, note: str | None = None) -> int:
    """Gives the profile `n` decided jobs (the first `saved` saved, the rest dismissed)."""
    jobs = [
        raw(f"{name}{i}", f"Cloud Architect {i}", company=f"Company {name} {i}") for i in range(n)
    ]
    add_matches(engine, name, jobs)
    with session(engine) as s:
        profile_id = s.scalars(select(ProfileDB.id).where(ProfileDB.name == name)).one()
        mine = s.scalars(
            select(MatchDB).where(MatchDB.profile_id == profile_id).order_by(MatchDB.id)
        ).all()
        ours = [m for m in mine if m.job.title.startswith("Cloud Architect ")][-n:]
        assert len(ours) == n
        for i, m in enumerate(ours):
            m.status = MatchStatus.SAVED if i < saved else MatchStatus.DISMISSED
            action = "save" if i < saved else "dismiss"
            s.add(FeedbackDB(match_id=m.id, action=action, origin="web", comment=note))
        return profile_id


def proposing(*rules: str) -> Any:
    return lambda kw: Response(RulesProposal(rules=list(rules)))


def test_propose_stores_a_draft_and_approval_moves_it_into_use(engine: Engine) -> None:
    pid = add_profile(engine, "daniel")
    decide(engine, "daniel", 6, saved=2, note="consultancy again")
    c = fake_client(engine, proposing("Dismisses consultancies", " Dismisses  consultancies ", ""))

    draft = learner.propose(engine, c, pid, model="m", language="en", now=datetime.now(UTC))

    assert draft == ["Dismisses consultancies"]  # trimmed and deduplicated
    st = learner.state(engine, pid)
    assert (st.rules, st.draft, st.saved, st.dismissed) == ([], draft, 2, 4)
    assert st.checked_at is not None and st.new_since_check == 0
    # Not in the scoring prompt yet: only approved rules are.
    with session(engine) as s:
        assert s.get_one(ProfileDB, pid).learned_rules == []
    call = c.anthropic.messages.calls[0]
    message = call["messages"][0]["content"]
    assert "DISMISSED: Cloud Architect" in message and "their note: consultancy again" in message
    assert "SAVED: Cloud Architect" in message
    with session(engine) as s:
        assert [x.operation for x in s.scalars(select(LLMCostDB))] == ["learn_rules"]

    learner.approve(engine, pid, ["Dismisses consultancies", "Likes platform teams", ""])
    st = learner.state(engine, pid)
    assert (st.rules, st.draft) == (["Dismisses consultancies", "Likes platform teams"], None)


def test_same_rules_as_in_use_leave_no_draft(engine: Engine) -> None:
    pid = add_profile(engine, "daniel")
    decide(engine, "daniel", 5)
    learner.approve(engine, pid, ["Dismisses consultancies"])
    c = fake_client(engine, proposing("Dismisses consultancies"))
    assert learner.propose(engine, c, pid, model="m", language="en", now=NOW) is None
    assert learner.state(engine, pid).draft is None
    # The current rules go to Claude, to be kept.
    assert "- Dismisses consultancies" in c.anthropic.messages.calls[0]["messages"][0]["content"]


def test_due() -> None:
    def st(**kw: Any) -> learner.LearningState:
        base = {"rules": [], "draft": None, "checked_at": None, "saved": 3, "dismissed": 3}
        return learner.LearningState(**(base | {"new_since_check": 2} | kw))

    assert learner.due(st(), NOW, 7, 5)
    assert not learner.due(st(saved=1), NOW, 7, 5)  # too few decisions
    assert not learner.due(st(new_since_check=0), NOW, 7, 5)  # nothing new
    assert not learner.due(st(draft=["x"]), NOW, 7, 5)  # one waits for review
    assert not learner.due(st(checked_at=NOW - timedelta(days=3)), NOW, 7, 5)
    assert learner.due(st(checked_at=NOW - timedelta(days=8)), NOW, 7, 5)


class Sent:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str, buttons: Any = None) -> None:
        self.messages.append((chat_id, text))


def test_scheduler_proposes_for_due_profiles_and_tells_them(engine: Engine, db_url: str) -> None:
    import asyncio

    pid = add_profile(engine, "daniel")
    add_profile(engine, "bob")
    decide(engine, "daniel", 5)
    decide(engine, "bob", 2)  # too few
    with session(engine) as s:
        s.get_one(ProfileDB, pid).telegram_chat_id = 42
    sent = Sent()
    settings = Settings(database_url=db_url, site_url="https://pi.example:8443/")
    scheduler = Scheduler(engine, settings, sent)
    c = fake_client(engine, proposing("Dismisses consultancies"))

    assert asyncio.run(scheduler.learn_due(c)) == ["daniel"]
    assert len(sent.messages) == 1
    chat, text = sent.messages[0]
    assert chat == 42 and "1 rules" in text and text.endswith("https://pi.example:8443/learned")
    # Nothing new since: not due again.
    assert asyncio.run(scheduler.learn_due(c)) == []
    assert len(c.anthropic.messages.calls) == 1


@pytest.fixture
def site(engine: Engine, db_url: str) -> Iterator[tuple[TestClient, list[Any]]]:
    clients: list[Any] = []

    def llm() -> Any:
        c = fake_client(engine, proposing("Dismisses consultancies", "Saves platform roles"))
        clients.append(c)
        return c

    settings = Settings(database_url=db_url, schedule_enabled=False, web_secure_cookie=False)
    with TestClient(create_app(settings, llm=llm), follow_redirects=False) as c:
        yield c, clients


def _wait(client: TestClient, gone: str) -> str:
    import time

    deadline = time.monotonic() + 5
    while True:
        text = client.get("/learned").text
        if gone not in text or time.monotonic() > deadline:
            return text
        time.sleep(0.05)


def test_site_suggest_review_and_approve(
    engine: Engine, site: tuple[TestClient, list[Any]]
) -> None:
    client, clients = site
    pid = add_profile(engine, "daniel")
    login(client)
    page = client.get("/learned").text
    assert "Save or dismiss at least 5 jobs first" in page
    assert client.post("/learned/suggest").status_code == 303
    assert clients == []  # not enough decisions: Claude is not called

    decide(engine, "daniel", 5)
    assert "0 rules in use" in client.get("/profile").text
    client.post("/learned/suggest")
    page = _wait(client, "is reading your decisions")
    assert "Suggested rules, waiting for you" in page
    assert "Dismisses consultancies\nSaves platform roles</textarea>" in page
    assert "new suggestion waiting" in client.get("/profile").text

    r = client.post("/learned", data={"rules": "Dismisses consultancies\n\nPrefers remote\n"})
    assert r.headers["location"] == "/learned?notice=saved"
    page = client.get(r.headers["location"]).text
    assert "Saved. The next scorings use these rules." in page
    assert learner.state(engine, pid).rules == ["Dismisses consultancies", "Prefers remote"]
    assert learner.state(engine, pid).draft is None


def test_site_discard_keeps_the_rules_in_use(engine: Engine, site: tuple[TestClient, Any]) -> None:
    client, _ = site
    pid = add_profile(engine, "daniel")
    learner.approve(engine, pid, ["Old rule"])
    with session(engine) as s:
        s.get_one(ProfileDB, pid).rules_draft = ["New rule"]
    login(client)
    assert "New rule</textarea>" in client.get("/learned").text
    client.post("/learned/discard")
    st = learner.state(engine, pid)
    assert (st.rules, st.draft) == (["Old rule"], None)


def test_rules_are_per_profile(engine: Engine, site: tuple[TestClient, Any]) -> None:
    client, _ = site
    add_profile(engine, "daniel")
    other = add_profile(engine, "bob")
    learner.approve(engine, other, ["Her rule"])
    login(client, "daniel", PASSWORD)
    assert "Her rule" not in client.get("/learned").text
    client.post("/learned", data={"rules": "Mine"})
    assert learner.state(engine, other).rules == ["Her rule"]
