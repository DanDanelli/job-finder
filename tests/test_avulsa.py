"""Avaliação avulsa de vagas (link ou texto mandado no bot), sem rede e sem o Claude real."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, select

from job_finder import avulsa
from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import CustoLLMDB, MatchDB
from job_finder.db.session import sessao
from job_finder.domain import Filtros, Perfil, ResultadoFit, StatusMatch
from job_finder.llm.client import ClienteLLM
from job_finder.pipeline.run import executar_rodada
from job_finder.telegram import acoes
from job_finder.telegram.bot import Agrupador

AGORA = datetime(2026, 11, 15, 12, tzinfo=UTC)
URL = "https://vagas.example/vaga/123"
PAGINA = """<html><head><title>Vaga</title><script>var x = 1;</script></head><body>
<h1>Arquiteto Cloud Sênior</h1><p>Acme Ltda · Remoto - Brasil</p>
<p>Desenhar arquiteturas AWS multi-conta, landing zone e governança.</p></body></html>"""
TEXTO_COLADO = "Arquiteto Cloud Sênior na Acme. " + "Requisitos: AWS, Terraform, EKS. " * 12


def publico(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def http() -> httpx.Client:
    return httpx.Client()


class Mensagens:
    def __init__(self, dados: avulsa.DadosVaga, fit: ResultadoFit) -> None:
        self.dados, self.fit = dados, fit
        self.chamadas: list[dict[str, Any]] = []

    def parse(self, **kw: Any) -> Any:
        self.chamadas.append(kw)
        saida = self.dados if kw["output_format"] is avulsa.DadosVaga else self.fit

        class Resposta:
            parsed_output = saida
            stop_reason = "end_turn"

            class usage:
                input_tokens = 1_000
                output_tokens = 100
                cache_read_input_tokens = 0
                cache_creation_input_tokens = 0

        return Resposta()


class Anthropic:
    def __init__(self, mensagens: Mensagens) -> None:
        self.messages = mensagens


def dados(**kw: Any) -> avulsa.DadosVaga:
    base: dict[str, Any] = {
        "eh_vaga": True,
        "titulo": "Arquiteto Cloud Sênior",
        "empresa": "Acme",
        "local": "Remoto - Brasil",
        "modalidade": "remoto",
    }
    return avulsa.DadosVaga.model_validate(base | kw)


def cliente(
    engine: Engine, d: avulsa.DadosVaga | None = None, score: int = 82
) -> tuple[ClienteLLM, Mensagens]:
    m = Mensagens(d or dados(), ResultadoFit(score=score, pontos_fortes=["AWS"], resumo="Bom."))
    return ClienteLLM(engine, Anthropic(m), limite_mensal_usd=5, relogio=lambda: AGORA), m


def perfis(engine: Engine, filtros: Filtros | None = None) -> int:
    with sessao(engine) as s:
        p = repo.salvar_perfil(s, Perfil(nome="daniel", filtros=filtros or Filtros()))
        repo.salvar_perfil(s, Perfil(nome="esposa"))
        return p.id


def avaliar(
    engine: Engine, perfil_id: int, mensagem: str, c: ClienteLLM, http: httpx.Client
) -> tuple[str, list[Any]]:
    return acoes.avaliar_no_chat(
        engine,
        Settings(anthropic_api_key="sk-teste"),
        perfil_id,
        mensagem,
        cliente=c,
        http=http,
        agora=AGORA,
        resolver=publico,
    )


# --- entrada e download -------------------------------------------------------


def test_interpreta_link_e_texto() -> None:
    assert avulsa.interpretar_mensagem(f"olha essa: {URL}.") == avulsa.Entrada(URL, None)
    entrada = avulsa.interpretar_mensagem(TEXTO_COLADO)
    assert entrada.url is None and entrada.texto == TEXTO_COLADO.strip()
    with pytest.raises(avulsa.EntradaInvalida):
        avulsa.interpretar_mensagem("oi, tudo bem?")


@respx.mock
def test_baixa_a_pagina_como_texto(http: httpx.Client) -> None:
    respx.get(URL).respond(200, html=PAGINA)
    texto = avulsa.baixar_pagina(http, URL, publico)
    assert "Arquiteto Cloud Sênior" in texto and "var x" not in texto


@respx.mock
def test_site_que_bloqueia_pede_o_texto(http: httpx.Client) -> None:
    respx.get(URL).respond(403, html="Security Check")
    with pytest.raises(avulsa.EntradaInvalida, match="cole aqui"):
        avulsa.baixar_pagina(http, URL, publico)


@pytest.mark.parametrize(
    "url", ["http://localhost:8000/health", "http://192.168.1.10/", "ftp://vagas.example/x"]
)
def test_nao_abre_enderecos_da_rede_local(http: httpx.Client, url: str) -> None:
    def resolver(host: str) -> list[str]:
        return {"localhost": ["127.0.0.1"], "192.168.1.10": ["192.168.1.10"]}.get(
            host, ["93.184.216.34"]
        )

    with pytest.raises(avulsa.EntradaInvalida):
        avulsa.baixar_pagina(http, url, resolver)


@respx.mock
def test_redirect_para_a_rede_local_e_bloqueado(http: httpx.Client) -> None:
    respx.get(URL).respond(302, headers={"location": "http://roteador.local/admin"})
    rota_interna = respx.get("http://roteador.local/admin").respond(200, html="admin")

    def resolver(host: str) -> list[str]:
        return ["192.168.0.1"] if host == "roteador.local" else ["93.184.216.34"]

    with pytest.raises(avulsa.EntradaInvalida, match="público"):
        avulsa.baixar_pagina(http, URL, resolver)
    assert not rota_interna.called


# --- avaliação de ponta a ponta -----------------------------------------------


@respx.mock
def test_link_vira_match_avaliado_com_botoes(engine: Engine, http: httpx.Client) -> None:
    respx.get(URL).respond(200, html=PAGINA)
    pid = perfis(engine)
    c, msgs = cliente(engine)

    texto, botoes = avaliar(engine, pid, f"o que acha? {URL}", c, http)

    assert "<b>82</b>" in texto and "Arquiteto Cloud Sênior" in texto
    with sessao(engine) as s:
        m = s.scalars(select(MatchDB)).one()
        assert (m.status, m.score, m.notificado_em is not None) == (StatusMatch.NOVA, 82, True)
        assert m.vaga.url == URL
        operacoes = sorted(s.scalars(select(CustoLLMDB.operacao)))
    assert [b.callback for b in botoes[:2]] == [f"fb:s:{m.id}", f"fb:d:{m.id}"]
    assert botoes[2].url == URL
    assert operacoes == ["extracao_vaga", "scoring"]
    # O texto da página vai para o LLM como dado, dentro de <pagina>.
    assert "<pagina>" in msgs.chamadas[0]["messages"][0]["content"]


@respx.mock
def test_mesmo_link_nao_e_avaliado_de_novo(engine: Engine, http: httpx.Client) -> None:
    rota = respx.get(URL).respond(200, html=PAGINA)
    pid = perfis(engine)
    c, msgs = cliente(engine)
    avaliar(engine, pid, URL, c, http)

    texto, botoes = avaliar(engine, pid, URL, c, http)

    assert avulsa.JA_AVALIADA in texto and botoes
    assert rota.call_count == 1 and len(msgs.chamadas) == 2


def test_texto_colado_sem_link_usa_busca_no_google(engine: Engine, http: httpx.Client) -> None:
    pid = perfis(engine)
    c, _ = cliente(engine)
    _, botoes = avaliar(engine, pid, TEXTO_COLADO, c, http)
    assert botoes[2].url is not None and botoes[2].url.startswith("https://www.google.com/search")


@respx.mock
def test_pagina_que_nao_e_vaga_pede_o_texto(engine: Engine, http: httpx.Client) -> None:
    respx.get(URL).respond(200, html="<p>Faça login para continuar</p>")
    pid = perfis(engine)
    c, msgs = cliente(engine, dados(eh_vaga=False, titulo=""))
    texto, botoes = avaliar(engine, pid, URL, c, http)
    assert "cole aqui" in texto and botoes == []
    assert len(msgs.chamadas) == 1  # não chegou a pontuar


def test_avisa_quando_os_filtros_descartariam(engine: Engine, http: httpx.Client) -> None:
    pid = perfis(engine, Filtros(palavras_bloqueadas=["estágio"]))
    c, _ = cliente(engine, dados(titulo="Estágio em Cloud", encerrada=True))
    texto, _ = avaliar(engine, pid, TEXTO_COLADO, c, http)
    assert "palavra bloqueada" in texto and "encerradas" in texto


def test_vaga_avulsa_nao_entra_na_rodada_dos_outros(engine: Engine, http: httpx.Client) -> None:
    pid = perfis(engine)
    c, _ = cliente(engine)
    avaliar(engine, pid, TEXTO_COLADO, c, http)
    with sessao(engine) as s:
        resumo = executar_rodada(s, engine, [], repo.listar_perfis(s), AGORA)
    assert all(p.avaliadas == 0 for p in resumo.perfis)


def test_sem_chave_do_claude(engine: Engine) -> None:
    pid = perfis(engine)
    texto, botoes = acoes.avaliar_no_chat(engine, Settings(), pid, TEXTO_COLADO)
    assert "ANTHROPIC_API_KEY" in texto and botoes == []


# --- bot ----------------------------------------------------------------------


def test_agrupador_junta_mensagens_cortadas_pelo_telegram() -> None:
    processadas: list[tuple[int, str]] = []

    async def processar(chat_id: int, texto: str) -> None:
        processadas.append((chat_id, texto))

    async def cenario(adicionar: Callable[[int, str], None]) -> None:
        adicionar(1, "parte 1")
        adicionar(2, "outro chat")
        await asyncio.sleep(0.01)
        adicionar(1, "parte 2")
        await asyncio.sleep(0.2)

    async def rodar() -> None:
        agrupador = Agrupador(processar, espera_s=0.05)
        await cenario(agrupador.adicionar)

    asyncio.run(rodar())
    assert sorted(processadas) == [(1, "parte 1\nparte 2"), (2, "outro chat")]
