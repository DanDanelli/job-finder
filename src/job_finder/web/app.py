"""FastAPI application. The `lifespan` starts the Telegram bot and the scheduler in-process."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine, text

from job_finder import __version__
from job_finder.config import Settings, get_settings
from job_finder.db.session import make_engine

log = logging.getLogger(__name__)


@asynccontextmanager
async def _services(settings: Settings, engine: Engine) -> AsyncIterator[None]:
    """Bot (if there is a token) + scheduler. Without a token, runs happen with no digest."""
    from job_finder.scheduler import Scheduler

    if not settings.schedule_enabled:
        log.info("scheduling disabled")
        yield
        return

    bot_app = None
    sender = None
    if settings.telegram_bot_token is not None:
        from job_finder.telegram.bot import TelegramSender, make_application

        bot_app = make_application(settings.telegram_bot_token.get_secret_value())
        sender = TelegramSender(bot_app.bot)
    else:
        log.warning("no JOBFINDER_TELEGRAM_BOT_TOKEN: runs without a digest")

    scheduler = Scheduler(engine, settings, sender)
    if bot_app is not None:
        from job_finder.telegram.bot import register_handlers

        register_handlers(bot_app, engine, settings, scheduler)
        await bot_app.initialize()
        await bot_app.start()
        assert bot_app.updater is not None
        await bot_app.updater.start_polling()
        log.info("Telegram bot running")

    task = asyncio.create_task(scheduler.loop(), name="scheduler")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if bot_app is not None:
            assert bot_app.updater is not None
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.resolved_database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with _services(settings, engine):
            yield

    app = FastAPI(title="Job Finder", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    return app
