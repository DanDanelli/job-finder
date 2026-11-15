import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine

from job_finder.connectors import base
from job_finder.connectors.remotive import URL, Remotive
from job_finder.quota import Quota, QuotaEsgotada

FIXTURE = Path(__file__).parent / "fixtures" / "remotive" / "remote_jobs.json"
HOJE = date(2026, 11, 15)


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
        yield router


def test_remotive_converte_a_resposta(engine: Engine, api: respx.MockRouter) -> None:
    with base.cliente_http() as http:
        vagas = Remotive(http).buscar([], Quota(engine, "remotive", 4, HOJE))

    assert len(vagas) == 6
    v = next(v for v in vagas if v.id_fonte == "2091137")
    assert v.fonte == "remotive"
    assert v.titulo == "Senior Data Engineer"
    assert v.empresa == "Lemon.io"
    assert v.local == "LATAM, Europe, USA, Canada, APAC"
    assert v.modalidade == "remote"
    assert v.tipo_contrato == "full_time"
    assert str(v.url).startswith("https://remotive.com/")  # termos: linkar o Remotive
    assert v.publicada_em == datetime(2026, 11, 11, 18, 39, 59)
    assert v.descricao and "<" not in v.descricao  # HTML removido
    assert v.dados_brutos["category"] == "Data and Analytics"

    sem_empresa_com_espaco = next(v for v in vagas if v.id_fonte == "1680495")
    assert sem_empresa_com_espaco.empresa == "Coalition Technologies"


def test_remotive_envia_user_agent_e_consome_quota(engine: Engine, api: respx.MockRouter) -> None:
    quota = Quota(engine, "remotive", 4, HOJE)
    with base.cliente_http() as http:
        Remotive(http).buscar([], quota)
    assert api.calls.last.request.headers["User-Agent"].startswith("job-finder/")
    assert quota.usadas_hoje() == 1


def test_remotive_respeita_a_quota(engine: Engine, api: respx.MockRouter) -> None:
    quota = Quota(engine, "remotive", 1, HOJE)
    quota.consumir()
    with base.cliente_http() as http, pytest.raises(QuotaEsgotada):
        Remotive(http).buscar([], quota)
    assert not api.calls  # nem chegou a chamar a API


def test_quota_por_dia_e_por_conector(engine: Engine) -> None:
    Quota(engine, "a", 2, HOJE).consumir()
    Quota(engine, "a", 2, HOJE).consumir()
    with pytest.raises(QuotaEsgotada):
        Quota(engine, "a", 2, HOJE).consumir()
    assert Quota(engine, "b", 2, HOJE).disponivel() == 2
    assert Quota(engine, "a", 2, date(2026, 11, 16)).disponivel() == 2
    assert Quota(engine, "a", None, HOJE).disponivel() is None


def test_registro_de_conectores() -> None:
    assert "remotive" in base.disponiveis()
    with pytest.raises(ValueError, match="desconhecido"), base.cliente_http() as http:
        base.criar("nao-existe", http)


def test_horario_da_quota_usa_a_data_utc() -> None:
    # A rodada passa agora.date() com agora em UTC; a quota é diária em UTC.
    assert datetime(2026, 11, 15, 23, 30, tzinfo=UTC).date() == HOJE
