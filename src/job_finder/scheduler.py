"""Scheduler: runs at the configured times, with the digest at the end of each one.

One run at a time (lock shared with the bot's /search). The pipeline is synchronous
(SQLAlchemy, httpx, Claude SDK) and runs in a thread so it does not block the bot.
"""

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from job_finder.config import Settings
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

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    async def run_and_notify(
        self, names: Sequence[str] | None = None, kind: str = "scheduled"
    ) -> Report | None:
        async with self.lock:
            try:
                report = await asyncio.to_thread(
                    full_run, self.engine, self.settings, names=names, kind=kind
                )
            except NoProfiles:
                log.info("%s run with no active profiles", kind)
                return None
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
