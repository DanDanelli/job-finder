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
    engine = criar_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=True,  # necessário para ALTER TABLE no SQLite
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
