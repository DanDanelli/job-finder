"""Engine, sessões e migrações."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _configurar_sqlite(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def criar_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configurar_sqlite)
    return engine


@contextmanager
def sessao(engine: Engine) -> Iterator[Session]:
    """Sessão com commit ao final do bloco e rollback em caso de erro."""
    fabrica = sessionmaker(engine, expire_on_commit=False)
    with fabrica() as s, s.begin():
        yield s


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revisao: str = "head") -> None:
    criar_engine(url).dispose()  # garante que o diretório do SQLite existe
    command.upgrade(alembic_config(url), revisao)
