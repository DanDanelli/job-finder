"""Ambiente do Alembic. A URL vem de `alembic_config()` ou das Settings."""

from alembic import context

from job_finder.config import get_settings
from job_finder.db.models import Base
from job_finder.db.session import criar_engine

config = context.config
url = config.get_main_option("sqlalchemy.url") or get_settings().resolved_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # FKs desligadas: ver criar_engine. A integridade é conferida no fim.
    engine = criar_engine(url, foreign_keys=False)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,  # necessário para ALTER TABLE no SQLite
        )
        with context.begin_transaction():
            context.run_migrations()
            if connection.dialect.name == "sqlite":
                violacoes = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
                if violacoes:
                    raise RuntimeError(f"migração deixou FKs inválidas: {violacoes[:5]}")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
