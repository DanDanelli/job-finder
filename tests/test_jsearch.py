"""Conector JSearch e quota mensal, sem rede."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base
from job_finder.connectors.jsearch import URL, JSearch
from job_finder.domain import Query, Salario
from job_finder.pipeline.normalize import normalizar
from job_finder.quota import Quota, QuotaEsgotada

FIXTURE = Path(__file__).parent / "fixtures" / "jsearch" / "search.json"
HOJE = date(2026, 11, 15)
QUERIES = [Query(termo="Cloud Architect"), Query(termo="Platform Engineer"), Query(termo="SRE")]


def settings(**kw: object) -> Settings:
    return Settings.model_validate({"jsearch_api_key": "chave-teste", **kw})


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
        yield router


def quota(engine: Engine, hoje: date = HOJE) -> Quota:
    return Quota(engine, "jsearch", 6, hoje, limite_mensal=190)


def test_converte_a_resposta(engine: Engine, api: respx.MockRouter) -> None:
    with base.cliente_http() as http:
        vagas = JSearch(http, settings(jsearch_buscas_por_rodada=1)).buscar(QUERIES, quota(engine))

    assert [v.id_fonte for v in vagas] == ["abc123==", "def456=="]  # a sem link é ignorada
    v = vagas[0]
    assert (v.fonte, v.titulo, v.empresa) == (
        "jsearch",
        "Arquiteto de Soluções AWS Sênior",
        "Acme Tecnologia",
    )
    assert str(v.url) == "https://br.linkedin.com/jobs/view/arquiteto-123"
    assert str(v.url_candidatura) == "https://acme.example/careers/42"
    assert v.publicada_em == datetime(2026, 11, 14, 10, tzinfo=UTC)
    assert v.salario == Salario(minimo=20000, maximo=28000, moeda="BRL", periodo="mes")
    assert v.dados_brutos["publisher"] == "LinkedIn"
    normalizada = normalizar(v)
    assert normalizada.pais == "BR" and normalizada.tipo_contrato == "tempo_integral"

    remota = normalizar(vagas[1])
    assert remota.modalidade == "remoto" and remota.tipo_contrato == "contrato"


def test_envia_chave_pais_e_uma_pagina(engine: Engine, api: respx.MockRouter) -> None:
    with base.cliente_http() as http:
        JSearch(http, settings(jsearch_buscas_por_rodada=1)).buscar(QUERIES, quota(engine))
    req = api.calls.last.request
    assert req.headers["x-rapidapi-key"] == "chave-teste"
    assert req.headers["x-rapidapi-host"] == "jsearch.p.rapidapi.com"
    assert dict(req.url.params) == {
        "query": "Cloud Architect",
        "country": "br",
        "page": "1",
        "num_pages": "1",
        "date_posted": "3days",
    }


def test_rodizio_entre_rodadas(engine: Engine, api: respx.MockRouter) -> None:
    conector_settings = settings(jsearch_buscas_por_rodada=2, jsearch_limite_diario=None)
    with base.cliente_http() as http:
        for _ in range(3):
            JSearch(http, conector_settings).buscar(QUERIES, quota(engine))
    termos = [c.request.url.params["query"] for c in api.calls]
    assert termos == [
        "Cloud Architect", "Platform Engineer",
        "SRE", "Cloud Architect",
        "Platform Engineer", "SRE",
    ]  # fmt: skip


def test_query_com_pais_proprio(engine: Engine) -> None:
    j = JSearch(httpx.Client(), settings(jsearch_paises=["br", "pt"]))
    pares = j.combinacoes([Query(termo="A"), Query(termo="B", paises=["ES"])])
    assert pares == [("A", "br"), ("A", "pt"), ("B", "es")]


def test_sem_chave_vira_erro_do_conector(engine: Engine) -> None:
    with base.cliente_http() as http, pytest.raises(RuntimeError, match="JSEARCH_API_KEY"):
        JSearch(http, Settings()).buscar(QUERIES, quota(engine))


def test_status_de_erro_da_api(engine: Engine) -> None:
    with respx.mock:
        respx.get(URL).respond(200, json={"status": "ERROR", "error": {"message": "x"}})
        with base.cliente_http() as http, pytest.raises(RuntimeError, match="ERROR"):
            JSearch(http, settings()).buscar(QUERIES, quota(engine))


def test_registrado() -> None:
    assert "jsearch" in base.disponiveis()


# --- quota mensal -------------------------------------------------------------


def test_quota_mensal(engine: Engine) -> None:
    for dia in (1, 2, 3):
        Quota(engine, "j", None, date(2026, 11, dia), limite_mensal=3).consumir()
    with pytest.raises(QuotaEsgotada, match="3/mês"):
        Quota(engine, "j", None, date(2026, 11, 20), limite_mensal=3).consumir()
    # Virada do mês zera; o diário continua valendo junto.
    dezembro = Quota(engine, "j", 1, date(2026, 12, 1), limite_mensal=3)
    assert dezembro.usadas_no_mes() == 0 and dezembro.disponivel() == 1


def test_digest_mostra_o_site_de_origem() -> None:
    from job_finder.db.models import VagaFonteDB
    from job_finder.telegram.digest import _nome_fonte

    def fonte(nome: str, **dados: str) -> VagaFonteDB:
        return VagaFonteDB(fonte=nome, id_fonte="1", url="https://x.example", dados_brutos=dados)

    assert _nome_fonte(fonte("jsearch", publisher="LinkedIn")) == "LinkedIn (Google for Jobs)"
    assert _nome_fonte(fonte("remotive")) == "Remotive"
    assert _nome_fonte(fonte("manual")) is None
