import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from job_finder.db.models import Base, JobDB, MatchDB, ProfileDB
from job_finder.db.session import session

TABLES = {
    "profiles",
    "jobs",
    "job_sources",
    "matches",
    "feedback",
    "runs",
    "quota_usage",
    "llm_costs",
}


def test_migration_creates_every_table(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) >= TABLES


def test_migrations_match_the_models(engine: Engine) -> None:
    """Fails if someone changed `models.py` without generating a new migration."""
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_sqlite_uses_wal_and_foreign_keys(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def _job(key: str = "acme|cloud architect|br") -> JobDB:
    return JobDB(
        dedup_key=key,
        title="Cloud Architect",
        company="Acme",
        country="BR",
        work_mode="remote",
        url="https://example.com/job/1",
    )


def test_match_is_unique_per_job_and_profile(engine: Engine) -> None:
    with session(engine) as s:
        profile = ProfileDB(name="daniel")
        job = _job()
        s.add_all([profile, job])
        s.flush()
        s.add(MatchDB(job_id=job.id, profile_id=profile.id, status="new"))

    with pytest.raises(IntegrityError), session(engine) as s:
        s.add(MatchDB(job_id=job.id, profile_id=profile.id, status="new"))


def test_dedup_key_is_unique(engine: Engine) -> None:
    with session(engine) as s:
        s.add(_job())
    with pytest.raises(IntegrityError), session(engine) as s:
        s.add(_job())


def test_foreign_keys_are_enforced(engine: Engine) -> None:
    with pytest.raises(IntegrityError), session(engine) as s:
        s.add(MatchDB(job_id=999, profile_id=999, status="new"))
