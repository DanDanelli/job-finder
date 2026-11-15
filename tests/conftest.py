import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from job_finder.config import Settings, get_settings
from job_finder.db.session import make_engine, upgrade


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No test reads the real `.env` or writes outside tmp_path."""
    for var in list(os.environ):
        if var.startswith(("JOBFINDER_", "ANTHROPIC_")):
            monkeypatch.delenv(var)
    monkeypatch.setenv("JOBFINDER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    upgrade(db_url)
    eng = make_engine(db_url)
    yield eng
    eng.dispose()
