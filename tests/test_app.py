from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.config import Settings, get_settings
from job_finder.web.app import create_app


def test_settings_default_to_sqlite_in_data_dir(tmp_path: Path) -> None:
    settings = get_settings()
    assert settings.resolved_database_url == f"sqlite:///{tmp_path / 'data' / 'job_finder.db'}"


def test_settings_accept_an_explicit_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBFINDER_DATABASE_URL", "sqlite:///other.db")
    assert Settings().resolved_database_url == "sqlite:///other.db"


def test_cli_db_upgrade_creates_the_database(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli_app, ["db", "upgrade"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "job_finder.db").exists()


def test_health(db_url: str) -> None:
    client = TestClient(create_app(Settings(database_url=db_url)))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
