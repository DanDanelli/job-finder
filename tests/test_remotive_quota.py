import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine

from job_finder.connectors import base
from job_finder.connectors.remotive import URL, Remotive
from job_finder.quota import Quota, QuotaExhausted

FIXTURE = Path(__file__).parent / "fixtures" / "remotive" / "remote_jobs.json"
TODAY = date(2026, 11, 15)


@pytest.fixture
def api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(URL).mock(return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text())))
        yield router


def test_remotive_converts_the_response(engine: Engine, api: respx.MockRouter) -> None:
    with base.http_client() as http:
        jobs = Remotive(http).search([], Quota(engine, "remotive", 4, TODAY))

    assert len(jobs) == 6
    j = next(j for j in jobs if j.source_id == "2091137")
    assert j.source == "remotive"
    assert j.title == "Senior Data Engineer"
    assert j.company == "Lemon.io"
    assert j.location == "LATAM, Europe, USA, Canada, APAC"
    assert j.work_mode == "remote"
    assert j.contract_type == "full_time"
    assert str(j.url).startswith("https://remotive.com/")  # terms: link to Remotive
    assert j.posted_at == datetime(2026, 11, 11, 18, 39, 59)
    assert j.description and "<" not in j.description  # HTML stripped
    assert j.raw_data["category"] == "Data and Analytics"

    padded_company = next(j for j in jobs if j.source_id == "1680495")
    assert padded_company.company == "Coalition Technologies"


def test_remotive_sends_user_agent_and_uses_quota(engine: Engine, api: respx.MockRouter) -> None:
    quota = Quota(engine, "remotive", 4, TODAY)
    with base.http_client() as http:
        Remotive(http).search([], quota)
    assert api.calls.last.request.headers["User-Agent"].startswith("job-finder/")
    assert quota.used_today() == 1


def test_remotive_respects_the_quota(engine: Engine, api: respx.MockRouter) -> None:
    quota = Quota(engine, "remotive", 1, TODAY)
    quota.consume()
    with base.http_client() as http, pytest.raises(QuotaExhausted):
        Remotive(http).search([], quota)
    assert not api.calls  # never reached the API


def test_quota_per_day_and_per_connector(engine: Engine) -> None:
    Quota(engine, "a", 2, TODAY).consume()
    Quota(engine, "a", 2, TODAY).consume()
    with pytest.raises(QuotaExhausted):
        Quota(engine, "a", 2, TODAY).consume()
    assert Quota(engine, "b", 2, TODAY).available() == 2
    assert Quota(engine, "a", 2, date(2026, 11, 16)).available() == 2
    assert Quota(engine, "a", None, TODAY).available() is None


def test_connector_registry() -> None:
    assert "remotive" in base.available()
    with pytest.raises(ValueError, match="unknown"), base.http_client() as http:
        base.create("does-not-exist", http)


def test_quota_day_uses_the_utc_date() -> None:
    # The run passes now.date() with now in UTC; the quota is daily in UTC.
    assert datetime(2026, 11, 15, 23, 30, tzinfo=UTC).date() == TODAY
