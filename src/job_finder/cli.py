"""Linha de comando: `job-finder ...`."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine

from job_finder.config import get_settings
from job_finder.db import repo
from job_finder.db import session as db_session

app = typer.Typer(help="Job Finder: busca vagas e ranqueia por fit com o currículo.")
db_app = typer.Typer(help="Banco de dados.")
perfil_app = typer.Typer(help="Perfis de busca.")
app.add_typer(db_app, name="db")
app.add_typer(perfil_app, name="perfil")
console = Console()


@app.callback()
def _configurar() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _engine() -> Engine:
    return db_session.criar_engine(get_settings().resolved_database_url)


@db_app.command("upgrade")
def db_upgrade(
    revisao: Annotated[str, typer.Argument(help="Revisão alvo do Alembic.")] = "head",
) -> None:
    """Cria ou atualiza o schema do banco."""
    url = get_settings().resolved_database_url
    db_session.upgrade(url, revisao)
    typer.echo(f"Banco atualizado ({revisao}): {url}")


@perfil_app.command("carregar")
def perfil_carregar(
    arquivos: Annotated[list[Path], typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Cria ou atualiza perfis a partir de arquivos YAML."""
    from job_finder.perfis import ler_perfil_yaml

    with db_session.sessao(_engine()) as s:
        for arquivo in arquivos:
            perfil = ler_perfil_yaml(arquivo)
            repo.salvar_perfil(s, perfil)
            typer.echo(f"Perfil '{perfil.nome}' salvo ({len(perfil.queries)} queries).")


@perfil_app.command("listar")
def perfil_listar() -> None:
    """Lista os perfis cadastrados."""
    with db_session.sessao(_engine()) as s:
        for p in repo.listar_perfis(s):
            perfil = repo.perfil_de(p)
            termos = ", ".join(q.termo for q in perfil.queries if q.ativa) or "-"
            typer.echo(f"{perfil.nome}: {termos}")


@app.command()
def run(
    perfil: Annotated[
        list[str] | None, typer.Option("--perfil", "-p", help="Só estes perfis.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Não grava nada no banco.")] = False,
) -> None:
    """Executa uma rodada: coleta, normaliza, deduplica e filtra por perfil."""
    from job_finder.connectors import base
    from job_finder.pipeline.run import executar_rodada

    settings = get_settings()
    engine = _engine()
    with base.cliente_http() as http, db_session.sessao(engine, commit=not dry_run) as s:
        perfis = repo.listar_perfis(s, perfil or None)
        if not perfis:
            raise typer.BadParameter("nenhum perfil encontrado; use `job-finder perfil carregar`")
        conectores = [base.criar(nome, http) for nome in settings.conectores]
        resumo = executar_rodada(
            s,
            engine,
            conectores,
            perfis,
            agora=datetime.now(UTC),
            tipo="dry_run" if dry_run else "sob_demanda",
        )

    fontes = ", ".join(f"{f}: {n}" for f, n in resumo.por_fonte.items()) or "nenhuma"
    console.print(
        f"[bold]Coletadas:[/] {resumo.coletadas} ({fontes}) · [bold]novas:[/] {resumo.novas}"
        + (" · [yellow]dry-run, nada gravado[/]" if dry_run else "")
    )
    for erro in resumo.erros:
        console.print(f"[red]⚠ {erro}[/]")
    for rp in resumo.perfis:
        descartes = ", ".join(f"{m}: {n}" for m, n in rp.descartes.most_common()) or "nenhum"
        tabela = Table(
            title=f"{rp.nome}: {len(rp.aprovadas)} de {rp.avaliadas} passaram nos filtros",
            caption=f"Descartes: {descartes}",
        )
        for coluna in ("Vaga", "Empresa", "Local", "Salário", "Link"):
            tabela.add_column(coluna, overflow="fold")
        for v in rp.aprovadas:
            sal = v.salario
            salario = (
                f"{sal.moeda or ''} {sal.minimo:,.0f}-{sal.maximo:,.0f}/{sal.periodo or '?'}"
                if sal and sal.minimo is not None and sal.maximo is not None
                else "-"
            )
            tabela.add_row(v.titulo, v.empresa or "-", v.local_texto or "-", salario, str(v.url))
        console.print(tabela)


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
