"""Telegram digest: picks a profile's new jobs, formats them and sends them.

Sending goes through a `Sender` (the real bot or a fake in tests). A job is marked as
notified only after its message goes out, so a network failure does not lose jobs.
"""

import asyncio
import html
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Engine, select

from job_finder.db.models import JobSourceDB, MatchDB, ProfileDB
from job_finder.db.session import session
from job_finder.domain import MatchStatus
from job_finder.i18n import t

log = logging.getLogger(__name__)

# Display names of the sources. Remotive's terms ask to credit it as the source.
SOURCE_NAMES = {"remotive": "Remotive"}
WORK_MODES = {"remote", "hybrid", "onsite"}
PERIODS = {"year", "month", "hour"}


@dataclass(frozen=True)
class Button:
    text: str
    callback: str | None = None
    url: str | None = None


class Sender(Protocol):
    async def send(
        self, chat_id: int, text: str, buttons: Sequence[Button] | None = None
    ) -> None: ...


@dataclass
class DigestItem:
    match_id: int
    score: int
    title: str
    company: str | None
    work_mode: str
    location_text: str | None
    salary_range: str | None  # e.g. "BRL 20k-25k"
    salary_period: str | None
    strengths: list[str]
    gaps: list[str]
    dealbreakers: list[str]
    summary: str | None
    url: str
    sources: list[str]


@dataclass
class Digest:
    chat_id: int | None
    new: int
    min_score: int
    items: list[DigestItem] = field(default_factory=list)
    below_minimum: list[int] = field(default_factory=list)  # ids: mark without sending
    above_minimum: int = 0


def _salary_range(m: MatchDB) -> str | None:
    j = m.job
    if j.salary_min is None or j.salary_max is None:
        return None

    def k(x: float) -> str:
        return f"{x / 1000:.0f}k" if x >= 10_000 else f"{x:,.0f}"

    span = (
        k(j.salary_min) if j.salary_min == j.salary_max else f"{k(j.salary_min)}-{k(j.salary_max)}"
    )
    return f"{j.salary_currency or ''} {span}".strip()


def _source_name(source: JobSourceDB) -> str | None:
    if source.source == "manual":
        return None  # ad-hoc evaluation: the person knows where it came from
    if source.source == "jsearch":
        # The origin site (LinkedIn, Indeed...) matters more than the aggregator.
        publisher = (source.raw_data or {}).get("publisher")
        return f"{publisher} (Google for Jobs)" if publisher else "Google for Jobs"
    return SOURCE_NAMES.get(source.source, source.source)


def digest_item(m: MatchDB) -> DigestItem:
    return DigestItem(
        match_id=m.id,
        score=m.score or 0,
        title=m.job.title,
        company=m.job.company,
        work_mode=m.job.work_mode,
        location_text=m.job.location_text,
        salary_range=_salary_range(m),
        salary_period=m.job.salary_period,
        strengths=list(m.strengths or []),
        gaps=list(m.gaps or []),
        dealbreakers=list(m.dealbreakers or []),
        summary=m.summary,
        url=m.job.url,
        sources=sorted({n for s in m.job.sources if (n := _source_name(s))}),
    )


def build_digest(engine: Engine, profile_id: int, *, min_score: int, maximum: int) -> Digest:
    """Scored jobs not yet notified: the best ones above the minimum, up to `maximum`.

    Jobs beyond the maximum wait for the next digest; the ones below the minimum are only
    counted in the header and marked as notified.
    """
    with session(engine, commit=False) as s:
        profile = s.get_one(ProfileDB, profile_id)
        matches = list(
            s.scalars(
                select(MatchDB)
                .where(
                    MatchDB.profile_id == profile_id,
                    MatchDB.status == MatchStatus.NEW,
                    MatchDB.notified_at.is_(None),
                )
                .order_by(MatchDB.score.desc(), MatchDB.id)
            )
        )
        above = [m for m in matches if (m.score or 0) >= min_score]
        return Digest(
            chat_id=profile.telegram_chat_id,
            new=len(matches),
            min_score=min_score,
            items=[digest_item(m) for m in above[:maximum]],
            below_minimum=[m.id for m in matches if (m.score or 0) < min_score],
            above_minimum=len(above),
        )


# --- formatting (Telegram HTML) -----------------------------------------------


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def format_header(d: Digest, lang: str) -> str:
    if d.new == 0:
        return t(lang, "digest.none")
    text = t(lang, "digest.header", new=d.new, above=d.above_minimum, min_score=d.min_score)
    if d.above_minimum > len(d.items):
        text += "\n" + t(lang, "digest.more", shown=len(d.items))
    return text


def _location(item: DigestItem, lang: str) -> str:
    mode = t(lang, f"work_mode.{item.work_mode}") if item.work_mode in WORK_MODES else None
    parts = [p for p in (mode, item.location_text) if p]
    return " · ".join(parts) or t(lang, "location.unknown")


def _salary(item: DigestItem, lang: str) -> str | None:
    if item.salary_range is None:
        return None
    if item.salary_period in PERIODS:
        return f"{item.salary_range}/{t(lang, f'period.{item.salary_period}')}"
    return item.salary_range


def format_job(item: DigestItem, lang: str) -> str:
    lines = [f"<b>{item.score}</b> · <b>{esc(item.title)}</b>"]
    details = (item.company, _location(item, lang), _salary(item, lang))
    detail = " · ".join(esc(p) for p in details if p)
    if detail:
        lines.append(detail)
    if item.strengths:
        lines.append("✅ " + "; ".join(esc(s) for s in item.strengths))
    if item.gaps:
        lines.append("⚠️ " + "; ".join(esc(g) for g in item.gaps))
    if item.dealbreakers:
        lines.append("⛔ " + "; ".join(esc(d) for d in item.dealbreakers))
    if item.summary:
        lines.append(f"<i>{esc(item.summary)}</i>")
    if item.sources:
        lines.append(f"via {esc(', '.join(item.sources))}")
    return "\n".join(lines)


def job_buttons(match_id: int, url: str, lang: str, status: str | None = None) -> list[Button]:
    save = t(lang, "button.saved" if status == MatchStatus.SAVED else "button.save")
    dismiss = t(lang, "button.dismissed" if status == MatchStatus.DISMISSED else "button.dismiss")
    return [
        Button(save, callback=f"fb:s:{match_id}"),
        Button(dismiss, callback=f"fb:d:{match_id}"),
        Button(t(lang, "button.open"), url=url),
    ]


def format_warnings(warnings: Sequence[str], lang: str) -> str:
    return t(lang, "digest.warnings") + "\n" + "\n".join(f"• {esc(w)}" for w in warnings)


# --- sending ------------------------------------------------------------------


def mark_notified(engine: Engine, ids: Sequence[int], when: datetime) -> None:
    if not ids:
        return
    with session(engine) as s:
        for m in s.scalars(select(MatchDB).where(MatchDB.id.in_(ids))):
            m.notified_at = when


async def send_digest(
    engine: Engine,
    sender: Sender,
    profile_id: int,
    *,
    min_score: int,
    maximum: int,
    language: str,
    warnings: Sequence[str] = (),
    pause_s: float = 0.4,  # Telegram allows ~1 message/s per chat
) -> int:
    """Sends the profile's digest. Returns how many jobs were sent."""
    d = await asyncio.to_thread(
        build_digest, engine, profile_id, min_score=min_score, maximum=maximum
    )
    if d.chat_id is None:
        log.info("profile %s has no telegram_chat_id; digest not sent", profile_id)
        return 0

    await sender.send(d.chat_id, format_header(d, language))
    sent = 0
    for item in d.items:
        await asyncio.sleep(pause_s)
        await sender.send(
            d.chat_id,
            format_job(item, language),
            job_buttons(item.match_id, item.url, language),
        )
        await asyncio.to_thread(mark_notified, engine, [item.match_id], datetime.now(UTC))
        sent += 1
    await asyncio.to_thread(mark_notified, engine, d.below_minimum, datetime.now(UTC))
    if warnings:
        await sender.send(d.chat_id, format_warnings(warnings, language))
    return sent
