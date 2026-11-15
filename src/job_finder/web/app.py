"""Aplicação FastAPI. Nos próximos marcos o `lifespan` também sobe o bot e o scheduler."""

from fastapi import FastAPI
from sqlalchemy import text

from job_finder import __version__
from job_finder.config import Settings, get_settings
from job_finder.db.session import criar_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = criar_engine(settings.resolved_database_url)
    app = FastAPI(title="Job Finder", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "versao": __version__}

    return app
