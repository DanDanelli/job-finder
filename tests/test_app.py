from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.config import Settings, get_settings
from job_finder.web.app import create_app


def test_settings_usa_sqlite_no_data_dir(tmp_path: Path) -> None:
    settings = get_settings()
    assert settings.resolved_database_url == f"sqlite:///{tmp_path / 'data' / 'job_finder.db'}"


def test_settings_aceita_database_url_explicita(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBFINDER_DATABASE_URL", "sqlite:///outro.db")
    assert Settings().resolved_database_url == "sqlite:///outro.db"


def test_cli_db_upgrade_cria_o_banco(tmp_path: Path) -> None:
    resultado = CliRunner().invoke(cli_app, ["db", "upgrade"])
    assert resultado.exit_code == 0, resultado.output
    assert (tmp_path / "data" / "job_finder.db").exists()


def test_health(db_url: str) -> None:
    client = TestClient(create_app(Settings(database_url=db_url)))
    resposta = client.get("/health")
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "ok"
