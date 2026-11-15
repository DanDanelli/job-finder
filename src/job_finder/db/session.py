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


def _configurar_sqlite(foreign_keys: bool) -> Any:
    def configurar(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return configurar


def criar_engine(url: str, *, foreign_keys: bool = True) -> Engine:
    """Engine com WAL e busy_timeout no SQLite.

    `foreign_keys=False` só nas migrações: o modo batch do Alembic recria a tabela
    (cria a nova, copia, apaga a antiga), e com as FKs ligadas apagar a antiga dispara
    o ON DELETE CASCADE nas tabelas filhas.
    """
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configurar_sqlite(foreign_keys))
    return engine


@contextmanager
def sessao(engine: Engine, *, commit: bool = True) -> Iterator[Session]:
    """Sessão transacional: commit ao final do bloco, rollback em caso de erro.

    Com `commit=False` tudo é desfeito ao final (usado no dry-run).
    """
    fabrica = sessionmaker(engine, expire_on_commit=False)
    with fabrica() as s:
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


def upgrade(url: str, revisao: str = "head") -> None:
    criar_engine(url).dispose()  # garante que o diretório do SQLite existe
    command.upgrade(alembic_config(url), revisao)
