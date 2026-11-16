"""Scheduler: runs at the configured times, with the digest at the end of each one.

One run at a time (lock shared with the bot's /search and the site's "Search now"). The
pipeline is synchronous (SQLAlchemy, httpx, Claude SDK) and runs in a thread so it does
not block the bot or the site. While it runs, `current` says what it is doing; when it
ends, `last` keeps each profile's outcome (in memory: a restart forgets it).
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.db.backup import BackupResult
from job_finder.llm.client import LLMClient
from job_finder.service import NoProfiles, Report, full_run
from job_finder.telegram.digest import Sender, send_digest

log = logging.getLogger(__name__)


def next_run(now: datetime, times: Sequence[time], tz: ZoneInfo) -> datetime:
    """Next scheduled time after `now`, in UTC. Follows the zone's daylight saving time."""
    if not times:
        raise ValueError("no run times configured")
    local = now.astimezone(tz)
    candidates = [
        datetime.combine(local.date() + timedelta(days=d), h, tzinfo=tz)
        for d in (0, 1)
        for h in times
    ]
    return min(c for c in candidates if c > local).astimezone(UTC)


@dataclass
class RunState:
    """What the run in progress is doing: an i18n key ("progress.*") and its params."""

    kind: str
    names: tuple[str, ...] | None
    started_at: datetime
    step: str = "progress.start"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunOutcome:
    """How the last run went for one profile."""

    finished_at: datetime
    kind: str
    approved: int = 0  # passed the filters
    scored: int = 0  # scored by Claude in this run
    warnings: tuple[str, ...] = ()
    error: str | None = None


class Scheduler:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        sender: Sender | None,
        *,
        pause_between_messages_s: float = 0.4,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.sender = sender
        self.pause = pause_between_messages_s
        self.lock = asyncio.Lock()
        self.current: RunState | None = None
        self.last: dict[str, RunOutcome] = {}  # by profile name
        self._task: asyncio.Task[Report | None] | None = None

    @property
    def busy(self) -> bool:
        return self.lock.locked() or (self._task is not None and not self._task.done())

    def start(self, names: Sequence[str] | None, kind: str = "on_demand") -> bool:
        """Starts a run in the background (the site's "Search now"). False if one is
        already running. Must be called from the event loop."""
        if self.busy:
            return False
        self._task = asyncio.create_task(self._run_logged(names, kind), name=f"{kind} run")
        return True

    async def _run_logged(self, names: Sequence[str] | None, kind: str) -> Report | None:
        try:
            return await self.run_and_notify(names, kind=kind)
        except Exception as e:
            log.exception("%s run failed", kind)
            for name in names or ():
                self.last[name] = RunOutcome(
                    datetime.now(UTC), kind, error=f"{type(e).__name__}: {e}"
                )
            return None

    def _progress(self, key: str, params: dict[str, Any]) -> None:
        # Called from the run's thread; replacing two attributes is enough here.
        if self.current is not None:
            self.current.step, self.current.params = key, dict(params)

    async def run_and_notify(
        self, names: Sequence[str] | None = None, kind: str = "scheduled"
    ) -> Report | None:
        async with self.lock:
            self.current = RunState(kind, tuple(names) if names else None, datetime.now(UTC))
            try:
                report = await asyncio.to_thread(
                    full_run,
                    self.engine,
                    self.settings,
                    names=names,
                    kind=kind,
                    progress=self._progress,
                )
            except NoProfiles:
                log.info("%s run with no active profiles", kind)
                self.current = None
                return None
            except BaseException:
                self.current = None
                raise
            # The outcome is in place before `current` clears, so a page never sees a
            # finished run with neither.
            finished = datetime.now(UTC)
            for pr in report.profiles:
                self.last[pr.name] = RunOutcome(
                    finished,
                    kind,
                    approved=len(pr.filters.approved),
                    scored=pr.scoring.scored if pr.scoring else 0,
                    warnings=tuple(report.warnings_for(pr.profile_id)),
                )
            self.current = None
            if self.sender is not None:
                for pr in report.profiles:
                    try:
                        await send_digest(
                            self.engine,
                            self.sender,
                            pr.profile_id,
                            min_score=self.settings.digest_min_score,
                            maximum=self.settings.digest_max_jobs,
                            language=self.settings.language,
                            warnings=report.warnings_for(pr.profile_id),
                            pause_s=self.pause,
                        )
                    except Exception:  # one broken chat does not block the other profile's
                        log.exception("failed to send the digest of %s", pr.name)
            return report

    async def learn_due(self, client: LLMClient | None = None) -> list[str]:
        """Asks Claude for rules for each profile that is due (see learner.due) and tells
        the person on Telegram when a proposal waits for review. Returns the names."""
        from sqlalchemy import select

        from job_finder.db.models import ProfileDB
        from job_finder.db.session import session
        from job_finder.i18n import t
        from job_finder.llm import learner
        from job_finder.llm.client import CostCapReached, make_client

        st = self.settings
        now = datetime.now(UTC)
        with session(self.engine, commit=False) as s:
            profiles = s.execute(
                select(ProfileDB.id, ProfileDB.name, ProfileDB.telegram_chat_id).where(
                    ProfileDB.paused.is_(False)
                )
            ).all()
        proposed: list[str] = []
        for profile_id, name, chat_id in profiles:
            state = learner.state(self.engine, profile_id)
            if not learner.due(state, now, st.learning_interval_days, st.learning_min_decisions):
                continue
            if client is None:
                if st.anthropic_api_key is None:
                    return proposed
                client = make_client(self.engine)
            try:
                rules = await asyncio.to_thread(
                    learner.propose,
                    self.engine,
                    client,
                    profile_id,
                    model=st.learning_model,
                    language=st.language,
                    now=now,
                )
            except CostCapReached:
                log.warning("cost cap reached; learning skipped")
                return proposed
            except Exception:
                log.exception("learning failed for %s", name)
                continue
            if rules is None:
                continue
            proposed.append(name)
            if self.sender is not None and chat_id is not None:
                text = t(st.language, "learn.proposed", n=len(rules))
                if st.site_url:
                    text += "\n" + st.site_url.rstrip("/") + "/learned"
                try:
                    await self.sender.send(chat_id, text)
                except Exception:
                    log.exception("failed to tell %s about the proposed rules", name)
        return proposed

    def backup_now(self, now: datetime | None = None) -> BackupResult | None:
        """Copies the database (SQLite only). Synchronous: call it in a thread."""
        from job_finder.db.backup import backup, sqlite_path

        database = sqlite_path(self.settings.resolved_database_url)
        if database is None:
            return None
        today = (now or datetime.now(UTC)).astimezone(ZoneInfo(self.settings.timezone)).date()
        return backup(
            database, self.settings.resolved_backup_dir, today, self.settings.backup_keep_days
        )

    async def backup_loop(self) -> None:
        """A copy on start if today's is missing, then one a day at `backup_time`."""
        from job_finder.db.backup import backups

        tz = ZoneInfo(self.settings.timezone)
        today = datetime.now(UTC).astimezone(tz).date()
        existing = backups(self.settings.resolved_backup_dir)
        if not existing or existing[0][0] < today:
            await self._backup_logged()
        while True:
            upcoming = next_run(datetime.now(UTC), [self.settings.backup_time], tz)
            await asyncio.sleep(max(0.0, (upcoming - datetime.now(UTC)).total_seconds()))
            await self._backup_logged()

    async def _backup_logged(self) -> None:
        try:
            await asyncio.to_thread(self.backup_now)
        except Exception:  # never take down the loop; the error goes to the log
            log.exception("database backup failed")

    async def loop(self) -> None:
        tz = ZoneInfo(self.settings.timezone)
        while True:
            upcoming = next_run(datetime.now(UTC), self.settings.run_times, tz)
            log.info("next scheduled run: %s", upcoming.astimezone(tz).isoformat())
            await asyncio.sleep(max(0.0, (upcoming - datetime.now(UTC)).total_seconds()))
            try:
                await self.run_and_notify(kind="scheduled")
            except Exception:  # never take down the loop; the error goes to the log
                log.exception("scheduled run failed")
            if self.settings.learning_enabled:
                try:
                    await self.learn_due()
                except Exception:
                    log.exception("learning from feedback failed")
