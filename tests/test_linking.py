"""Linking a Telegram chat from the site, and creating a profile from the terminal."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import ProfileDB
from job_finder.db.session import session
from job_finder.domain import Profile
from job_finder.scheduler import Scheduler
from job_finder.telegram import actions
from job_finder.telegram.linking import TelegramLinks
from job_finder.web import auth
from job_finder.web.app import create_app

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PASSWORD = "correct horse battery"
CHAT = 1001


def add_profile(engine: Engine, name: str, chat: int | None = None) -> int:
    with session(engine) as s:
        db = repo.save_profile(s, Profile(name=name, telegram_chat_id=chat))
        db.password_hash = auth.hash_password(PASSWORD)
        return db.id


def chat_of(engine: Engine, profile_id: int) -> int | None:
    with session(engine, commit=False) as s:
        return s.get_one(ProfileDB, profile_id).telegram_chat_id


# --- the codes ----------------------------------------------------------------------


def test_a_code_works_once() -> None:
    links = TelegramLinks()
    code = links.new(7, NOW)
    assert links.pending(7, NOW) == code
    assert links.redeem("wrong", NOW) is None
    assert links.redeem(f" {code.code} ", NOW) == 7
    assert links.redeem(code.code, NOW) is None
    assert links.pending(7, NOW) is None


def test_a_code_expires_and_a_new_one_replaces_the_old() -> None:
    links = TelegramLinks(ttl=timedelta(minutes=15))
    old = links.new(7, NOW)
    new = links.new(7, NOW)
    assert links.redeem(old.code, NOW) is None
    assert links.pending(7, NOW + timedelta(minutes=15)) is None
    assert links.redeem(new.code, NOW + timedelta(minutes=15)) is None


def test_codes_fit_a_telegram_deep_link() -> None:
    links = TelegramLinks()
    code = links.new(7, NOW)
    assert links.deep_link(code) is None  # no bot yet
    links.bot_username = "jobs_bot"
    assert links.deep_link(code) == f"https://t.me/jobs_bot?start={code.code}"
    assert code.code.replace("-", "").replace("_", "").isalnum() and len(code.code) <= 64


def test_link_chat_refuses_a_chat_of_another_profile(engine: Engine) -> None:
    alice = add_profile(engine, "alice", chat=CHAT)
    bob = add_profile(engine, "bob")
    assert actions.link_chat(engine, CHAT, bob) is None
    assert chat_of(engine, alice) == CHAT
    linked = actions.link_chat(engine, CHAT, alice)  # the same profile: fine
    assert linked is not None and linked.name == "alice"
    assert actions.link_chat(engine, 2002, bob) is not None
    assert chat_of(engine, bob) == 2002


# --- the Account page ---------------------------------------------------------------


@pytest.fixture
def client(db_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=db_url, schedule_enabled=False, web_secure_cookie=False)
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


def test_account_page_links_and_unlinks_telegram(engine: Engine, client: TestClient) -> None:
    alice = add_profile(engine, "alice")
    client.post("/login", data={"username": "alice", "password": PASSWORD})
    links: TelegramLinks = client.app.state.telegram_links  # type: ignore[attr-defined]

    page = client.get("/account").text
    assert "JOBFINDER_TELEGRAM_BOT_TOKEN" in page  # no bot: nothing to link
    assert client.post("/account/telegram").status_code == 303
    assert links.pending(alice, datetime.now(UTC)) is None

    links.bot_username = "jobs_bot"
    page = client.get("/account").text
    assert "Link Telegram" in page and "Not linked yet" in page
    client.post("/account/telegram")
    code = links.pending(alice, datetime.now(UTC))
    assert code is not None
    page = client.get("/account").text
    assert f"https://t.me/jobs_bot?start={code.code}" in page
    assert f"/start {code.code}" in page

    profile_id = links.redeem(code.code, datetime.now(UTC))
    assert profile_id is not None
    actions.link_chat(engine, CHAT, profile_id)
    page = client.get("/account").text
    assert "Linked:" in page and "Unlink" in page and "Link another chat" in page

    response = client.post("/account/telegram/unlink")
    assert response.headers["location"].startswith("/account?unlinked=1")
    assert chat_of(engine, alice) is None
    assert "Telegram unlinked" in client.get("/account?unlinked=1").text


def test_telegram_routes_need_a_session(client: TestClient) -> None:
    assert client.post("/account/telegram").headers["location"] == "/login"
    assert client.post("/account/telegram/unlink").headers["location"] == "/login"


# --- the bot's /start ---------------------------------------------------------------


def run_start(engine: Engine, links: TelegramLinks, chat: int, args: list[str]) -> list[str]:
    from telegram.ext import CommandHandler

    from job_finder.telegram.bot import make_application, register_handlers

    app = make_application("123456:TEST")  # building it does not touch the network
    register_handlers(app, engine, Settings(), Scheduler(engine, Settings(), None), links)
    (start,) = [
        h for h in app.handlers[0] if isinstance(h, CommandHandler) and "start" in h.commands
    ]
    answers: list[str] = []

    async def reply_text(text: str, **kw: Any) -> None:
        answers.append(text)

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat, full_name="Someone"),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    asyncio.run(start.callback(update, SimpleNamespace(args=args)))  # type: ignore[arg-type]
    return answers


def test_start_with_a_code_links_the_chat(engine: Engine) -> None:
    alice = add_profile(engine, "alice")
    links = TelegramLinks()
    code = links.new(alice, datetime.now(UTC))

    (answer,) = run_start(engine, links, CHAT, [code.code])
    assert answer.startswith("✅ This chat is now linked") and "Hi, alice!" in answer
    assert chat_of(engine, alice) == CHAT

    (again,) = run_start(engine, links, CHAT, [code.code])  # used up
    assert again.startswith("This link code is wrong or has expired")


def test_start_refuses_a_chat_linked_to_someone_else(engine: Engine) -> None:
    add_profile(engine, "alice", chat=CHAT)
    bob = add_profile(engine, "bob")
    links = TelegramLinks()
    code = links.new(bob, datetime.now(UTC))
    (answer,) = run_start(engine, links, CHAT, [code.code])
    assert answer.startswith("This chat is already linked to another profile")
    assert chat_of(engine, bob) is None


def test_start_from_an_unknown_chat_points_to_the_site(engine: Engine) -> None:
    (answer,) = run_start(engine, TelegramLinks(), CHAT, [])
    assert "<b>Link Telegram</b>" in answer and f"<code>{CHAT}</code>" in answer


# --- profile create -----------------------------------------------------------------


@pytest.fixture
def cli_engine(engine: Engine, db_url: str, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("JOBFINDER_DATABASE_URL", db_url)
    return engine


def test_profile_create(cli_engine: Engine) -> None:
    runner = CliRunner()
    result = runner.invoke(cli_app, ["profile", "create", "alex", "--stdin"], input=PASSWORD)
    assert result.exit_code == 0, result.output
    with session(cli_engine, commit=False) as s:
        db = repo.find_profile(s, "alex")
        assert db is not None and db.telegram_chat_id is None and db.queries == []
        assert auth.verify_password(PASSWORD, db.password_hash)

    again = runner.invoke(cli_app, ["profile", "create", "alex", "--stdin"], input=PASSWORD)
    assert again.exit_code != 0 and "already exists" in again.output


@pytest.mark.parametrize(
    ("name", "password", "message"),
    [("Alex Smith", PASSWORD, "lowercase"), ("alex", "short", "8")],
)
def test_profile_create_refuses_bad_input(
    cli_engine: Engine, name: str, password: str, message: str
) -> None:
    result = CliRunner().invoke(cli_app, ["profile", "create", name, "--stdin"], input=password)
    assert result.exit_code != 0 and message in result.output
    with session(cli_engine, commit=False) as s:
        assert repo.list_profiles(s) == []
