"""Digest, bot actions and scheduler, with a fake Sender (no Telegram)."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import FeedbackDB, MatchDB
from job_finder.db.session import session
from job_finder.domain import MatchStatus, Profile, RawJob
from job_finder.i18n import t
from job_finder.pipeline.run import execute_run
from job_finder.quota import Quota
from job_finder.scheduler import Scheduler, next_run
from job_finder.service import full_run
from job_finder.telegram import actions
from job_finder.telegram.digest import (
    Button,
    build_digest,
    format_header,
    format_job,
    send_digest,
)
from job_finder.web.app import create_app

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
LISBON = ZoneInfo("Europe/Lisbon")
CHAT_DANIEL = 111
CHAT_PARTNER = 222
NO_NEW_JOBS = "No new jobs since the last digest."


class FakeSender:
    def __init__(self, fail_at: int | None = None) -> None:
        self.messages: list[tuple[int, str, list[Button]]] = []
        self.fail_at = fail_at

    async def send(self, chat_id: int, text: str, buttons: Sequence[Button] | None = None) -> None:
        if self.fail_at is not None and len(self.messages) == self.fail_at:
            raise ConnectionError("telegram down")
        self.messages.append((chat_id, text, list(buttons or [])))


def raw(source_id: str, title: str, **kw: Any) -> RawJob:
    base: dict[str, Any] = {
        "source": "remotive",
        "source_id": source_id,
        "title": title,
        "company": "Acme & Co",
        "location": "Europe",
        "work_mode": "remote",
        "url": f"https://remotive.com/{source_id}",
        "posted_at": NOW - timedelta(days=1),
        "salary_text": "€60k - €80k",
    }
    return RawJob.model_validate(base | kw)


class Source:
    name = "remotive"
    daily_limit = None

    def __init__(self, jobs: list[RawJob]) -> None:
        self.jobs = jobs

    def search(self, queries: object, quota: Quota) -> list[RawJob]:
        return self.jobs


def prepare(engine: Engine, scores: dict[str, int]) -> dict[str, int]:
    """Profiles daniel and partner; one job per title, already scored for daniel."""
    with session(engine) as s:
        repo.save_profile(s, Profile(name="daniel", telegram_chat_id=CHAT_DANIEL))
        repo.save_profile(s, Profile(name="partner", telegram_chat_id=CHAT_PARTNER))
    jobs = [raw(str(i), title) for i, title in enumerate(scores)]
    with session(engine) as s:
        execute_run(s, engine, [Source(jobs)], repo.list_profiles(s, ["daniel"]), NOW)
    ids = {}
    with session(engine) as s:
        for m in s.scalars(select(MatchDB)):
            m.score = scores[m.job.title]
            m.status = MatchStatus.NEW
            m.summary = f"Summary of {m.job.title}"
            m.strengths = ["AWS <strong>"]
            m.gaps = ["Azure"]
            ids[m.job.title] = m.id
    return ids


def profile_id(engine: Engine, name: str) -> int:
    with session(engine) as s:
        p = repo.find_profile(s, name)
        assert p is not None
        return p.id


# --- schedule -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 15/11 is winter in Lisbon (UTC+0).
        (datetime(2026, 11, 15, 7, 0, tzinfo=UTC), datetime(2026, 11, 15, 8, 0, tzinfo=UTC)),
        (datetime(2026, 11, 15, 8, 0, tzinfo=UTC), datetime(2026, 11, 15, 18, 0, tzinfo=UTC)),
        (datetime(2026, 11, 15, 19, 0, tzinfo=UTC), datetime(2026, 11, 16, 8, 0, tzinfo=UTC)),
        # Summer (UTC+1): 08:00 in Lisbon is 07:00 UTC.
        (datetime(2026, 7, 1, 6, 0, tzinfo=UTC), datetime(2026, 7, 1, 7, 0, tzinfo=UTC)),
        # The eve of the clock change (2026-03-29): tomorrow's 08:00 is already UTC+1.
        (datetime(2026, 3, 28, 20, 0, tzinfo=UTC), datetime(2026, 3, 29, 7, 0, tzinfo=UTC)),
    ],
)
def test_next_run(now: datetime, expected: datetime) -> None:
    assert next_run(now, [time(8), time(18)], LISBON) == expected


def test_next_run_in_sao_paulo() -> None:
    # UTC-3 all year: 08:00 in São Paulo is 11:00 UTC.
    now = datetime(2026, 11, 15, 10, 0, tzinfo=UTC)
    expected = datetime(2026, 11, 15, 11, 0, tzinfo=UTC)
    assert next_run(now, [time(8), time(18)], ZoneInfo("America/Sao_Paulo")) == expected


def test_run_times_configurable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBFINDER_RUN_TIMES", '["07:30", "19:00"]')
    assert Settings().run_times == [time(7, 30), time(19)]


# --- digest -------------------------------------------------------------------


def test_digest_selects_sorts_and_cuts(engine: Engine) -> None:
    prepare(engine, {"A": 90, "B": 75, "C": 40, "D": 65})
    d = build_digest(engine, profile_id(engine, "daniel"), min_score=60, maximum=2)
    assert d.chat_id == CHAT_DANIEL
    assert (d.new, d.above_minimum) == (4, 3)
    assert [i.title for i in d.items] == ["A", "B"]  # D (65) waits for the next one
    assert len(d.below_minimum) == 1  # C
    assert format_header(d, "en") == (
        "<b>4 new jobs</b> · 3 with fit ≥ 60\n"
        "Showing the best 2; the rest comes in the next digest."
    )
    assert format_header(d, "pt-BR").startswith("<b>4 vagas novas</b> · 3 com fit ≥ 60")


def test_format_job_escapes_html_and_credits_the_source(engine: Engine) -> None:
    prepare(engine, {"Cloud <Architect>": 88})
    item = build_digest(engine, profile_id(engine, "daniel"), min_score=0, maximum=5).items[0]
    assert format_job(item, "en") == (
        "<b>88</b> · <b>Cloud &lt;Architect&gt;</b>\n"
        "Acme &amp; Co · Remote · Europe · EUR 60k-80k/year\n"
        "✅ AWS &lt;strong&gt;\n"
        "⚠️ Azure\n"
        "<i>Summary of Cloud &lt;Architect&gt;</i>\n"
        "via Remotive"
    )
    assert "Acme &amp; Co · Remoto · Europe · EUR 60k-80k/ano" in format_job(item, "pt-BR")


def test_empty_digest() -> None:
    from job_finder.telegram.digest import Digest

    assert format_header(Digest(chat_id=1, new=0, min_score=60), "en") == NO_NEW_JOBS


def test_send_digest_marks_notified_and_does_not_repeat(engine: Engine) -> None:
    ids = prepare(engine, {"A": 90, "B": 30})
    pid = profile_id(engine, "daniel")
    sender = FakeSender()

    n = asyncio.run(
        send_digest(
            engine,
            sender,
            pid,
            min_score=60,
            maximum=10,
            language="en",
            warnings=["x"],
            pause_s=0,
        )
    )

    assert n == 1
    header, job, warnings = sender.messages
    assert header[0] == CHAT_DANIEL
    assert "2 new jobs" in header[1]
    assert [b.callback for b in job[2][:2]] == [f"fb:s:{ids['A']}", f"fb:d:{ids['A']}"]
    assert job[2][2].url == "https://remotive.com/0"
    assert "Warnings" in warnings[1]

    # A and B (below the minimum) are marked notified; the next digest is empty.
    second = FakeSender()
    asyncio.run(
        send_digest(engine, second, pid, min_score=60, maximum=10, language="en", pause_s=0)
    )
    assert [m[1] for m in second.messages] == [NO_NEW_JOBS]


def test_failed_send_does_not_mark_the_job(engine: Engine) -> None:
    prepare(engine, {"A": 90, "B": 80})
    pid = profile_id(engine, "daniel")
    with pytest.raises(ConnectionError):
        asyncio.run(
            send_digest(
                engine,
                FakeSender(fail_at=2),
                pid,
                min_score=0,
                maximum=10,
                language="en",
                pause_s=0,
            )
        )
    d = build_digest(engine, pid, min_score=0, maximum=10)
    assert [i.title for i in d.items] == ["B"]  # A went out; B retries in the next one


def test_profile_without_chat_sends_nothing(engine: Engine) -> None:
    with session(engine) as s:
        repo.save_profile(s, Profile(name="no_chat"))
    sender = FakeSender()
    pid = profile_id(engine, "no_chat")
    sent = asyncio.run(send_digest(engine, sender, pid, min_score=0, maximum=1, language="en"))
    assert sent == 0
    assert sender.messages == []


# --- bot actions --------------------------------------------------------------


def test_allowlist(engine: Engine) -> None:
    prepare(engine, {"A": 90})
    assert actions.profile_for_chat(engine, CHAT_DANIEL).name == "daniel"  # type: ignore[union-attr]
    assert actions.profile_for_chat(engine, 999) is None


def test_feedback_updates_status_and_is_recorded(engine: Engine) -> None:
    ids = prepare(engine, {"A": 90})
    r = actions.record_feedback(engine, CHAT_DANIEL, f"fb:s:{ids['A']}")
    assert r is not None and r.status == MatchStatus.SAVED
    r = actions.record_feedback(engine, CHAT_DANIEL, f"fb:d:{ids['A']}")  # changed their mind
    assert r is not None and r.status == MatchStatus.DISMISSED
    with session(engine) as s:
        assert s.get_one(MatchDB, ids["A"]).status == MatchStatus.DISMISSED
        assert [f.action for f in s.scalars(select(FeedbackDB).order_by(FeedbackDB.id))] == [
            "save",
            "dismiss",
        ]


@pytest.mark.parametrize("data", ["fb:x:1", "fb:s:abc", "junk", "fb:s:99999"])
def test_invalid_feedback(engine: Engine, data: str) -> None:
    prepare(engine, {"A": 90})
    assert actions.record_feedback(engine, CHAT_DANIEL, data) is None


def test_feedback_from_another_profile_is_rejected(engine: Engine) -> None:
    ids = prepare(engine, {"A": 90})  # daniel's match
    assert actions.record_feedback(engine, CHAT_PARTNER, f"fb:s:{ids['A']}") is None
    assert actions.record_feedback(engine, 999, f"fb:s:{ids['A']}") is None
    with session(engine) as s:
        assert s.get_one(MatchDB, ids["A"]).status == MatchStatus.NEW


def test_saved_and_status(engine: Engine) -> None:
    ids = prepare(engine, {"A & B": 90, "C": 70})
    pid = profile_id(engine, "daniel")
    assert actions.saved_text(engine, pid, "en") == t("en", "saved.none")
    actions.record_feedback(engine, CHAT_DANIEL, f"fb:s:{ids['A & B']}")
    saved = actions.saved_text(engine, pid, "en")
    assert '<a href="https://remotive.com/0">A &amp; B</a>' in saved

    status = actions.status_text(engine, pid, Settings(), now=NOW)
    assert "Runs: 08:00, 18:00 (America/Sao_Paulo)" in status
    assert "1 new, 1 saved, 0 dismissed, 0 pending" in status
    assert "Claude this month: US$ 0.00 of US$ 5.00" in status

    status_pt = actions.status_text(engine, pid, Settings(language="pt-BR"), now=NOW)
    assert "1 novas, 1 salvas, 0 descartadas, 0 pendentes" in status_pt


def test_pause_removes_the_profile_from_scheduled_runs(engine: Engine) -> None:
    prepare(engine, {"A": 90})
    pid = profile_id(engine, "daniel")
    assert actions.toggle_pause(engine, pid) is True
    assert actions.profile_for_chat(engine, CHAT_DANIEL).paused  # type: ignore[union-attr]

    settings = Settings(connectors=[])
    scheduled = full_run(engine, settings, kind="scheduled", now=NOW)
    assert [p.name for p in scheduled.profiles] == ["partner"]
    # On demand (/search) the paused profile still runs.
    manual = full_run(engine, settings, names=["daniel"], now=NOW)
    assert [p.name for p in manual.profiles] == ["daniel"]

    assert actions.toggle_pause(engine, pid) is False


# --- scheduler ----------------------------------------------------------------


def test_scheduler_runs_and_sends_each_profiles_digest(engine: Engine) -> None:
    prepare(engine, {"A": 90})
    sender = FakeSender()
    scheduler = Scheduler(engine, Settings(connectors=[]), sender, pause_between_messages_s=0)

    report = asyncio.run(scheduler.run_and_notify())

    assert report is not None
    by_chat: dict[int, list[str]] = {}
    for chat, text, _ in sender.messages:
        by_chat.setdefault(chat, []).append(text)
    assert any("<b>A</b>" in text for text in by_chat[CHAT_DANIEL])
    assert by_chat[CHAT_PARTNER][0] == NO_NEW_JOBS
    # Without a Claude key, the warning goes at the end for both.
    assert all("ANTHROPIC_API_KEY" in texts[-1] for texts in by_chat.values())


def test_app_starts_and_stops_the_scheduler(db_url: str) -> None:
    from job_finder.db.session import upgrade

    upgrade(db_url)
    app = create_app(Settings(database_url=db_url, schedule_enabled=True))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_bot_registers_the_commands_and_their_aliases(engine: Engine) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from job_finder.telegram.bot import keyboard, make_application, register_handlers

    app = make_application("123456:TEST")  # building it does not touch the network
    register_handlers(app, engine, Settings(), Scheduler(engine, Settings(), None))
    handlers = app.handlers[0]
    commands = {c for h in handlers if isinstance(h, CommandHandler) for c in h.commands}
    assert commands == {
        "start",
        "search",
        "buscar",
        "saved",
        "salvas",
        "pause",
        "pausar",
        "status",
        "evaluate",
        "avaliar",
    }
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)

    markup = keyboard([Button("👍", callback="fb:s:1"), Button("🔗", url="https://x.example")])
    row = markup.inline_keyboard[0]
    assert (row[0].callback_data, row[1].url) == ("fb:s:1", "https://x.example")


def test_log_does_not_expose_the_bot_token(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from typer.testing import CliRunner

    from job_finder.cli import app as cli_app

    CliRunner().invoke(cli_app, ["profile", "list"])  # runs the callback that sets up logging
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("POST https://api.telegram.org/bot123:SECRET/getMe")
    assert "SECRET" not in caplog.text


def test_long_country_lists_are_shortened() -> None:
    from job_finder.telegram.digest import short_places

    many = "Anguilla, Antigua and Barbuda, Argentina, Aruba, Brazil, Chile"
    assert short_places(many) == "Brazil, Anguilla, Antigua and Barbuda +3"
    assert (
        short_places("Argentina, Brazil, Colombia, Mexico") == "Argentina, Brazil, Colombia, Mexico"
    )
    assert short_places("São Paulo, SP, BR") == "São Paulo, SP, BR"
    assert short_places(None) is None


def test_adzuna_credit_links_to_adzuna() -> None:
    from job_finder.telegram.digest import DigestItem, format_job

    item = DigestItem(
        match_id=1,
        score=70,
        title="Brand Manager",
        company=None,
        work_mode="onsite",
        location_text=None,
        salary_range=None,
        salary_period=None,
        strengths=[],
        gaps=[],
        dealbreakers=[],
        summary=None,
        url="https://www.adzuna.com.br/land/ad/1",
        sources=["Adzuna", "Remotive"],
        source_links={"Adzuna": "https://www.adzuna.com.br"},
    )
    assert format_job(item, "en").endswith(
        'via <a href="https://www.adzuna.com.br">Adzuna</a>, Remotive'
    )
