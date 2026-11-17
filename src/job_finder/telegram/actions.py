"""Bot actions on the database, with no Telegram dependency (testable directly)."""

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
import httpx
from sqlalchemy import Engine, func, select

from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import FeedbackDB, MatchDB, ProfileDB, QuotaUsageDB, RunDB
from job_finder.db.session import session
from job_finder.domain import MatchStatus
from job_finder.i18n import MESSAGES, t
from job_finder.llm.client import CostCapReached, InvalidResponse, LLMClient, is_fatal, make_client
from job_finder.telegram.digest import Button, esc, format_job, job_buttons

log = logging.getLogger(__name__)

ACTIONS = {"s": ("save", MatchStatus.SAVED), "d": ("dismiss", MatchStatus.DISMISSED)}


@dataclass(frozen=True)
class ChatProfile:
    id: int
    name: str
    paused: bool


def profile_for_chat(engine: Engine, chat_id: int) -> ChatProfile | None:
    """Allowlist: only chats linked to a profile are served."""
    with session(engine, commit=False) as s:
        p = repo.profile_for_chat(s, chat_id)
        return ChatProfile(p.id, p.name, p.paused) if p else None


@dataclass(frozen=True)
class FeedbackResult:
    match_id: int
    status: str
    url: str


def record_feedback(
    engine: Engine, chat_id: int, callback_data: str, origin: str = "telegram"
) -> FeedbackResult | None:
    """Applies 👍/👎 from a `fb:<s|d>:<match_id>` button.

    Returns None if the callback is invalid or the match is not this chat's profile's.
    """
    parts = callback_data.split(":")
    if len(parts) != 3 or parts[0] != "fb" or parts[1] not in ACTIONS or not parts[2].isdigit():
        return None
    action, status = ACTIONS[parts[1]]
    with session(engine) as s:
        profile = repo.profile_for_chat(s, chat_id)
        match = s.get(MatchDB, int(parts[2]))
        if profile is None or match is None or match.profile_id != profile.id:
            return None
        match.status = status
        s.add(FeedbackDB(match_id=match.id, action=action, origin=origin))
        return FeedbackResult(match.id, status, match.job.url)


def match_in_buttons(callbacks: list[str]) -> int | None:
    """The match a job message is about, from its buttons' `fb:<s|d>:<id>` data."""
    for data in callbacks:
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "fb" and parts[2].isdigit():
            return int(parts[2])
    return None


def record_note(
    engine: Engine, chat_id: int, match_id: int, text: str, origin: str = "telegram"
) -> str | None:
    """Saves a reply to a job's message as the person's note on it. Returns the note
    ("" when it came out empty, which clears it), or None if the match is not this
    chat's profile's."""
    with session(engine) as s:
        profile = repo.profile_for_chat(s, chat_id)
        match = s.get(MatchDB, match_id)
        if profile is None or match is None or match.profile_id != profile.id:
            return None
        return repo.add_note(s, match, text, origin) or ""


def toggle_pause(engine: Engine, profile_id: int) -> bool:
    with session(engine) as s:
        p = s.get_one(ProfileDB, profile_id)
        p.paused = not p.paused
        return p.paused


def saved_text(engine: Engine, profile_id: int, lang: str, limit: int = 20) -> str:
    with session(engine, commit=False) as s:
        matches = s.scalars(
            select(MatchDB)
            .where(MatchDB.profile_id == profile_id, MatchDB.status == MatchStatus.SAVED)
            .order_by(MatchDB.updated_at.desc())
            .limit(limit)
        ).all()
        if not matches:
            return t(lang, "saved.none")
        lines = [t(lang, "saved.title", n=len(matches))]
        for m in matches:
            company = f" · {esc(m.job.company)}" if m.job.company else ""
            lines.append(
                f'• <a href="{esc(m.job.url)}">{esc(m.job.title)}</a>{company} ({m.score})'
            )
        return "\n".join(lines)


def status_text(
    engine: Engine, profile_id: int, settings: Settings, now: datetime | None = None
) -> str:
    now = now or datetime.now(UTC)
    lang = settings.language
    client = LLMClient(
        engine, None, monthly_cap_usd=settings.monthly_cost_cap_usd, clock=lambda: now
    )
    with session(engine, commit=False) as s:
        profile = s.get_one(ProfileDB, profile_id)
        name, paused = profile.name, profile.paused
        last = s.scalars(select(RunDB).order_by(RunDB.id.desc()).limit(1)).first()
        last_run = (last.started_at, last.status, last.jobs_new) if last else None
        counts = dict(
            s.execute(
                select(MatchDB.status, func.count())
                .where(MatchDB.profile_id == profile_id)
                .group_by(MatchDB.status)
            ).all()
        )
        quotas = s.execute(
            select(QuotaUsageDB.connector, QuotaUsageDB.calls).where(QuotaUsageDB.day == now.date())
        ).all()

    times = ", ".join(h.strftime("%H:%M") for h in settings.run_times)
    lines = [
        f"<b>{esc(name)}</b>" + (t(lang, "status.paused") if paused else ""),
        t(lang, "status.runs", times=times, timezone=esc(settings.timezone)),
    ]
    if last_run:
        started, st, new = last_run
        lines.append(
            t(lang, "status.last_run", when=started.strftime("%d/%m %H:%M UTC"), status=st, new=new)
        )
    lines.append(
        t(
            lang,
            "status.jobs",
            new=counts.get(MatchStatus.NEW, 0),
            saved=counts.get(MatchStatus.SAVED, 0),
            applied=counts.get(MatchStatus.APPLIED, 0),
            dismissed=counts.get(MatchStatus.DISMISSED, 0),
            pending=counts.get(MatchStatus.PENDING, 0),
            skipped=counts.get(MatchStatus.SKIPPED, 0),
        )
    )
    lines.append(
        t(
            lang,
            "status.claude",
            spent=f"{client.spent_this_month():.2f}",
            cap=f"{settings.monthly_cost_cap_usd:.2f}",
        )
    )
    if quotas:
        lines.append(t(lang, "status.quota", quotas=", ".join(f"{c} {n}" for c, n in quotas)))
    return "\n".join(lines)


def render_notice(lang: str, key: str, params: dict[str, object]) -> str:
    """Translates a notice from the ad-hoc evaluation, escaping its parameters."""
    if key == "evaluate.would_discard":
        code = str(params.get("reason"))
        reason_key = f"reason.{code}"
        reason = t(lang, reason_key) if reason_key in MESSAGES["en"] else code
        return t(lang, key, reason=esc(reason))
    return t(lang, key, **{k: esc(str(v)) for k, v in params.items()})


def evaluate_in_chat(
    engine: Engine,
    settings: Settings,
    profile_id: int,
    message: str,
    *,
    client: LLMClient | None = None,
    http: httpx.Client | None = None,
    now: datetime | None = None,
    resolver: Callable[[str], list[str]] | None = None,
) -> tuple[str, list[Button]]:
    """The bot's answer to a job link or text: (HTML text, buttons)."""
    from job_finder import adhoc
    from job_finder.connectors.base import http_client

    lang = settings.language
    if client is None:
        if settings.anthropic_api_key is None:
            return t(lang, "evaluate.no_key"), []
        client = make_client(engine)
    extras = {"resolver": resolver} if resolver else {}
    try:
        with contextlib.nullcontext(http) if http else http_client() as h:
            r = adhoc.evaluate_message(
                engine,
                client,
                profile_id,
                message,
                http=h,
                model=settings.scoring_model,
                language=lang,
                now=now or datetime.now(UTC),
                **extras,
            )
    except adhoc.InvalidInput as e:
        return render_notice(lang, e.notice.key, e.notice.params), []
    except CostCapReached:
        return t(lang, "evaluate.cost_cap"), []
    except (InvalidResponse, anthropic.APIError, ValueError) as e:
        log.warning("ad-hoc evaluation failed: %s", e)
        if is_fatal(e):
            return esc(t(lang, "warn.claude_down", error=f"{type(e).__name__}: {e}")), []
        return t(lang, "evaluate.retry"), []
    text = format_job(r.item, lang)
    if r.notices:
        text = "\n".join(render_notice(lang, n.key, n.params) for n in r.notices) + "\n\n" + text
    return text, job_buttons(r.item.match_id, r.item.url, lang, r.status)
