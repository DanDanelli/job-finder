"""Aplicação FastAPI. O `lifespan` sobe o bot do Telegram e o agendador no mesmo processo."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine, text

from job_finder import __version__
from job_finder.config import Settings, get_settings
from job_finder.db.session import criar_engine

log = logging.getLogger(__name__)


@asynccontextmanager
async def _servicos(settings: Settings, engine: Engine) -> AsyncIterator[None]:
    """Bot (se houver token) + agendador. Sem token, as rodadas rodam sem digest."""
    from job_finder.scheduler import Agendador

    if not settings.agendamento_ativo:
        log.info("agendamento desativado")
        yield
        return

    bot_app = None
    enviador = None
    if settings.telegram_bot_token is not None:
        from job_finder.telegram.bot import EnviadorTelegram, criar_aplicacao

        bot_app = criar_aplicacao(settings.telegram_bot_token.get_secret_value())
        enviador = EnviadorTelegram(bot_app.bot)
    else:
        log.warning("sem JOBFINDER_TELEGRAM_BOT_TOKEN: rodadas sem digest")

    agendador = Agendador(engine, settings, enviador)
    if bot_app is not None:
        from job_finder.telegram.bot import registrar_handlers

        registrar_handlers(bot_app, engine, settings, agendador)
        await bot_app.initialize()
        await bot_app.start()
        assert bot_app.updater is not None
        await bot_app.updater.start_polling()
        log.info("bot do Telegram ativo")

    tarefa = asyncio.create_task(agendador.loop(), name="agendador")
    try:
        yield
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa
        if bot_app is not None:
            assert bot_app.updater is not None
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = criar_engine(settings.resolved_database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with _servicos(settings, engine):
            yield

    app = FastAPI(title="Job Finder", version=__version__, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "versao": __version__}

    return app
