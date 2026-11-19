"""FastAPI application. The `lifespan` starts the Telegram bot and the scheduler in-process."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine, text

from job_finder import __version__
from job_finder.config import Settings, get_settings
from job_finder.db.session import make_engine
from job_finder.llm.client import LLMClient
from job_finder.scheduler import Scheduler
from job_finder.telegram.linking import TelegramLinks
from job_finder.web.cv import Extractor

log = logging.getLogger(__name__)


@asynccontextmanager
async def _services(
    settings: Settings, engine: Engine, scheduler: Scheduler, links: TelegramLinks
) -> AsyncIterator[None]:
    """Bot (if there is a token) + scheduled runs. Without a token, runs happen with no
    digest. With scheduling disabled, neither starts; the site can still run searches."""
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

    scheduler.sender = sender
    if bot_app is not None:
        from job_finder.telegram.bot import register_handlers

        register_handlers(bot_app, engine, settings, scheduler, links)
        await bot_app.initialize()
        links.bot_username = bot_app.bot.username  # known after initialize (getMe)
        await bot_app.start()
        assert bot_app.updater is not None
        await bot_app.updater.start_polling()
        log.info("Telegram bot running")

    tasks = [asyncio.create_task(scheduler.loop(), name="scheduler")]
    if settings.backup_enabled:
        tasks.append(asyncio.create_task(scheduler.backup_loop(), name="backup"))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if bot_app is not None:
            assert bot_app.updater is not None
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()


def create_app(
    settings: Settings | None = None,
    *,
    extract: Extractor | None = None,
    llm: Callable[[], LLMClient] | None = None,
) -> FastAPI:
    """`extract` (the CV reader) and `llm` (the rules learner's client) replace Claude in
    tests."""
    settings = settings or get_settings()
    engine = make_engine(settings.resolved_database_url)
    scheduler = Scheduler(engine, settings, None)
    links = TelegramLinks()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with _services(settings, engine, scheduler, links):
            yield

    app = FastAPI(title="Job Finder", version=__version__, lifespan=lifespan)
    app.state.telegram_links = links

    from job_finder.web.site import mount_site

    mount_site(app, engine, settings, scheduler, extract=extract, llm=llm, links=links)

    @app.get("/health")
    def health() -> dict[str, str]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    return app
