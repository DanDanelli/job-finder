"""Linha de comando: `job-finder ...`."""

import typer

from job_finder.config import get_settings
from job_finder.db import session as db_session

app = typer.Typer(help="Job Finder: busca vagas e ranqueia por fit com o currículo.")
db_app = typer.Typer(help="Banco de dados.")
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade(revisao: str = typer.Argument("head", help="Revisão alvo do Alembic.")) -> None:
    """Cria ou atualiza o schema do banco."""
    url = get_settings().resolved_database_url
    db_session.upgrade(url, revisao)
    typer.echo(f"Banco atualizado ({revisao}): {url}")


@app.command()
def serve() -> None:
    """Sobe a aplicação web."""
    import uvicorn

    from job_finder.web.app import create_app

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    app()
