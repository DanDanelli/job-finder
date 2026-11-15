"""Rodada completa: coleta, filtros e scoring. Usada pela CLI, pelo agendador e pelo bot."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base
from job_finder.connectors.base import Conector
from job_finder.db import repo
from job_finder.db.session import sessao
from job_finder.llm.client import ClienteLLM, criar_cliente, erro_fatal
from job_finder.llm.scorer import ResumoScoring, pontuar_pendentes
from job_finder.pipeline.run import ResumoPerfil, ResumoRodada, executar_rodada

log = logging.getLogger(__name__)


class NenhumPerfil(Exception):
    pass


@dataclass
class RelatorioPerfil:
    perfil_id: int
    nome: str
    filtros: ResumoPerfil
    scoring: ResumoScoring | None = None


@dataclass
class Relatorio:
    rodada: ResumoRodada
    perfis: list[RelatorioPerfil] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)  # valem para todos os perfis

    def avisos_de(self, perfil_id: int) -> list[str]:
        avisos = list(self.avisos)
        for p in self.perfis:
            if p.perfil_id == perfil_id and p.scoring:
                if p.scoring.falhas:
                    avisos.append(
                        f"{len(p.scoring.falhas)} vagas sem nota (nova tentativa na próxima rodada)"
                    )
                if p.scoring.interrompido:
                    avisos.append(p.scoring.interrompido)
        return avisos


def rodada_completa(
    engine: Engine,
    settings: Settings,
    *,
    nomes: Sequence[str] | None = None,
    tipo: str = "sob_demanda",
    dry_run: bool = False,
    pontuar: bool = True,
    conectores: Sequence[Conector] | None = None,
    cliente: ClienteLLM | None = None,
    agora: datetime | None = None,
) -> Relatorio:
    """Roda o pipeline. Rodadas agendadas pulam perfis pausados.

    Erros de fonte e do Claude viram avisos no relatório; só falta de perfil levanta.
    """
    agora = agora or datetime.now(UTC)
    with base.cliente_http() as http, sessao(engine, commit=not dry_run) as s:
        perfis = repo.listar_perfis(
            s, list(nomes) if nomes else None, incluir_pausados=tipo != "agendada"
        )
        if not perfis:
            raise NenhumPerfil("nenhum perfil encontrado; use `job-finder perfil carregar`")
        ids = {p.nome: p.id for p in perfis}
        if conectores is None:
            conectores = [base.criar(nome, http) for nome in settings.conectores]
        rodada = executar_rodada(
            s, engine, conectores, perfis, agora=agora, tipo="dry_run" if dry_run else tipo
        )

    relatorio = Relatorio(
        rodada=rodada,
        perfis=[RelatorioPerfil(ids[rp.nome], rp.nome, rp) for rp in rodada.perfis],
        avisos=list(rodada.erros),
    )
    if dry_run or not pontuar:
        return relatorio
    if cliente is None:
        if settings.anthropic_api_key is None:
            relatorio.avisos.append("sem ANTHROPIC_API_KEY: vagas ficam sem nota")
            return relatorio
        cliente = criar_cliente(engine)

    for rp in relatorio.perfis:
        try:
            rp.scoring = pontuar_pendentes(
                engine,
                cliente,
                rp.perfil_id,
                modelo=settings.modelo_scoring,
                top_n=settings.prerank_top_n,
                agora=agora,
                execucao_id=rodada.execucao_id,
            )
        except Exception as e:
            if not erro_fatal(e):
                raise
            log.error("scoring interrompido: %s", e)
            relatorio.avisos.append(f"Claude indisponível: {type(e).__name__}: {e}")
            break
        if rp.scoring.interrompido:
            break
    return relatorio
