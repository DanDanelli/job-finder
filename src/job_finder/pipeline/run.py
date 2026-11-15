"""Uma rodada do pipeline: coletar → normalizar → deduplicar → salvar → filtrar por perfil.

A coleta é feita uma vez para todos os perfis (economiza quota); os filtros e,
a partir do M2, o scoring são aplicados por perfil.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from job_finder.connectors.base import Conector
from job_finder.db import repo
from job_finder.db.models import ExecucaoDB, MatchDB, PerfilDB
from job_finder.domain import Query, StatusMatch, Vaga, VagaBruta
from job_finder.pipeline.dedup import chave_dedup
from job_finder.pipeline.filters import motivo_descarte
from job_finder.pipeline.normalize import normalizar
from job_finder.quota import Quota, QuotaEsgotada

log = logging.getLogger(__name__)


@dataclass
class ResumoPerfil:
    nome: str
    avaliadas: int = 0
    aprovadas: list[Vaga] = field(default_factory=list)
    descartes: Counter[str] = field(default_factory=Counter)


@dataclass
class ResumoRodada:
    execucao_id: int
    coletadas: int = 0
    novas: int = 0
    por_fonte: Counter[str] = field(default_factory=Counter)
    perfis: list[ResumoPerfil] = field(default_factory=list)
    erros: list[str] = field(default_factory=list)


def _queries(perfis: Sequence[PerfilDB]) -> list[Query]:
    """União das queries ativas de todos os perfis, sem repetição."""
    vistas: dict[tuple[str, tuple[str, ...]], Query] = {}
    for p in perfis:
        for q in repo.perfil_de(p).queries:
            if q.ativa:
                vistas.setdefault((q.termo.casefold(), tuple(sorted(q.paises))), q)
    return list(vistas.values())


def coletar(
    engine: Engine, conectores: Sequence[Conector], queries: list[Query], agora: datetime
) -> tuple[list[VagaBruta], list[str]]:
    brutas: list[VagaBruta] = []
    erros: list[str] = []
    for conector in conectores:
        quota = Quota(engine, conector.nome, conector.limite_diario, agora.date())
        try:
            resultado = conector.buscar(queries, quota)
        except QuotaEsgotada as e:
            erros.append(str(e))
            continue
        except Exception as e:  # uma fonte com problema não derruba as outras
            log.exception("conector %s falhou", conector.nome)
            erros.append(f"{conector.nome}: {type(e).__name__}: {e}")
            continue
        log.info("%s: %d vagas", conector.nome, len(resultado))
        brutas.extend(resultado)
    return brutas, erros


def executar_rodada(
    s: Session,
    engine: Engine,
    conectores: Sequence[Conector],
    perfis: Sequence[PerfilDB],
    agora: datetime,
    tipo: str = "sob_demanda",
) -> ResumoRodada:
    # A coleta (rede + quota, que grava em transação própria) vem antes de qualquer
    # escrita desta sessão: o SQLite aceita um escritor por vez, e chamadas HTTP
    # lentas não devem segurar o lock do banco.
    brutas, erros = coletar(engine, conectores, _queries(perfis), agora)

    execucao = ExecucaoDB(tipo=tipo, status="rodando", iniciada_em=agora)
    s.add(execucao)
    s.flush()
    resumo = ResumoRodada(execucao_id=execucao.id, erros=list(erros))

    for bruta in brutas:
        try:
            vaga = normalizar(bruta)
            _, nova = repo.registrar_vaga(s, bruta, vaga, chave_dedup(vaga))
        except Exception as e:
            log.exception("falha ao normalizar %s/%s", bruta.fonte, bruta.id_fonte)
            resumo.erros.append(f"{bruta.fonte}/{bruta.id_fonte}: {type(e).__name__}: {e}")
            continue
        resumo.coletadas += 1
        resumo.por_fonte[bruta.fonte] += 1
        resumo.novas += int(nova)

    for perfil_db in perfis:
        resumo.perfis.append(_filtrar_para_perfil(s, perfil_db, agora))

    execucao.finalizada_em = datetime.now(agora.tzinfo)
    execucao.vagas_coletadas = resumo.coletadas
    execucao.vagas_novas = resumo.novas
    execucao.matches_novos = sum(len(p.aprovadas) for p in resumo.perfis)
    execucao.erros = resumo.erros
    if not resumo.erros:
        execucao.status = "ok"
    elif resumo.coletadas:
        execucao.status = "parcial"
    else:
        execucao.status = "erro"
    return resumo


def _filtrar_para_perfil(s: Session, perfil_db: PerfilDB, agora: datetime) -> ResumoPerfil:
    perfil = repo.perfil_de(perfil_db)
    resumo = ResumoPerfil(nome=perfil.nome)
    desde = agora - timedelta(days=perfil.filtros.idade_maxima_dias)
    for vaga_db in repo.candidatas(s, perfil_db.id, desde):
        vaga = repo.vaga_de(vaga_db)
        resumo.avaliadas += 1
        motivo = motivo_descarte(vaga, perfil.filtros, agora)
        if motivo:
            resumo.descartes[motivo] += 1
            continue
        # Passou nos filtros: fica pendente até o scoring (M2).
        s.add(MatchDB(vaga_id=vaga_db.id, perfil_id=perfil_db.id, status=StatusMatch.PENDENTE))
        resumo.aprovadas.append(vaga)
    s.flush()
    return resumo
