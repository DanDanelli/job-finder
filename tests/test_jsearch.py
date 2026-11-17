"""JSearch connector and monthly quota, without network."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base
from job_finder.connectors.jsearch import URL, JSearch, _stable_id
from job_finder.domain import Query, Salary
from job_finder.pipeline.normalize import normalize
from job_finder.quota import Quota, QuotaExhausted

FIXTURE = Path(__file__).parent / "fixtures" / "jsearch" / "search.json"
TODAY = date(2026, 11, 15)
QUERIES = [Query(term="Cloud Architect"), Query(term="Platform Engineer"), Query(term="SRE")]


def settings(**kw: object) -> Settings:
    return Settings.model_validate({"jsearch_api_key": "test-key", **kw})


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
        yield router


def quota(engine: Engine, today: date = TODAY) -> Quota:
    return Quota(engine, "jsearch", 6, today, monthly_limit=190)


NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def connector(http: httpx.Client, **kw: object) -> JSearch:
    return JSearch(http, settings(**kw), clock=lambda: NOW)


def test_converts_the_real_response(engine: Engine, api: respx.MockRouter) -> None:
    with base.http_client() as http:
        jobs = connector(http, jsearch_searches_per_run=1).search(QUERIES, quota(engine))

    linkedin, glassdoor, floripa = jobs
    assert linkedin.source == "jsearch"
    assert linkedin.source_id == "mlnvcI2qJnU3DPq2AAAAAA=="  # Google's docid, stable
    assert linkedin.title == "InfoSec Architecture/Cloud Specialist"
    assert linkedin.company == "RHI Magnesita"
    assert str(linkedin.url).startswith("https://br.linkedin.com/jobs/view/")
    assert linkedin.apply_url is None  # no direct option
    assert linkedin.posted_at == NOW - timedelta(days=19)  # "há 19 dias"
    assert linkedin.raw_data["publisher"] == "LinkedIn"
    n = normalize(linkedin)
    assert n.country == "BR" and n.contract_type == "full_time"

    assert glassdoor.work_mode == "remote" and normalize(glassdoor).work_mode == "remote"
    assert floripa.posted_at == NOW - timedelta(hours=22)
    assert normalize(floripa).contract_type == "contract"  # "Prestador de serviços"
    assert normalize(floripa).country == "BR"  # "Santa Catarina" + job_country


def test_job_id_is_stable_across_searches() -> None:
    import base64

    def job_id(token: str) -> str:
        return (
            base64.urlsafe_b64encode(f"abcDEF123-_AAAAAA==:{token}".encode()).decode().rstrip("=")
        )

    assert _stable_id(job_id("search1")) == _stable_id(job_id("search2")) == "abcDEF123-_AAAAAA=="
    assert len(_stable_id("not-base64!!")) == 32


@pytest.mark.parametrize(
    ("text", "age"),
    [
        ("há 22 horas", timedelta(hours=22)),
        ("há 1 dia", timedelta(days=1)),
        ("há 2 semanas", timedelta(weeks=2)),
        ("há 1 mês", timedelta(days=30)),
        ("há 30+ dias", timedelta(days=30)),
        ("3 days ago", timedelta(days=3)),
    ],
)
def test_relative_date(text: str, age: timedelta) -> None:
    assert connector(httpx.Client())._posted_at({"job_posted_at": text}) == NOW - age
    assert connector(httpx.Client())._posted_at({"job_posted_at": None}) is None


def test_salary_when_present() -> None:
    job = {
        "job_id": "x",
        "job_title": "Arquiteto",
        "job_apply_link": "https://x.example",
        "job_min_salary": 20000,
        "job_max_salary": 28000,
        "job_salary_period": "MONTH",
        "job_salary_string": "R$ 20 mil a R$ 28 mil por mês",
    }
    j = connector(httpx.Client())._convert(job)
    assert j is not None
    assert j.salary == Salary(minimum=20000, maximum=28000, currency=None, period="month")
    assert connector(httpx.Client())._convert({"job_id": "y", "job_title": "No link"}) is None


def test_sends_key_country_and_one_page(engine: Engine, api: respx.MockRouter) -> None:
    with base.http_client() as http:
        connector(http, jsearch_searches_per_run=1).search(QUERIES, quota(engine))
    req = api.calls.last.request
    assert req.headers["x-api-key"] == "test-key"
    assert dict(req.url.params) == {
        "query": "Cloud Architect",
        "country": "br",
        "num_pages": "1",
        "date_posted": "3days",
    }


def test_rotation_between_runs(engine: Engine, api: respx.MockRouter) -> None:
    connector_settings = settings(jsearch_searches_per_run=2, jsearch_daily_limit=None)
    with base.http_client() as http:
        for _ in range(3):
            JSearch(http, connector_settings).search(QUERIES, quota(engine))
    terms = [c.request.url.params["query"] for c in api.calls]
    assert terms == [
        "Cloud Architect", "Platform Engineer",
        "SRE", "Cloud Architect",
        "Platform Engineer", "SRE",
    ]  # fmt: skip


def test_query_with_its_own_countries(engine: Engine) -> None:
    j = JSearch(httpx.Client(), settings(jsearch_countries=["br", "pt"]))
    pairs = j.pairs([Query(term="A"), Query(term="B", countries=["ES"])])
    assert pairs == [("A", "br"), ("A", "pt"), ("B", "es")]


def test_missing_key_is_a_connector_error(engine: Engine) -> None:
    with base.http_client() as http, pytest.raises(RuntimeError, match="JSEARCH_API_KEY"):
        JSearch(http, Settings()).search(QUERIES, quota(engine))


def test_api_error_status(engine: Engine) -> None:
    with respx.mock:
        respx.get(URL).respond(200, json={"status": "ERROR", "error": {"message": "x"}})
        with base.http_client() as http, pytest.raises(RuntimeError, match="ERROR"):
            JSearch(http, settings()).search(QUERIES, quota(engine))


def test_registered() -> None:
    assert "jsearch" in base.available()


# --- monthly quota ------------------------------------------------------------


def test_monthly_quota(engine: Engine) -> None:
    for day in (1, 2, 3):
        Quota(engine, "j", None, date(2026, 11, day), monthly_limit=3).consume()
    with pytest.raises(QuotaExhausted, match="3/month"):
        Quota(engine, "j", None, date(2026, 11, 20), monthly_limit=3).consume()
    # A new month resets it; the daily limit still applies alongside.
    december = Quota(engine, "j", 1, date(2026, 12, 1), monthly_limit=3)
    assert december.used_this_month() == 0 and december.available() == 1


def test_digest_shows_the_origin_site() -> None:
    from job_finder.db.models import JobSourceDB
    from job_finder.telegram.digest import _source_name

    def source(name: str, **data: str) -> JobSourceDB:
        return JobSourceDB(source=name, source_id="1", url="https://x.example", raw_data=data)

    assert _source_name(source("jsearch", publisher="LinkedIn")) == "LinkedIn (Google for Jobs)"
    assert _source_name(source("remotive")) == "Remotive"
    assert _source_name(source("manual")) is None


def test_jsearch_credit_links_to_google_for_jobs() -> None:
    from job_finder.db.models import JobSourceDB
    from job_finder.telegram.digest import _source_link

    def source(name: str, **data: str) -> JobSourceDB:
        return JobSourceDB(source=name, source_id="1", url="https://x.example", raw_data=data)

    google = "https://www.google.com/search?q=jobs&udm=8#vhid=1"
    assert _source_link(source("jsearch", publisher="WhatJobs", google_link=google)) == google
    assert _source_link(source("jsearch", google_link="javascript:alert(1)")) is None
    assert _source_link(source("jsearch")) is None
    assert _source_link(source("adzuna")) == "https://www.adzuna.co.uk"
    assert _source_link(source("remotive")) is None
    assert _source_link(source("manual")) is None
