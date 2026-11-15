"""We Work Remotely connector, with a recorded (trimmed) category feed and no network."""

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx
from defusedxml import EntitiesForbidden
from sqlalchemy import Engine

from job_finder.config import Settings
from job_finder.connectors import base, weworkremotely
from job_finder.domain import ContractType, Query, RawJob, WorkMode
from job_finder.pipeline.locations import is_eligible
from job_finder.pipeline.normalize import normalize
from job_finder.quota import Quota, QuotaExhausted

FEED = (Path(__file__).parent / "fixtures" / "weworkremotely" / "sales.xml").read_bytes()
TODAY = date(2026, 11, 15)
NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
CATEGORIES = ["remote-sales-and-marketing-jobs", "remote-design-jobs"]


@pytest.fixture
def feeds() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://weworkremotely\.com/categories/.*\.rss").respond(
            200, content=FEED, headers={"Content-Type": "application/rss+xml"}
        )
        yield router


def connector(
    http: httpx.Client, categories: list[str] = CATEGORIES
) -> weworkremotely.WeWorkRemotely:
    settings = Settings.model_validate({"weworkremotely_categories": categories})
    return weworkremotely.WeWorkRemotely(http, settings, clock=lambda: NOW)


def search(engine: Engine, limit: int = 33) -> dict[str, RawJob]:
    with base.http_client() as http:
        jobs = connector(http).search(
            [Query(term="x")], Quota(engine, "weworkremotely", limit, TODAY)
        )
    return {j.source_id: j for j in jobs}


def test_converts_the_feed(engine: Engine, feeds: respx.MockRouter) -> None:
    jobs = search(engine)

    j = jobs["pavago-seo-digital-marketing-specialist"]
    assert (j.company, j.title) == ("Pavago", "SEO & Digital Marketing Specialist")
    assert j.location == "Worldwide"
    assert (
        str(j.url)
        == "https://weworkremotely.com/remote-jobs/pavago-seo-digital-marketing-specialist"
    )
    assert j.posted_at is not None and j.posted_at.tzinfo is not None
    assert j.description and "<" not in j.description
    assert "Sales and Marketing" in j.tags


def test_one_request_per_category_and_duplicates_are_merged(
    engine: Engine, feeds: respx.MockRouter
) -> None:
    jobs = search(engine)
    assert len(feeds.calls) == 2  # same fixture served for both categories
    assert len(jobs) == 5  # 6 items, one expired


def test_expired_jobs_are_dropped(engine: Engine, feeds: respx.MockRouter) -> None:
    assert not any(k.endswith("-expired") for k in search(engine))


def test_country_flags_win_over_region(engine: Engine, feeds: respx.MockRouter) -> None:
    jobs = {k: normalize(r) for k, r in search(engine).items()}

    # region "Anywhere in the World", but the flags say only the US
    zanda = jobs["zanda-health-performance-marketing-paid-media"]
    assert zanda.eligible_locations == ["US"]
    assert is_eligible(zanda.eligible_locations, ["BR"]) is False

    # a long flag list with Brazil, Peru (a Brazilian state code too) and Bosnia
    icarus = next(j for k, j in jobs.items() if k.startswith("icarus-digital-marketing"))
    assert {"BR", "PE", "BA", "AR"} <= set(icarus.eligible_locations)
    assert is_eligible(icarus.eligible_locations, ["BR"]) is True

    assert jobs["advantage-hq-senior-video-editor"].eligible_locations == ["LATAM"]
    assert jobs["pavago-seo-digital-marketing-specialist"].eligible_locations == ["WORLDWIDE"]


def test_normalizes_to_remote_jobs(engine: Engine, feeds: respx.MockRouter) -> None:
    jobs = [normalize(r) for r in search(engine).values()]
    assert all(j.work_mode == WorkMode.REMOTE for j in jobs)
    assert {j.contract_type for j in jobs} <= {ContractType.FULL_TIME, ContractType.CONTRACT}


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ("🇧🇴 Bolivia (Plurinational State of), 🇧🇷 Brazil, and 🇵🇪 Peru", ["BO", "BR", "PE"]),
        ("🇰🇷 Korea (Republic of), 🇹🇷 Türkiye, 🇧🇦 Bosnia and Herzegovina", ["KR", "TR", "BA"]),
        ("🇬🇧 United Kingdom of Great Britain and Northern Ireland", ["GB"]),
        ("🇻🇮 Virgin Islands, U.S.", ["VI"]),
    ],
)
def test_flags_become_parseable_places(flags: str, expected: list[str]) -> None:
    location = weworkremotely._location(flags, "Anywhere in the World")
    assert (
        normalize(
            RawJob(source="x", source_id="1", title="t", url="https://x.example", location=location)
        ).eligible_locations
        == expected
    )


def test_quota_runs_out_midway_keeps_what_was_downloaded(
    engine: Engine, feeds: respx.MockRouter
) -> None:
    assert len(search(engine, limit=1)) == 5
    assert len(feeds.calls) == 1


def test_no_quota_left_raises(engine: Engine, feeds: respx.MockRouter) -> None:
    quota = Quota(engine, "weworkremotely", 1, TODAY)
    quota.consume()
    with base.http_client() as http, pytest.raises(QuotaExhausted):
        connector(http).search([], quota)
    assert not feeds.calls


def test_hostile_xml_is_rejected(engine: Engine) -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
        b"<rss><channel><item><title>&b;</title></item></channel></rss>"
    )
    with respx.mock() as router, base.http_client() as http:
        router.get(url__regex=r".*").respond(200, content=bomb)
        with pytest.raises(EntitiesForbidden):
            connector(http, ["x"]).search([], Quota(engine, "weworkremotely", 5, TODAY))


def test_is_registered() -> None:
    assert "weworkremotely" in base.available()
