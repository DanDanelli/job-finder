"""Daily SQLite backup with retention."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from job_finder.db import repo
from job_finder.db.backup import BackupError, backup, backups
from job_finder.db.session import session
from job_finder.domain import Profile


def _db_file(db_url: str) -> Path:
    return Path(db_url.removeprefix("sqlite:///"))


def test_backup_copies_a_live_database_and_keeps_n_days(
    engine: Engine, db_url: str, tmp_path: Path
) -> None:
    with session(engine) as s:
        repo.save_profile(s, Profile(name="alice"))
    target = tmp_path / "other-disk"
    day = date(2026, 11, 1)
    for i in range(9):
        backup(_db_file(db_url), target, day + timedelta(days=i), keep=7)
    kept = backups(target)
    assert [d for d, _ in kept] == [day + timedelta(days=i) for i in range(8, 1, -1)]
    newest = kept[0][1]
    assert oct(newest.stat().st_mode & 0o777) == "0o600"
    # the copy is a working database with the data, even though the source is in WAL mode
    with sqlite3.connect(newest) as c:
        assert c.execute("select name from profiles").fetchall() == [("alice",)]
    assert not list(target.glob("*.partial"))


def test_same_day_backup_replaces_that_days_copy(
    engine: Engine, db_url: str, tmp_path: Path
) -> None:
    day = date(2026, 11, 5)
    backup(_db_file(db_url), tmp_path, day, keep=7)
    with session(engine) as s:
        repo.save_profile(s, Profile(name="later"))
    backup(_db_file(db_url), tmp_path, day, keep=7)
    assert len(backups(tmp_path)) == 1
    with sqlite3.connect(backups(tmp_path)[0][1]) as c:
        assert ("later",) in c.execute("select name from profiles").fetchall()


def test_other_files_in_the_folder_are_left_alone(
    engine: Engine, db_url: str, tmp_path: Path
) -> None:
    (tmp_path / "job_finder.before-0002.db").write_text("manual copy")
    for i in range(3):
        backup(_db_file(db_url), tmp_path, date(2026, 11, 1) + timedelta(days=i), keep=1)
    assert (tmp_path / "job_finder.before-0002.db").exists()
    assert len(backups(tmp_path)) == 1


def test_missing_database(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        backup(tmp_path / "nope.db", tmp_path / "b", date(2026, 11, 1), keep=7)


def test_cli_backup(engine: Engine, db_url: str, tmp_path: Path) -> None:
    from job_finder.cli import app

    result = CliRunner().invoke(
        app,
        ["db", "backup"],
        env={"JOBFINDER_DATABASE_URL": db_url, "JOBFINDER_BACKUP_DIR": str(tmp_path / "bk")},
    )
    assert result.exit_code == 0, result.output
    assert len(backups(tmp_path / "bk")) == 1


def test_backup_loop_makes_a_copy_on_start(engine: Engine, db_url: str, tmp_path: Path) -> None:
    import asyncio
    import contextlib

    async def run() -> None:

        from job_finder.config import Settings
        from job_finder.scheduler import Scheduler

        settings = Settings(database_url=db_url, backup_dir=tmp_path / "bk")
        task = asyncio.create_task(Scheduler(engine, settings, None).backup_loop())
        for _ in range(100):
            if backups(tmp_path / "bk"):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert len(backups(tmp_path / "bk")) == 1

    asyncio.run(run())
