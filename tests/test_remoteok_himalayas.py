"""Remote OK and Himalayas connectors, with recorded responses and no network."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base, himalayas, remoteok
from job_finder.domain import ContractType, Query, RawJob, Salary, WorkMode
from job_finder.pipeline.normalize import normalize
from job_finder.quota import Quota, QuotaExhausted

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 15)
NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def load(path: str) -> object:
    return json.loads((FIXTURES / path).read_text())


# --- Remote OK ------------------------------------------------------------------


@pytest.fixture
def remoteok_api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(remoteok.URL).respond(200, json=load("remoteok/api.json"))
        yield router


def remoteok_jobs(engine: Engine) -> dict[str, RawJob]:
    with base.http_client() as http:
        jobs = remoteok.RemoteOK(http).search([], Quota(engine, "remoteok", 4, TODAY))
    return {j.source_id: j for j in jobs}


def test_remoteok_skips_the_legal_notice_and_converts(
    engine: Engine, remoteok_api: respx.MockRouter
) -> None:
    jobs = remoteok_jobs(engine)
    assert len(jobs) == 6

    j = jobs["1137540"]
    assert j.source == "remoteok"
    assert (j.title, j.company, j.location) == ("Technical Account Manager", "Yuno", "Brazil")
    assert str(j.url).startswith("https://remoteok.com/remote-jobs/")  # terms: link back
    assert j.work_mode == "remote" and j.posted_at is not None
    assert j.description and "<" not in j.description


def test_remoteok_fixes_double_encoded_text(engine: Engine, remoteok_api: respx.MockRouter) -> None:
    assert remoteok_jobs(engine)["1137677"].location == "München"


def test_remoteok_salary_is_yearly_usd_and_implausible_values_are_dropped(
    engine: Engine, remoteok_api: respx.MockRouter
) -> None:
    jobs = remoteok_jobs(engine)
    assert jobs["1137700"].salary == Salary(
        minimum=60000, maximum=80000, currency="USD", period="year"
    )
    assert jobs["1137716"].salary is None  # "21": an hourly rate
    assert jobs["1137796"].salary is None  # 0 = not given


def test_remoteok_uses_the_quota(engine: Engine, remoteok_api: respx.MockRouter) -> None:
    quota = Quota(engine, "remoteok", 1, TODAY)
    quota.consume()
    with base.http_client() as http, pytest.raises(QuotaExhausted):
        remoteok.RemoteOK(http).search([], quota)
    assert not remoteok_api.calls


# --- Himalayas ------------------------------------------------------------------


@pytest.fixture
def himalayas_api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(himalayas.URL).respond(200, json=load("himalayas/search.json"))
        yield router


def connector(http: httpx.Client, **kw: object) -> himalayas.Himalayas:
    return himalayas.Himalayas(http, Settings.model_validate(kw), clock=lambda: NOW)


def quota(engine: Engine) -> Quota:
    return Quota(engine, "himalayas", 20, TODAY)


def test_himalayas_converts_the_response(engine: Engine, himalayas_api: respx.MockRouter) -> None:
    with base.http_client() as http:
        jobs = {j.source_id: j for j in connector(http).search([Query(term="x")], quota(engine))}

    j = jobs["avahi/jobs/aws-cloud-architect"]
    assert j.title == "AWS Cloud Architect"  # trailing nbsp stripped
    assert j.company == "Avahi"
    assert j.location == "Argentina, Brazil, Colombia, Guatemala, Mexico"
    # terms: apply through applicationLink, a Himalayas page
    assert str(j.url).startswith("https://himalayas.app/companies/avahi/jobs/")
    assert j.posted_at == datetime.fromtimestamp(1793534013, UTC)
    assert j.description and "<" not in j.description


def test_himalayas_drops_expired_jobs(engine: Engine, himalayas_api: respx.MockRouter) -> None:
    with base.http_client() as http:
        jobs = connector(http).search([Query(term="x")], quota(engine))
    assert len(jobs) == 4
    assert not any("canonical" in j.source_id for j in jobs)


def test_himalayas_salaries(engine: Engine, himalayas_api: respx.MockRouter) -> None:
    with base.http_client() as http:
        raw = connector(http).search([Query(term="x")], quota(engine))

    def salary(suffix: str) -> Salary | None:
        return next(j for j in raw if j.source_id.endswith(suffix)).salary

    assert salary("azure-cloud-architect") == Salary(
        minimum=5000, maximum=7000, currency="USD", period="month"
    )
    assert salary("8425408588") == Salary(minimum=40, maximum=60, currency="USD", period="hour")
    # weekly -> yearly
    assert salary("6422244912") == Salary(
        minimum=78000, maximum=None, currency="USD", period="year"
    )
    assert salary("avahi/jobs/aws-cloud-architect") is None


def test_himalayas_normalizes_to_remote_jobs_open_to_brazil(
    engine: Engine, himalayas_api: respx.MockRouter
) -> None:
    with base.http_client() as http:
        raw = connector(http).search([Query(term="x")], quota(engine))
    jobs = [normalize(r) for r in raw]
    assert all(j.work_mode == WorkMode.REMOTE for j in jobs)
    assert all("BR" in j.eligible_locations for j in jobs)
    assert {j.contract_type for j in jobs} <= {ContractType.FULL_TIME, ContractType.CONTRACT}


def test_himalayas_rotates_term_country_pairs_and_sends_the_key(
    engine: Engine, himalayas_api: respx.MockRouter
) -> None:
    queries = [Query(term="Cloud Architect"), Query(term="SRE", countries=["PT"])]
    q = quota(engine)
    with base.http_client() as http:
        c = connector(http, himalayas_api_key="hk-test", himalayas_searches_per_run=1)
        assert c.pairs(queries) == [("Cloud Architect", "BR"), ("SRE", "PT")]
        c.search(queries, q)
        c.search(queries, q)

    params = [dict(call.request.url.params) for call in himalayas_api.calls]
    assert params == [{"q": "Cloud Architect", "country": "BR"}, {"q": "SRE", "country": "PT"}]
    assert himalayas_api.calls.last.request.headers["Authorization"] == "Bearer hk-test"
    assert q.used_today() == 2


def test_himalayas_without_key_sends_no_authorization(
    engine: Engine, himalayas_api: respx.MockRouter
) -> None:
    with base.http_client() as http:
        connector(http, himalayas_searches_per_run=1).search([Query(term="x")], quota(engine))
    assert "Authorization" not in himalayas_api.calls.last.request.headers


def test_new_connectors_are_registered() -> None:
    assert {"remoteok", "himalayas"} <= set(base.available())
