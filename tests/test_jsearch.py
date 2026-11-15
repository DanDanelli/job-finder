"""Conector JSearch e quota mensal, sem rede."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base
from job_finder.connectors.jsearch import URL, JSearch, _id_estavel
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


AGORA = datetime(2026, 11, 15, 12, tzinfo=UTC)


def conector(http: httpx.Client, **kw: object) -> JSearch:
    return JSearch(http, settings(**kw), relogio=lambda: AGORA)


def test_converte_a_resposta_real(engine: Engine, api: respx.MockRouter) -> None:
    with base.cliente_http() as http:
        vagas = conector(http, jsearch_buscas_por_rodada=1).buscar(QUERIES, quota(engine))

    linkedin, glassdoor, floripa = vagas
    assert linkedin.fonte == "jsearch"
    assert linkedin.id_fonte == "mlnvcI2qJnU3DPq2AAAAAA=="  # docid do Google, estável
    assert linkedin.titulo == "InfoSec Architecture/Cloud Specialist"
    assert linkedin.empresa == "RHI Magnesita"
    assert str(linkedin.url).startswith("https://br.linkedin.com/jobs/view/")
    assert linkedin.url_candidatura is None  # nenhuma opção direta
    assert linkedin.publicada_em == AGORA - timedelta(days=19)  # "há 19 dias"
    assert linkedin.dados_brutos["publisher"] == "LinkedIn"
    n = normalizar(linkedin)
    assert n.pais == "BR" and n.tipo_contrato == "tempo_integral"

    assert glassdoor.modalidade == "remote" and normalizar(glassdoor).modalidade == "remoto"
    assert floripa.publicada_em == AGORA - timedelta(hours=22)
    assert normalizar(floripa).tipo_contrato == "contrato"  # "Prestador de serviços"
    assert normalizar(floripa).pais == "BR"  # "Santa Catarina" + job_country


def test_job_id_estavel_entre_buscas() -> None:
    import base64

    def job_id(token: str) -> str:
        return (
            base64.urlsafe_b64encode(f"abcDEF123-_AAAAAA==:{token}".encode()).decode().rstrip("=")
        )

    assert _id_estavel(job_id("busca1")) == _id_estavel(job_id("busca2")) == "abcDEF123-_AAAAAA=="
    assert len(_id_estavel("nao-e-base64!!")) == 32


@pytest.mark.parametrize(
    ("texto", "idade"),
    [
        ("há 22 horas", timedelta(hours=22)),
        ("há 1 dia", timedelta(days=1)),
        ("há 2 semanas", timedelta(weeks=2)),
        ("há 1 mês", timedelta(days=30)),
        ("há 30+ dias", timedelta(days=30)),
        ("3 days ago", timedelta(days=3)),
    ],
)
def test_data_relativa(texto: str, idade: timedelta) -> None:
    assert conector(httpx.Client())._publicada({"job_posted_at": texto}) == AGORA - idade
    assert conector(httpx.Client())._publicada({"job_posted_at": None}) is None


def test_salario_quando_vem() -> None:
    job = {
        "job_id": "x",
        "job_title": "Arquiteto",
        "job_apply_link": "https://x.example",
        "job_min_salary": 20000,
        "job_max_salary": 28000,
        "job_salary_period": "MONTH",
        "job_salary_string": "R$ 20 mil a R$ 28 mil por mês",
    }
    v = conector(httpx.Client())._converter(job)
    assert v is not None
    assert v.salario == Salario(minimo=20000, maximo=28000, moeda=None, periodo="mes")
    assert conector(httpx.Client())._converter({"job_id": "y", "job_title": "Sem link"}) is None


def test_envia_chave_pais_e_uma_pagina(engine: Engine, api: respx.MockRouter) -> None:
    with base.cliente_http() as http:
        conector(http, jsearch_buscas_por_rodada=1).buscar(QUERIES, quota(engine))
    req = api.calls.last.request
    assert req.headers["x-api-key"] == "chave-teste"
    assert dict(req.url.params) == {
        "query": "Cloud Architect",
        "country": "br",
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
