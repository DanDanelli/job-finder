"""Engine, sessions and migrations."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _configure_sqlite(foreign_keys: bool) -> Any:
    def configure(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return configure


def make_engine(url: str, *, foreign_keys: bool = True) -> Engine:
    """Engine with WAL and busy_timeout on SQLite.

    `foreign_keys=False` is for migrations only: Alembic's batch mode recreates the table
    (create the new one, copy, drop the old one), and with FKs on, dropping the old one
    fires ON DELETE CASCADE on the child tables.
    """
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite(foreign_keys))
    return engine


@contextmanager
def session(engine: Engine, *, commit: bool = True) -> Iterator[Session]:
    """Transactional session: commits at the end of the block, rolls back on error.

    With `commit=False` everything is undone at the end (used by dry runs).
    """
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        s.begin()
        try:
            yield s
        except BaseException:
            s.rollback()
            raise
        if commit:
            s.commit()
        else:
            s.rollback()


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    make_engine(url).dispose()  # makes sure the SQLite directory exists
    command.upgrade(alembic_config(url), revision)
