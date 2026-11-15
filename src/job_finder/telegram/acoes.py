"""Ações do bot sobre o banco, sem dependência do Telegram (testáveis direto)."""

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import anthropic
import httpx
from sqlalchemy import Engine, func, select

from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import ExecucaoDB, FeedbackDB, MatchDB, PerfilDB, UsoQuotaDB
from job_finder.db.session import sessao
from job_finder.domain import StatusMatch
from job_finder.llm.client import (
    ClienteLLM,
    LimiteDeCustoAtingido,
    RespostaInvalida,
    criar_cliente,
    erro_fatal,
)
from job_finder.telegram.digest import Botao, _e, botoes_da_vaga, formatar_vaga

log = logging.getLogger(__name__)

ACOES = {"s": ("salvar", StatusMatch.SALVA), "d": ("descartar", StatusMatch.DESCARTADA)}


@dataclass(frozen=True)
class PerfilChat:
    id: int
    nome: str
    pausado: bool


def perfil_do_chat(engine: Engine, chat_id: int) -> PerfilChat | None:
    """Allowlist: só chats ligados a um perfil são atendidos."""
    with sessao(engine, commit=False) as s:
        p = repo.perfil_do_chat(s, chat_id)
        return PerfilChat(p.id, p.nome, p.pausado) if p else None


@dataclass(frozen=True)
class ResultadoFeedback:
    match_id: int
    status: str
    url: str


def registrar_feedback(
    engine: Engine, chat_id: int, dados_callback: str, origem: str = "telegram"
) -> ResultadoFeedback | None:
    """Aplica 👍/👎 de um botão `fb:<s|d>:<match_id>`.

    Devolve None se o callback for inválido ou se o match não for do perfil deste chat.
    """
    partes = dados_callback.split(":")
    if len(partes) != 3 or partes[0] != "fb" or partes[1] not in ACOES or not partes[2].isdigit():
        return None
    acao, status = ACOES[partes[1]]
    with sessao(engine) as s:
        perfil = repo.perfil_do_chat(s, chat_id)
        match = s.get(MatchDB, int(partes[2]))
        if perfil is None or match is None or match.perfil_id != perfil.id:
            return None
        match.status = status
        s.add(FeedbackDB(match_id=match.id, acao=acao, origem=origem))
        return ResultadoFeedback(match.id, status, match.vaga.url)


def alternar_pausa(engine: Engine, perfil_id: int) -> bool:
    with sessao(engine) as s:
        p = s.get_one(PerfilDB, perfil_id)
        p.pausado = not p.pausado
        return p.pausado


def texto_salvas(engine: Engine, perfil_id: int, limite: int = 20) -> str:
    with sessao(engine, commit=False) as s:
        matches = s.scalars(
            select(MatchDB)
            .where(MatchDB.perfil_id == perfil_id, MatchDB.status == StatusMatch.SALVA)
            .order_by(MatchDB.atualizado_em.desc())
            .limit(limite)
        ).all()
        if not matches:
            return "Nenhuma vaga salva ainda. Use 👍 nas vagas do digest."
        linhas = [f"<b>Vagas salvas</b> ({len(matches)})"]
        for m in matches:
            empresa = f" · {_e(m.vaga.empresa)}" if m.vaga.empresa else ""
            linhas.append(
                f'• <a href="{_e(m.vaga.url)}">{_e(m.vaga.titulo)}</a>{empresa} ({m.score})'
            )
        return "\n".join(linhas)


def texto_status(
    engine: Engine, perfil_id: int, settings: Settings, agora: datetime | None = None
) -> str:
    agora = agora or datetime.now(UTC)
    cliente = ClienteLLM(
        engine, None, limite_mensal_usd=settings.custo_mensal_max_usd, relogio=lambda: agora
    )
    with sessao(engine, commit=False) as s:
        perfil = s.get_one(PerfilDB, perfil_id)
        nome, pausado = perfil.nome, perfil.pausado
        ultima = s.scalars(select(ExecucaoDB).order_by(ExecucaoDB.id.desc()).limit(1)).first()
        resumo_ultima = (ultima.iniciada_em, ultima.status, ultima.vagas_novas) if ultima else None
        contagem = dict(
            s.execute(
                select(MatchDB.status, func.count())
                .where(MatchDB.perfil_id == perfil_id)
                .group_by(MatchDB.status)
            ).all()
        )
        quotas = s.execute(
            select(UsoQuotaDB.conector, UsoQuotaDB.chamadas).where(UsoQuotaDB.dia == agora.date())
        ).all()

    horarios = ", ".join(h.strftime("%H:%M") for h in settings.horarios)
    linhas = [
        f"<b>{_e(nome)}</b>" + (" · ⏸ pausado" if pausado else ""),
        f"Rodadas: {horarios} ({_e(settings.timezone)})",
    ]
    if resumo_ultima:
        iniciada, st, novas = resumo_ultima
        quando = iniciada.strftime("%d/%m %H:%M UTC")
        linhas.append(f"Última rodada: {quando}, {st}, {novas} vagas novas")
    linhas.append(
        "Vagas: "
        + ", ".join(
            f"{contagem.get(st, 0)} {rotulo}"
            for st, rotulo in (
                (StatusMatch.NOVA, "novas"),
                (StatusMatch.SALVA, "salvas"),
                (StatusMatch.DESCARTADA, "descartadas"),
                (StatusMatch.PENDENTE, "pendentes"),
            )
        )
    )
    teto = settings.custo_mensal_max_usd
    linhas.append(f"Claude no mês: US$ {cliente.gasto_no_mes():.2f} de US$ {teto:.2f}")
    if quotas:
        linhas.append("Quota hoje: " + ", ".join(f"{c} {n}" for c, n in quotas))
    return "\n".join(linhas)


COMO_AVALIAR = (
    "Mande o link de uma vaga que você viu (LinkedIn, Indeed, Catho, Gupy, site da "
    "empresa...) ou cole o texto completo dela, e eu digo o fit com o seu perfil."
)


def avaliar_no_chat(
    engine: Engine,
    settings: Settings,
    perfil_id: int,
    mensagem: str,
    *,
    cliente: ClienteLLM | None = None,
    http: httpx.Client | None = None,
    agora: datetime | None = None,
    resolver: Callable[[str], list[str]] | None = None,
) -> tuple[str, list[Botao]]:
    """Resposta do bot para um link ou texto de vaga: (texto HTML, botões)."""
    from job_finder import avulsa
    from job_finder.connectors.base import cliente_http

    if cliente is None:
        if settings.anthropic_api_key is None:
            return "Sem ANTHROPIC_API_KEY configurada: não consigo avaliar vagas.", []
        cliente = criar_cliente(engine)
    extras = {"resolver": resolver} if resolver else {}
    try:
        with contextlib.nullcontext(http) if http else cliente_http() as h:
            r = avulsa.avaliar_mensagem(
                engine,
                cliente,
                perfil_id,
                mensagem,
                http=h,
                modelo=settings.modelo_scoring,
                agora=agora or datetime.now(UTC),
                **extras,
            )
    except avulsa.EntradaInvalida as e:
        return _e(str(e)), []
    except LimiteDeCustoAtingido:
        return "O teto de gasto com o Claude deste mês foi atingido; não consigo avaliar.", []
    except (RespostaInvalida, anthropic.APIError, ValueError) as e:
        log.warning("avaliação avulsa falhou: %s", e)
        if erro_fatal(e):
            return _e(f"Claude indisponível: {type(e).__name__}: {e}"), []
        return "Não consegui avaliar agora. Tente de novo em alguns minutos.", []
    texto = formatar_vaga(r.item)
    if r.avisos:
        texto = "\n".join(_e(a) for a in r.avisos) + "\n\n" + texto
    return texto, botoes_da_vaga(r.item.match_id, r.item.url, r.status)
