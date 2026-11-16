"""Daily copy of the SQLite database, keeping the last N days.

Uses SQLite's online backup API, so it is safe while the app writes (WAL). Each copy is
checked (`PRAGMA integrity_check`) before it replaces the day's file. Put `backup_dir`
on another disk than the database: a copy on the same disk does not survive the disk.
"""

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

PREFIX = "job_finder-"
_NAME = re.compile(rf"^{PREFIX}(\d{{4}}-\d{{2}}-\d{{2}})\.db$")


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int
    removed: list[Path]


def sqlite_path(url: str) -> Path | None:
    """The database file of a SQLite URL; None for other databases."""
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///"))
    return None


def backups(directory: Path) -> list[tuple[date, Path]]:
    """Existing copies, newest first."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        m = _NAME.match(path.name)
        if m:
            found.append((date.fromisoformat(m.group(1)), path))
    return sorted(found, reverse=True)


def backup(database: Path, directory: Path, today: date, keep: int) -> BackupResult:
    """Copies `database` to `directory/job_finder-<today>.db` (replacing that day's
    copy) and deletes copies beyond the `keep` newest days."""
    if not database.is_file():
        raise BackupError(f"database not found: {database}")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{PREFIX}{today.isoformat()}.db"
    partial = target.with_suffix(".db.partial")
    partial.unlink(missing_ok=True)

    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(partial)
        try:
            source.backup(copy)
            status = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
    finally:
        source.close()
    if status != "ok":
        partial.unlink(missing_ok=True)
        raise BackupError(f"the copy failed the integrity check: {status}")
    os.chmod(partial, 0o600)  # holds CV data and feedback
    partial.replace(target)

    removed = []
    for _, path in backups(directory)[keep:]:
        path.unlink(missing_ok=True)
        removed.append(path)
    size = target.stat().st_size
    log.info("backup %s (%d KB); removed %d old", target.name, size // 1024, len(removed))
    return BackupResult(target, size, removed)
