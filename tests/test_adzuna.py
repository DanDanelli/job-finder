"""Adzuna connector, without network."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base
from job_finder.connectors.adzuna import Adzuna
from job_finder.domain import ContractType, Query, Salary
from job_finder.pipeline.normalize import normalize
from job_finder.quota import Quota

FIXTURE = Path(__file__).parent / "fixtures" / "adzuna" / "search.json"
TODAY = date(2026, 11, 15)
SEARCH = "https://api.adzuna.com/v1/api/jobs/br/search/1"


def settings(**kw: object) -> Settings:
    return Settings.model_validate(
        {"adzuna_app_id": "id-test", "adzuna_app_key": "key-secret", **kw}
    )


def quota(engine: Engine) -> Quota:
    return Quota(engine, "adzuna", 10, TODAY, monthly_limit=250)


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://api.adzuna.com/v1/api/jobs/").respond(
            200, json=json.loads(FIXTURE.read_text())
        )
        yield router


def test_converts_the_response(engine: Engine, api: respx.MockRouter) -> None:
    with base.http_client() as http:
        jobs = Adzuna(http, settings()).search([Query(term="marketing")], quota(engine))

    assert len(jobs) == 2  # the one without a title is dropped
    j = next(j for j in jobs if j.source_id == "br:5012345678")
    assert j.title == "Coordenador de Marketing"  # highlight tags stripped
    assert j.company == "Acme Moda"
    assert j.location == "São Paulo, São Paulo, BR"
    assert str(j.url).startswith("https://www.adzuna.com.br/land/ad/")
    assert j.posted_at == datetime(2026, 11, 14, 10, tzinfo=UTC)
    assert j.salary == Salary(minimum=96000, maximum=120000, currency="BRL", period="year")
    assert j.raw_data["site"] == "https://www.adzuna.com.br"

    normalized = normalize(j)
    assert normalized.country == "BR"
    assert normalized.contract_type == ContractType.FULL_TIME


def test_predicted_salary_is_dropped(engine: Engine, api: respx.MockRouter) -> None:
    with base.http_client() as http:
        jobs = Adzuna(http, settings()).search([Query(term="marca")], quota(engine))
    j = next(j for j in jobs if j.source_id == "br:5012345679")
    assert j.salary is None
    assert j.location == "Brasil"  # already names the country


def test_sends_the_search_and_rotates_pairs(engine: Engine, api: respx.MockRouter) -> None:
    queries = [Query(term="Brand Manager"), Query(term="Marketing", countries=["GB", "PT"])]
    q = quota(engine)
    with base.http_client() as http:
        c = Adzuna(http, settings(adzuna_searches_per_run=1))
        # PT is not covered by Adzuna.
        assert c.pairs(queries) == [("Brand Manager", "br"), ("Marketing", "gb")]
        c.search(queries, q)
        c.search(queries, q)

    first, second = (call.request.url for call in api.calls)
    assert first.path == "/v1/api/jobs/br/search/1"
    assert second.path == "/v1/api/jobs/gb/search/1"
    assert first.params["what"] == "Brand Manager"
    assert first.params["sort_by"] == "date"
    assert first.params["app_key"] == "key-secret"
    assert q.used_this_month() == 2


@pytest.mark.parametrize("status", [401, 429, 500])
def test_errors_do_not_leak_the_key(engine: Engine, status: int) -> None:
    with respx.mock() as router:
        router.get(url__startswith=SEARCH).respond(status)
        with base.http_client() as http, pytest.raises(RuntimeError) as e:
            Adzuna(http, settings()).search([Query(term="x")], quota(engine))
    assert str(e.value) == f"Adzuna: HTTP {status} (br)"
    assert "key-secret" not in str(e.value) and e.value.__cause__ is None
    assert e.value.__suppress_context__


def test_one_failed_search_keeps_the_others(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with respx.mock() as router:
        router.get(url__startswith=SEARCH, params={"what": "broken"}).respond(503)
        router.get(url__startswith=SEARCH).respond(200, json=json.loads(FIXTURE.read_text()))
        with base.http_client() as http:
            jobs = Adzuna(http, settings()).search(
                [Query(term="broken"), Query(term="marketing")], quota(engine)
            )
    assert len(jobs) == 2
    assert "Adzuna: HTTP 503 (br), search 'broken' skipped" in caplog.text
    assert "key-secret" not in caplog.text


def test_network_error_does_not_leak_the_key(engine: Engine) -> None:
    with respx.mock() as router:
        router.get(url__startswith=SEARCH).mock(side_effect=httpx.ConnectError("boom"))
        with base.http_client() as http, pytest.raises(RuntimeError) as e:
            Adzuna(http, settings()).search([Query(term="x")], quota(engine))
    assert "key-secret" not in str(e.value)


def test_without_keys(engine: Engine) -> None:
    with base.http_client() as http, pytest.raises(RuntimeError, match="ADZUNA_APP_ID"):
        Adzuna(http, Settings()).search([Query(term="x")], quota(engine))


def test_real_brazil_response(engine: Engine) -> None:
    """Recorded on 2026-11-15: no salary, company sometimes missing, a 500-char excerpt."""
    real = json.loads((FIXTURE.parent / "real_br.json").read_text())
    with respx.mock() as router:
        router.get(url__startswith=SEARCH).respond(200, json=real)
        with base.http_client() as http:
            jobs = Adzuna(http, settings()).search([Query(term="Brand Manager")], quota(engine))

    assert len(jobs) == 3
    assert all(normalize(j).country == "BR" and j.salary is None for j in jobs)
    assert {j.location for j in jobs} == {"São Paulo, Estado de São Paulo, BR", "Brasil"}
    assert all(str(j.url).startswith("https://www.adzuna.com.br/details/") for j in jobs)
