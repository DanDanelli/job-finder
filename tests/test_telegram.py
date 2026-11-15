"""Digest, ações do bot e agendador, com um Enviador falso (sem Telegram)."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import FeedbackDB, MatchDB
from job_finder.db.session import sessao
from job_finder.domain import Perfil, StatusMatch, VagaBruta
from job_finder.pipeline.run import executar_rodada
from job_finder.quota import Quota
from job_finder.scheduler import Agendador, proxima_execucao
from job_finder.servico import rodada_completa
from job_finder.telegram import acoes
from job_finder.telegram.digest import (
    Botao,
    enviar_digest,
    formatar_cabecalho,
    formatar_vaga,
    montar_digest,
)
from job_finder.web.app import create_app

AGORA = datetime(2026, 11, 15, 12, tzinfo=UTC)
LISBOA = ZoneInfo("Europe/Lisbon")
CHAT_DANIEL = 111
CHAT_ESPOSA = 222


class EnviadorFalso:
    def __init__(self, falhar_em: int | None = None) -> None:
        self.mensagens: list[tuple[int, str, list[Botao]]] = []
        self.falhar_em = falhar_em

    async def enviar(self, chat_id: int, texto: str, botoes: Sequence[Botao] | None = None) -> None:
        if self.falhar_em is not None and len(self.mensagens) == self.falhar_em:
            raise ConnectionError("telegram fora")
        self.mensagens.append((chat_id, texto, list(botoes or [])))


def bruta(id_fonte: str, titulo: str, **kw: Any) -> VagaBruta:
    base: dict[str, Any] = {
        "fonte": "remotive",
        "id_fonte": id_fonte,
        "titulo": titulo,
        "empresa": "Acme & Co",
        "local": "Europe",
        "modalidade": "remote",
        "url": f"https://remotive.com/{id_fonte}",
        "publicada_em": AGORA - timedelta(days=1),
        "salario_texto": "€60k - €80k",
    }
    return VagaBruta.model_validate(base | kw)


class Fonte:
    nome = "remotive"
    limite_diario = None

    def __init__(self, vagas: list[VagaBruta]) -> None:
        self.vagas = vagas

    def buscar(self, queries: object, quota: Quota) -> list[VagaBruta]:
        return self.vagas


def preparar(engine: Engine, scores: dict[str, int]) -> dict[str, int]:
    """Perfis daniel e esposa; uma vaga por título, já pontuada para o daniel."""
    with sessao(engine) as s:
        repo.salvar_perfil(s, Perfil(nome="daniel", telegram_chat_id=CHAT_DANIEL))
        repo.salvar_perfil(s, Perfil(nome="esposa", telegram_chat_id=CHAT_ESPOSA))
    vagas = [bruta(str(i), titulo) for i, titulo in enumerate(scores)]
    with sessao(engine) as s:
        executar_rodada(s, engine, [Fonte(vagas)], repo.listar_perfis(s, ["daniel"]), AGORA)
    ids = {}
    with sessao(engine) as s:
        for m in s.scalars(select(MatchDB)):
            m.score = scores[m.vaga.titulo]
            m.status = StatusMatch.NOVA
            m.resumo = f"Resumo de {m.vaga.titulo}"
            m.pontos_fortes = ["AWS <forte>"]
            m.gaps = ["Azure"]
            ids[m.vaga.titulo] = m.id
    return ids


def perfil_id(engine: Engine, nome: str) -> int:
    with sessao(engine) as s:
        p = repo.buscar_perfil(s, nome)
        assert p is not None
        return p.id


# --- agenda -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agora", "esperado"),
    [
        # 15/11 é inverno em Lisboa (UTC+0).
        (datetime(2026, 11, 15, 7, 0, tzinfo=UTC), datetime(2026, 11, 15, 8, 0, tzinfo=UTC)),
        (datetime(2026, 11, 15, 8, 0, tzinfo=UTC), datetime(2026, 11, 15, 18, 0, tzinfo=UTC)),
        (datetime(2026, 11, 15, 19, 0, tzinfo=UTC), datetime(2026, 11, 16, 8, 0, tzinfo=UTC)),
        # Verão (UTC+1): 08h em Lisboa são 07h UTC.
        (datetime(2026, 7, 1, 6, 0, tzinfo=UTC), datetime(2026, 7, 1, 7, 0, tzinfo=UTC)),
        # Véspera da mudança de horário (29/03/2026): amanhã 08h já é UTC+1.
        (datetime(2026, 3, 28, 20, 0, tzinfo=UTC), datetime(2026, 3, 29, 7, 0, tzinfo=UTC)),
    ],
)
def test_proxima_execucao(agora: datetime, esperado: datetime) -> None:
    assert proxima_execucao(agora, [time(8), time(18)], LISBOA) == esperado


def test_horarios_configuraveis_por_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBFINDER_HORARIOS", '["07:30", "19:00"]')
    assert Settings().horarios == [time(7, 30), time(19)]


# --- digest -------------------------------------------------------------------


def test_digest_seleciona_ordena_e_corta(engine: Engine) -> None:
    preparar(engine, {"A": 90, "B": 75, "C": 40, "D": 65})
    d = montar_digest(engine, perfil_id(engine, "daniel"), score_minimo=60, maximo=2)
    assert d.chat_id == CHAT_DANIEL
    assert (d.novas, d.acima_do_minimo) == (4, 3)
    assert [i.titulo for i in d.itens] == ["A", "B"]  # D (65) fica para o próximo
    assert len(d.abaixo_do_minimo) == 1  # C
    assert formatar_cabecalho(d) == (
        "<b>4 vagas novas</b> · 3 com fit ≥ 60\n"
        "Mostrando as 2 melhores; o resto vem no próximo digest."
    )


def test_formatar_vaga_escapa_html_e_cita_a_fonte(engine: Engine) -> None:
    preparar(engine, {"Cloud <Architect>": 88})
    item = montar_digest(engine, perfil_id(engine, "daniel"), score_minimo=0, maximo=5).itens[0]
    assert formatar_vaga(item) == (
        "<b>88</b> · <b>Cloud &lt;Architect&gt;</b>\n"
        "Acme &amp; Co · Remoto · Europe · EUR 60k-80k/ano\n"
        "✅ AWS &lt;forte&gt;\n"
        "⚠️ Azure\n"
        "<i>Resumo de Cloud &lt;Architect&gt;</i>\n"
        "via Remotive"
    )


def test_digest_vazio() -> None:
    from job_finder.telegram.digest import Digest

    assert formatar_cabecalho(Digest(chat_id=1, novas=0, score_minimo=60)) == (
        "Nenhuma vaga nova desde o último digest."
    )


def test_enviar_digest_marca_notificadas_e_nao_repete(engine: Engine) -> None:
    ids = preparar(engine, {"A": 90, "B": 30})
    pid = perfil_id(engine, "daniel")
    enviador = EnviadorFalso()

    n = asyncio.run(
        enviar_digest(engine, enviador, pid, score_minimo=60, maximo=10, avisos=["x"], pausa_s=0)
    )

    assert n == 1
    cabecalho, vaga, avisos = enviador.mensagens
    assert cabecalho[0] == CHAT_DANIEL
    assert "2 vagas novas" in cabecalho[1]
    assert [b.callback for b in vaga[2][:2]] == [f"fb:s:{ids['A']}", f"fb:d:{ids['A']}"]
    assert vaga[2][2].url == "https://remotive.com/0"
    assert "Avisos" in avisos[1]

    # A e B (abaixo do mínimo) ficam notificadas; o próximo digest vem vazio.
    segundo = EnviadorFalso()
    asyncio.run(enviar_digest(engine, segundo, pid, score_minimo=60, maximo=10, pausa_s=0))
    assert [m[1] for m in segundo.mensagens] == ["Nenhuma vaga nova desde o último digest."]


def test_falha_no_envio_nao_marca_a_vaga(engine: Engine) -> None:
    preparar(engine, {"A": 90, "B": 80})
    pid = perfil_id(engine, "daniel")
    with pytest.raises(ConnectionError):
        asyncio.run(
            enviar_digest(
                engine, EnviadorFalso(falhar_em=2), pid, score_minimo=0, maximo=10, pausa_s=0
            )
        )
    d = montar_digest(engine, pid, score_minimo=0, maximo=10)
    assert [i.titulo for i in d.itens] == ["B"]  # A saiu; B tenta de novo no próximo


def test_perfil_sem_chat_nao_envia(engine: Engine) -> None:
    with sessao(engine) as s:
        repo.salvar_perfil(s, Perfil(nome="sem_chat"))
    enviador = EnviadorFalso()
    pid = perfil_id(engine, "sem_chat")
    assert asyncio.run(enviar_digest(engine, enviador, pid, score_minimo=0, maximo=1)) == 0
    assert enviador.mensagens == []


# --- ações do bot -------------------------------------------------------------


def test_allowlist(engine: Engine) -> None:
    preparar(engine, {"A": 90})
    assert acoes.perfil_do_chat(engine, CHAT_DANIEL).nome == "daniel"  # type: ignore[union-attr]
    assert acoes.perfil_do_chat(engine, 999) is None


def test_feedback_atualiza_status_e_grava(engine: Engine) -> None:
    ids = preparar(engine, {"A": 90})
    r = acoes.registrar_feedback(engine, CHAT_DANIEL, f"fb:s:{ids['A']}")
    assert r is not None and r.status == StatusMatch.SALVA
    r = acoes.registrar_feedback(engine, CHAT_DANIEL, f"fb:d:{ids['A']}")  # mudou de ideia
    assert r is not None and r.status == StatusMatch.DESCARTADA
    with sessao(engine) as s:
        assert s.get_one(MatchDB, ids["A"]).status == StatusMatch.DESCARTADA
        assert [f.acao for f in s.scalars(select(FeedbackDB).order_by(FeedbackDB.id))] == [
            "salvar",
            "descartar",
        ]


@pytest.mark.parametrize("dados", ["fb:x:1", "fb:s:abc", "lixo", "fb:s:99999"])
def test_feedback_invalido(engine: Engine, dados: str) -> None:
    preparar(engine, {"A": 90})
    assert acoes.registrar_feedback(engine, CHAT_DANIEL, dados) is None


def test_feedback_de_outro_perfil_e_rejeitado(engine: Engine) -> None:
    ids = preparar(engine, {"A": 90})  # match do daniel
    assert acoes.registrar_feedback(engine, CHAT_ESPOSA, f"fb:s:{ids['A']}") is None
    assert acoes.registrar_feedback(engine, 999, f"fb:s:{ids['A']}") is None
    with sessao(engine) as s:
        assert s.get_one(MatchDB, ids["A"]).status == StatusMatch.NOVA


def test_salvas_e_status(engine: Engine) -> None:
    ids = preparar(engine, {"A & B": 90, "C": 70})
    pid = perfil_id(engine, "daniel")
    assert "Nenhuma vaga salva" in acoes.texto_salvas(engine, pid)
    acoes.registrar_feedback(engine, CHAT_DANIEL, f"fb:s:{ids['A & B']}")
    salvas = acoes.texto_salvas(engine, pid)
    assert '<a href="https://remotive.com/0">A &amp; B</a>' in salvas

    status = acoes.texto_status(engine, pid, Settings(), agora=AGORA)
    assert "Rodadas: 08:00, 18:00 (Europe/Lisbon)" in status
    assert "1 novas, 1 salvas, 0 descartadas, 0 pendentes" in status
    assert "Claude no mês: US$ 0.00 de US$ 5.00" in status


def test_pausa_tira_o_perfil_da_rodada_agendada(engine: Engine) -> None:
    preparar(engine, {"A": 90})
    pid = perfil_id(engine, "daniel")
    assert acoes.alternar_pausa(engine, pid) is True
    assert acoes.perfil_do_chat(engine, CHAT_DANIEL).pausado  # type: ignore[union-attr]

    settings = Settings(conectores=[])
    agendada = rodada_completa(engine, settings, tipo="agendada", agora=AGORA)
    assert [p.nome for p in agendada.perfis] == ["esposa"]
    # Sob demanda (/buscar) o perfil pausado ainda roda.
    manual = rodada_completa(engine, settings, nomes=["daniel"], agora=AGORA)
    assert [p.nome for p in manual.perfis] == ["daniel"]

    assert acoes.alternar_pausa(engine, pid) is False


# --- agendador ----------------------------------------------------------------


def test_agendador_roda_e_envia_o_digest_de_cada_perfil(engine: Engine) -> None:
    preparar(engine, {"A": 90})
    enviador = EnviadorFalso()
    agendador = Agendador(engine, Settings(conectores=[]), enviador, pausa_entre_mensagens_s=0)

    relatorio = asyncio.run(agendador.rodar_e_notificar())

    assert relatorio is not None
    por_chat: dict[int, list[str]] = {}
    for chat, texto, _ in enviador.mensagens:
        por_chat.setdefault(chat, []).append(texto)
    assert any("<b>A</b>" in t for t in por_chat[CHAT_DANIEL])
    assert por_chat[CHAT_ESPOSA][0] == "Nenhuma vaga nova desde o último digest."
    # Sem chave do Claude, o aviso vai no rodapé dos dois.
    assert all("ANTHROPIC_API_KEY" in textos[-1] for textos in por_chat.values())


def test_app_sobe_e_desce_o_agendador(db_url: str) -> None:
    from job_finder.db.session import upgrade

    upgrade(db_url)
    app = create_app(Settings(database_url=db_url, agendamento_ativo=True))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_bot_registra_os_comandos(engine: Engine) -> None:
    from telegram.ext import CallbackQueryHandler, CommandHandler

    from job_finder.telegram.bot import criar_aplicacao, registrar_handlers, teclado

    app = criar_aplicacao("123456:TESTE")  # construir não acessa a rede
    registrar_handlers(app, engine, Settings(), Agendador(engine, Settings(), None))
    handlers = app.handlers[0]
    comandos = {c for h in handlers if isinstance(h, CommandHandler) for c in h.commands}
    assert comandos == {"start", "buscar", "salvas", "pausar", "status"}
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)

    markup = teclado([Botao("👍", callback="fb:s:1"), Botao("🔗", url="https://x.example")])
    linha = markup.inline_keyboard[0]
    assert (linha[0].callback_data, linha[1].url) == ("fb:s:1", "https://x.example")


def test_log_nao_expoe_o_token_do_bot(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from typer.testing import CliRunner

    from job_finder.cli import app as cli_app

    CliRunner().invoke(cli_app, ["perfil", "listar"])  # roda o callback que configura o log
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("POST https://api.telegram.org/bot123:SEGREDO/getMe")
    assert "SEGREDO" not in caplog.text
