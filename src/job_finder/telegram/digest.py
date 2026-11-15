"""Digest do Telegram: escolhe as vagas novas de um perfil, formata e envia.

O envio passa por um `Enviador` (o bot real ou um dublê nos testes). Uma vaga é marcada
como notificada só depois que a mensagem dela sai, para uma falha de rede não perder vagas.
"""

import asyncio
import html
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import Engine, select

from job_finder.db.models import MatchDB, PerfilDB
from job_finder.db.session import sessao
from job_finder.domain import StatusMatch

log = logging.getLogger(__name__)

# Nome de exibição das fontes. Os termos do Remotive pedem para citá-lo como fonte.
NOMES_FONTES = {"remotive": "Remotive"}
MODALIDADES = {"remoto": "Remoto", "hibrido": "Híbrido", "presencial": "Presencial"}
PERIODOS = {"ano": "ano", "mes": "mês", "hora": "hora"}


@dataclass(frozen=True)
class Botao:
    texto: str
    callback: str | None = None
    url: str | None = None


class Enviador(Protocol):
    async def enviar(
        self, chat_id: int, texto: str, botoes: Sequence[Botao] | None = None
    ) -> None: ...


@dataclass
class ItemDigest:
    match_id: int
    score: int
    titulo: str
    empresa: str | None
    local: str
    salario: str | None
    pontos_fortes: list[str]
    gaps: list[str]
    dealbreakers: list[str]
    resumo: str | None
    url: str
    fontes: list[str]


@dataclass
class Digest:
    chat_id: int | None
    novas: int
    score_minimo: int
    itens: list[ItemDigest] = field(default_factory=list)
    abaixo_do_minimo: list[int] = field(default_factory=list)  # ids: marcar sem enviar
    acima_do_minimo: int = 0


def _salario(m: MatchDB) -> str | None:
    v = m.vaga
    if v.salario_min is None or v.salario_max is None:
        return None

    def k(x: float) -> str:
        return f"{x / 1000:.0f}k" if x >= 10_000 else f"{x:,.0f}"

    faixa = (
        k(v.salario_min)
        if v.salario_min == v.salario_max
        else f"{k(v.salario_min)}-{k(v.salario_max)}"
    )
    periodo = f"/{PERIODOS[v.salario_periodo]}" if v.salario_periodo in PERIODOS else ""
    return f"{v.salario_moeda or ''} {faixa}{periodo}".strip()


def _local(m: MatchDB) -> str:
    v = m.vaga
    modalidade = MODALIDADES.get(v.modalidade)
    partes = [p for p in (modalidade, v.local_texto) if p]
    return " · ".join(partes) or "local não informado"


def _item(m: MatchDB) -> ItemDigest:
    return ItemDigest(
        match_id=m.id,
        score=m.score or 0,
        titulo=m.vaga.titulo,
        empresa=m.vaga.empresa,
        local=_local(m),
        salario=_salario(m),
        pontos_fortes=list(m.pontos_fortes or []),
        gaps=list(m.gaps or []),
        dealbreakers=list(m.dealbreakers or []),
        resumo=m.resumo,
        url=m.vaga.url,
        fontes=sorted({NOMES_FONTES.get(f.fonte, f.fonte) for f in m.vaga.fontes}),
    )


def montar_digest(engine: Engine, perfil_id: int, *, score_minimo: int, maximo: int) -> Digest:
    """Vagas com nota ainda não notificadas: as melhores acima do mínimo, até `maximo`.

    As que passam do máximo ficam para o próximo digest; as abaixo do mínimo são só
    contadas no cabeçalho e marcadas como notificadas.
    """
    with sessao(engine, commit=False) as s:
        perfil = s.get_one(PerfilDB, perfil_id)
        matches = list(
            s.scalars(
                select(MatchDB)
                .where(
                    MatchDB.perfil_id == perfil_id,
                    MatchDB.status == StatusMatch.NOVA,
                    MatchDB.notificado_em.is_(None),
                )
                .order_by(MatchDB.score.desc(), MatchDB.id)
            )
        )
        acima = [m for m in matches if (m.score or 0) >= score_minimo]
        return Digest(
            chat_id=perfil.telegram_chat_id,
            novas=len(matches),
            score_minimo=score_minimo,
            itens=[_item(m) for m in acima[:maximo]],
            abaixo_do_minimo=[m.id for m in matches if (m.score or 0) < score_minimo],
            acima_do_minimo=len(acima),
        )


# --- formatação (HTML do Telegram) --------------------------------------------


def _e(texto: str) -> str:
    return html.escape(texto, quote=False)


def formatar_cabecalho(d: Digest) -> str:
    if d.novas == 0:
        return "Nenhuma vaga nova desde o último digest."
    texto = f"<b>{d.novas} vagas novas</b> · {d.acima_do_minimo} com fit ≥ {d.score_minimo}"
    if d.acima_do_minimo > len(d.itens):
        texto += f"\nMostrando as {len(d.itens)} melhores; o resto vem no próximo digest."
    return texto


def formatar_vaga(item: ItemDigest) -> str:
    linhas = [f"<b>{item.score}</b> · <b>{_e(item.titulo)}</b>"]
    detalhe = " · ".join(_e(p) for p in (item.empresa, item.local, item.salario) if p)
    if detalhe:
        linhas.append(detalhe)
    if item.pontos_fortes:
        linhas.append("✅ " + "; ".join(_e(p) for p in item.pontos_fortes))
    if item.gaps:
        linhas.append("⚠️ " + "; ".join(_e(g) for g in item.gaps))
    if item.dealbreakers:
        linhas.append("⛔ " + "; ".join(_e(d) for d in item.dealbreakers))
    if item.resumo:
        linhas.append(f"<i>{_e(item.resumo)}</i>")
    if item.fontes:
        linhas.append(f"via {_e(', '.join(item.fontes))}")
    return "\n".join(linhas)


def botoes_da_vaga(match_id: int, url: str, status: str | None = None) -> list[Botao]:
    salvar = "✅ Salva" if status == StatusMatch.SALVA else "👍 Salvar"
    descartar = "🗑 Descartada" if status == StatusMatch.DESCARTADA else "👎 Descartar"
    return [
        Botao(salvar, callback=f"fb:s:{match_id}"),
        Botao(descartar, callback=f"fb:d:{match_id}"),
        Botao("🔗 Abrir", url=url),
    ]


def formatar_avisos(avisos: Sequence[str]) -> str:
    return "<b>Avisos</b>\n" + "\n".join(f"• {_e(a)}" for a in avisos)


# --- envio --------------------------------------------------------------------


def marcar_notificados(engine: Engine, ids: Sequence[int], quando: datetime) -> None:
    if not ids:
        return
    with sessao(engine) as s:
        for m in s.scalars(select(MatchDB).where(MatchDB.id.in_(ids))):
            m.notificado_em = quando


async def enviar_digest(
    engine: Engine,
    enviador: Enviador,
    perfil_id: int,
    *,
    score_minimo: int,
    maximo: int,
    avisos: Sequence[str] = (),
    pausa_s: float = 0.4,  # o Telegram limita ~1 mensagem/s por chat
) -> int:
    """Envia o digest do perfil. Devolve quantas vagas foram enviadas."""
    d = await asyncio.to_thread(
        montar_digest, engine, perfil_id, score_minimo=score_minimo, maximo=maximo
    )
    if d.chat_id is None:
        log.info("perfil %s sem telegram_chat_id; digest não enviado", perfil_id)
        return 0

    await enviador.enviar(d.chat_id, formatar_cabecalho(d))
    enviadas = 0
    for item in d.itens:
        await asyncio.sleep(pausa_s)
        await enviador.enviar(
            d.chat_id, formatar_vaga(item), botoes_da_vaga(item.match_id, item.url)
        )
        await asyncio.to_thread(marcar_notificados, engine, [item.match_id], datetime.now(UTC))
        enviadas += 1
    await asyncio.to_thread(marcar_notificados, engine, d.abaixo_do_minimo, datetime.now(UTC))
    if avisos:
        await enviador.enviar(d.chat_id, formatar_avisos(avisos))
    return enviadas
