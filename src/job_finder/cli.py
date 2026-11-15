"""Linha de comando: `job-finder ...`."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
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
from job_finder.db.models import PerfilDB
from job_finder.domain import Vaga

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
    # O httpx loga a URL de cada requisição em INFO, e a API do Telegram leva o token do
    # bot na URL. Esses loggers ficam em WARNING para o token nunca ir para o log.
    for ruidoso in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


@contextmanager
def _erros_do_claude() -> Iterator[None]:
    """Erro da API do Claude vira uma mensagem curta em vez de traceback."""
    import anthropic

    from job_finder.llm.client import LimiteDeCustoAtingido, RespostaInvalida

    try:
        yield
    except anthropic.APIStatusError as e:
        mensagem = e.body.get("error", {}).get("message") if isinstance(e.body, dict) else None
        console.print(f"[red]Claude API ({e.status_code}): {mensagem or e}[/]")
        raise typer.Exit(1) from e
    except (anthropic.APIConnectionError, LimiteDeCustoAtingido, RespostaInvalida) as e:
        console.print(f"[red]Claude: {e}[/]")
        raise typer.Exit(1) from e


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
            existente = repo.buscar_perfil(s, perfil.nome)
            if "dados" not in perfil.model_fields_set and existente is not None:
                # YAML sem `dados`: mantém o que foi extraído do currículo.
                perfil.dados = repo.perfil_de(existente).dados
            repo.salvar_perfil(s, perfil)
            typer.echo(f"Perfil '{perfil.nome}' salvo ({len(perfil.queries)} queries).")


@perfil_app.command("importar")
def perfil_importar(
    curriculo: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="PDF.")],
    perfil: Annotated[str, typer.Option("--perfil", "-p", help="Nome do perfil.")],
) -> None:
    """Lê o currículo com o Claude e preenche os dados e as queries do perfil."""
    import shutil

    from job_finder.domain import Perfil, Query
    from job_finder.llm.client import criar_cliente
    from job_finder.llm.profile_extractor import extrair_perfil

    settings = get_settings()
    engine = _engine()
    destino = settings.data_dir / "curriculos" / f"{perfil}.pdf"
    destino.parent.mkdir(parents=True, exist_ok=True)
    if curriculo.resolve() != destino.resolve():
        shutil.copyfile(curriculo, destino)

    with db_session.sessao(engine) as s:
        db = repo.buscar_perfil(s, perfil) or repo.salvar_perfil(s, Perfil(nome=perfil))
        perfil_id = db.id

    cliente = criar_cliente(engine)
    with _erros_do_claude(), console.status(f"Lendo o currículo com {settings.modelo_perfil}..."):
        extracao = extrair_perfil(
            cliente, destino, modelo=settings.modelo_perfil, perfil_id=perfil_id
        )

    with db_session.sessao(engine) as s:
        db = s.get_one(PerfilDB, perfil_id)
        atual = repo.perfil_de(db)
        atual.dados = extracao.dados
        # Termos novos entram ativos só se o perfil ainda não tinha nenhuma query ativa;
        # senão entram desativados, para o usuário revisar.
        tinha_ativas = any(q.ativa for q in atual.queries)
        conhecidos = {q.termo.casefold() for q in atual.queries}
        novos = [t for t in extracao.termos_busca if t.casefold() not in conhecidos]
        atual.queries += [Query(termo=t, ativa=not tinha_ativas) for t in novos]
        repo.salvar_perfil(s, atual)
        db.curriculo_path = str(destino)

    d = extracao.dados
    console.print(f"[bold]{perfil}[/]: {d.senioridade or '?'}, {d.anos_experiencia or '?'} anos")
    console.print(d.resumo)
    console.print("[bold]Cargos:[/] " + ", ".join(d.cargos))
    console.print("[bold]Skills:[/] " + ", ".join(f"{k.nome} ({k.peso})" for k in d.skills))
    console.print("[bold]Idiomas:[/] " + ", ".join(f"{i.codigo} {i.nivel}" for i in d.idiomas))
    console.print(
        "[bold]Queries novas:[/] "
        + (", ".join(novos) or "nenhuma")
        + ("" if not tinha_ativas or not novos else " [yellow](desativadas, revise)[/]")
    )
    console.print(
        f"Custo no mês: US$ {cliente.gasto_no_mes():.4f}. "
        f"Revise com `job-finder perfil exportar {perfil} > perfil.yaml`, edite e "
        "`job-finder perfil carregar perfil.yaml`."
    )


@perfil_app.command("exportar")
def perfil_exportar(nome: Annotated[str, typer.Argument(help="Nome do perfil.")]) -> None:
    """Imprime o perfil em YAML, no formato que `perfil carregar` lê."""
    from job_finder.perfis import escrever_perfil_yaml

    with db_session.sessao(_engine(), commit=False) as s:
        db = repo.buscar_perfil(s, nome)
        if db is None:
            raise typer.BadParameter(f"perfil '{nome}' não encontrado")
        typer.echo(escrever_perfil_yaml(repo.perfil_de(db)), nl=False)


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
    sem_llm: Annotated[
        bool, typer.Option("--sem-llm", help="Não pontua com o Claude; matches ficam pendentes.")
    ] = False,
) -> None:
    """Executa uma rodada: coleta, normaliza, deduplica, filtra e pontua por perfil."""
    from job_finder.servico import NenhumPerfil, rodada_completa

    settings = get_settings()
    engine = _engine()
    try:
        with _erros_do_claude(), console.status("Rodando..."):
            relatorio = rodada_completa(
                engine,
                settings,
                nomes=perfil or None,
                tipo="sob_demanda",
                dry_run=dry_run,
                pontuar=not sem_llm,
            )
    except NenhumPerfil as e:
        raise typer.BadParameter(str(e)) from e

    resumo = relatorio.rodada
    fontes = ", ".join(f"{f}: {n}" for f, n in resumo.por_fonte.items()) or "nenhuma"
    console.print(
        f"[bold]Coletadas:[/] {resumo.coletadas} ({fontes}) · [bold]novas:[/] {resumo.novas}"
        + (" · [yellow]dry-run, nada gravado[/]" if dry_run else "")
    )
    for rp in relatorio.perfis:
        f = rp.filtros
        descartes = ", ".join(f"{m}: {n}" for m, n in f.descartes.most_common()) or "nenhum"
        console.print(
            f"[bold]{rp.nome}[/]: {len(f.aprovadas)} de {f.avaliadas} passaram nos filtros"
            f" (descartes: {descartes})"
        )
        if rp.scoring:
            console.print(
                f"  {rp.scoring.avaliadas} vagas pontuadas de {rp.scoring.pendentes} pendentes"
            )
            _mostrar_ranking(engine, rp.perfil_id)
        elif dry_run:
            _mostrar_aprovadas(f.aprovadas)
    for aviso in {a for rp in relatorio.perfis for a in relatorio.avisos_de(rp.perfil_id)}:
        console.print(f"[red]⚠ {aviso}[/]")


def _mostrar_aprovadas(vagas: list[Vaga]) -> None:
    tabela = Table()
    for coluna in ("Vaga", "Empresa", "Local", "Salário", "Link"):
        tabela.add_column(coluna, overflow="fold")
    for v in vagas:
        sal = v.salario
        salario = (
            f"{sal.moeda or ''} {sal.minimo:,.0f}-{sal.maximo:,.0f}/{sal.periodo or '?'}"
            if sal and sal.minimo is not None and sal.maximo is not None
            else "-"
        )
        tabela.add_row(v.titulo, v.empresa or "-", v.local_texto or "-", salario, str(v.url))
    console.print(tabela)


def _pontuar(engine: Engine, nomes: list[str] | None, execucao_id: int | None = None) -> None:
    from job_finder.llm.client import criar_cliente
    from job_finder.llm.scorer import pontuar_pendentes

    settings = get_settings()
    cliente = criar_cliente(engine)
    with db_session.sessao(engine, commit=False) as s:
        perfis = [(p.id, p.nome) for p in repo.listar_perfis(s, nomes)]
    if not perfis:
        raise typer.BadParameter("nenhum perfil encontrado; use `job-finder perfil carregar`")

    for perfil_id, nome in perfis:
        with (
            _erros_do_claude(),
            console.status(f"Pontuando vagas de {nome} com {settings.modelo_scoring}..."),
        ):
            r = pontuar_pendentes(
                engine,
                cliente,
                perfil_id,
                modelo=settings.modelo_scoring,
                top_n=settings.prerank_top_n,
                agora=datetime.now(UTC),
                execucao_id=execucao_id,
            )
        console.print(
            f"[bold]{nome}[/]: {r.avaliadas} vagas pontuadas de {r.pendentes} pendentes"
            + (f", {len(r.falhas)} falhas" if r.falhas else "")
        )
        for falha in r.falhas:
            console.print(f"[red]⚠ {falha}[/]")
        if r.interrompido:
            console.print(f"[red]⚠ {r.interrompido}[/]")
            break
        _mostrar_ranking(engine, perfil_id)
    console.print(f"Custo do Claude no mês: US$ {cliente.gasto_no_mes():.4f}")


def _mostrar_ranking(engine: Engine, perfil_id: int, limite: int = 10) -> None:
    from sqlalchemy import select

    from job_finder.db.models import MatchDB
    from job_finder.domain import StatusMatch

    with db_session.sessao(engine, commit=False) as s:
        matches = s.scalars(
            select(MatchDB)
            .where(MatchDB.perfil_id == perfil_id, MatchDB.status == StatusMatch.NOVA)
            .order_by(MatchDB.score.desc())
            .limit(limite)
        ).all()
        if not matches:
            return
        tabela = Table(title="Melhores vagas novas")
        for coluna in ("Score", "Vaga", "Empresa", "Por quê", "Link"):
            tabela.add_column(coluna, overflow="fold")
        for m in matches:
            porque = m.resumo or ""
            if m.dealbreakers:
                porque += " [red]✗ " + "; ".join(m.dealbreakers) + "[/]"
            tabela.add_row(str(m.score), m.vaga.titulo, m.vaga.empresa or "-", porque, m.vaga.url)
        console.print(tabela)


@app.command()
def pontuar(
    perfil: Annotated[
        list[str] | None, typer.Option("--perfil", "-p", help="Só estes perfis.")
    ] = None,
) -> None:
    """Pontua com o Claude as vagas pendentes (sem coletar vagas novas)."""
    if get_settings().anthropic_api_key is None:
        raise typer.BadParameter("ANTHROPIC_API_KEY não configurada (coloque no .env)")
    _pontuar(_engine(), perfil or None)


@app.command()
def digest(
    perfil: Annotated[str, typer.Option("--perfil", "-p", help="Nome do perfil.")],
) -> None:
    """Envia agora, pelo Telegram, o digest das vagas com nota ainda não notificadas."""
    import asyncio

    from job_finder.telegram.bot import EnviadorTelegram, criar_aplicacao
    from job_finder.telegram.digest import enviar_digest

    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise typer.BadParameter("JOBFINDER_TELEGRAM_BOT_TOKEN não configurado (coloque no .env)")
    engine = _engine()
    with db_session.sessao(engine, commit=False) as s:
        db = repo.buscar_perfil(s, perfil)
        if db is None:
            raise typer.BadParameter(f"perfil '{perfil}' não encontrado")
        if db.telegram_chat_id is None:
            raise typer.BadParameter(
                f"perfil '{perfil}' sem telegram_chat_id; mande /start para o bot para descobrir"
            )
        perfil_id = db.id

    async def enviar() -> int:
        bot_app = criar_aplicacao(settings.telegram_bot_token.get_secret_value())  # type: ignore[union-attr]
        async with bot_app.bot as bot:
            return await enviar_digest(
                engine,
                EnviadorTelegram(bot),
                perfil_id,
                score_minimo=settings.digest_score_minimo,
                maximo=settings.digest_max_vagas,
            )

    console.print(f"{asyncio.run(enviar())} vagas enviadas para {perfil}.")


@app.command()
def avaliar(
    entrada: Annotated[str, typer.Argument(help="Link da vaga ou o texto dela.")],
    perfil: Annotated[str, typer.Option("--perfil", "-p", help="Nome do perfil.")],
) -> None:
    """Avalia uma vaga avulsa (o mesmo que mandar o link para o bot)."""
    import re

    from job_finder.telegram import acoes

    settings = get_settings()
    engine = _engine()
    with db_session.sessao(engine, commit=False) as s:
        db = repo.buscar_perfil(s, perfil)
        if db is None:
            raise typer.BadParameter(f"perfil '{perfil}' não encontrado")
        perfil_id = db.id
    texto, botoes = acoes.avaliar_no_chat(engine, settings, perfil_id, entrada)
    console.print(re.sub(r"</?[a-z]+>", "", texto), markup=False, highlight=False)
    for b in botoes:
        if b.url:
            console.print(b.url, markup=False)


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
