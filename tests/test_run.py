"""End-to-end pipeline with fake connectors (no network)."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from job_finder.cli import app as cli_app
from job_finder.connectors.remotive import URL
from job_finder.db import repo
from job_finder.db.models import JobDB, JobSourceDB, MatchDB, RunDB
from job_finder.db.session import session, upgrade
from job_finder.domain import Filters, MatchStatus, Profile, Query, RawJob
from job_finder.pipeline.run import RunSummary, execute_run
from job_finder.quota import Quota

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


class Fake:
    daily_limit: int | None = None

    def __init__(self, name: str, jobs: list[RawJob]) -> None:
        self.name = name
        self.jobs = jobs
        self.received_queries: list[Query] = []

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        quota.consume()
        self.received_queries = list(queries)
        return self.jobs


class Broken:
    name = "broken"
    daily_limit: int | None = None

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        raise httpx.ConnectError("no network")


def raw(source: str, source_id: str, **kw: object) -> RawJob:
    base: dict[str, object] = {
        "source": source,
        "source_id": source_id,
        "title": "Cloud Architect",
        "company": "Acme",
        "location": "Europe",
        "work_mode": "remote",
        "url": f"https://{source}.example/{source_id}",
        "posted_at": NOW - timedelta(days=1),
    }
    return RawJob.model_validate(base | kw)


def create_profiles(engine: Engine) -> None:
    with session(engine) as s:
        repo.save_profile(
            s,
            Profile(
                name="alice",
                queries=[Query(term="Cloud Architect"), Query(term="DevOps", active=False)],
                filters=Filters(remote_from=["PT"]),
            ),
        )
        repo.save_profile(
            s,
            Profile(
                name="bob",
                queries=[Query(term="Brand Marketing"), Query(term="cloud architect")],
                filters=Filters(remote_from=["BR"], blocked_keywords=["architect"]),
            ),
        )


def run(engine: Engine, connectors: Sequence[object]) -> RunSummary:
    with session(engine) as s:
        return execute_run(s, engine, connectors, repo.list_profiles(s), NOW)  # type: ignore[arg-type]


def count(engine: Engine, model: type) -> int:
    with session(engine) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_full_run(engine: Engine) -> None:
    create_profiles(engine)
    source_a = Fake(
        "a",
        [
            raw("a", "1"),
            raw("a", "2", title="Data Engineer", location="USA"),
            raw("a", "3", title="Old Job", posted_at=NOW - timedelta(days=60)),
        ],
    )
    # The same job "a/1" posted by another source, with title and company spelled differently.
    source_b = Fake("b", [raw("b", "x", title="Cloud Architect (m/f/d)", company="ACME GmbH")])

    summary = run(engine, [source_a, source_b])

    assert summary.errors == []
    assert summary.collected == 4
    assert summary.new == 3  # a/1 and b/x are the same job
    assert count(engine, JobDB) == 3
    assert count(engine, JobSourceDB) == 4

    # Active queries, deduplicated (a different case counts as the same).
    assert sorted(q.term for q in source_a.received_queries) == [
        "Brand Marketing",
        "Cloud Architect",
    ]

    alice, bob = summary.profiles
    assert [j.title for j in alice.approved] == ["Cloud Architect"]
    assert alice.discards == {"location": 1}  # "USA" does not accept residents of PT
    assert bob.approved == []
    assert bob.discards == {"location": 2}  # Europe and the US do not accept residents of BR
    # The 60-day-old job is not even a candidate.
    assert alice.evaluated == 2

    with session(engine) as s:
        matches = s.scalars(select(MatchDB)).all()
        assert [(m.profile.name, m.status) for m in matches] == [("alice", MatchStatus.PENDING)]
        run_row = s.scalars(select(RunDB)).one()
        assert (run_row.status, run_row.jobs_new, run_row.matches_new) == ("ok", 3, 1)
        assert run_row.finished_at is not None


def test_second_run_does_not_duplicate(engine: Engine) -> None:
    create_profiles(engine)
    source = Fake("a", [raw("a", "1"), raw("a", "2", title="SRE")])
    run(engine, [source])
    summary = run(engine, [source])

    assert summary.new == 0
    assert count(engine, JobDB) == 2
    assert count(engine, JobSourceDB) == 2
    assert count(engine, MatchDB) == 2  # one match per job for alice, no repeats
    assert summary.profiles[0].evaluated == 0


def test_broken_source_does_not_take_down_the_run(engine: Engine) -> None:
    create_profiles(engine)
    summary = run(engine, [Broken(), Fake("a", [raw("a", "1")])])

    assert summary.collected == 1
    assert summary.errors == ["broken: ConnectError: no network"]
    with session(engine) as s:
        assert s.scalars(select(RunDB)).one().status == "partial"


def test_exhausted_quota_becomes_a_warning(engine: Engine) -> None:
    create_profiles(engine)
    source = Fake("a", [raw("a", "1")])
    source.daily_limit = 1
    Quota(engine, "a", 1, NOW.date()).consume()

    summary = run(engine, [source])
    assert summary.collected == 0
    assert summary.errors == ["a quota exhausted (1/day)"]
    with session(engine) as s:
        assert s.scalars(select(RunDB)).one().status == "error"


# --- CLI ----------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "remotive" / "remote_jobs.json"
EXAMPLE = Path(__file__).parents[1] / "profiles" / "example.yaml"


@pytest.fixture
def cli_db(tmp_path: Path) -> Engine:
    from job_finder.config import get_settings
    from job_finder.db.session import make_engine

    url = get_settings().resolved_database_url
    upgrade(url)
    return make_engine(url)


@pytest.fixture
def remotive_api() -> respx.MockRouter:
    data = json.loads(FIXTURE.read_text())
    # Recent dates so they pass the age filter.
    for job in data["jobs"]:
        job["publication_date"] = (datetime.now(UTC) - timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
    with respx.mock() as router:
        router.get(URL).mock(return_value=httpx.Response(200, json=data))
        yield router


def test_cli_dry_run_writes_nothing_but_uses_quota(
    cli_db: Engine, remotive_api: respx.MockRouter
) -> None:
    runner = CliRunner()
    assert runner.invoke(cli_app, ["profile", "load", str(EXAMPLE)]).exit_code == 0

    result = runner.invoke(cli_app, ["run", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Collected: 6" in result.output
    assert "dry run" in result.output
    assert count(cli_db, JobDB) == 0
    assert count(cli_db, RunDB) == 0
    # The HTTP call happened, so it counts against the quota even in a dry run.
    assert Quota(cli_db, "remotive", 4, datetime.now(UTC).date()).used_today() == 1


def test_cli_run_writes_and_shows_approved(cli_db: Engine, remotive_api: respx.MockRouter) -> None:
    runner = CliRunner()
    runner.invoke(cli_app, ["profile", "load", str(EXAMPLE)])
    result = runner.invoke(cli_app, ["run", "--profile", "example"])

    assert result.exit_code == 0, result.output
    assert count(cli_db, JobDB) == 6
    # Example profile: remote from PT. The two jobs restricted to the US/Canada fail the
    # location filter; the four Worldwide/Europe ones pass (a USD salary is not compared
    # with the EUR minimum, since there is no currency conversion).
    assert "4 of 6 passed the filters" in result.output
    assert "location: 2" in result.output
    with session(cli_db) as s:
        titles = set(s.scalars(select(JobDB.title).join(MatchDB)))
    assert "Senior Data Engineer" in titles
    assert "Customer Service Representative" not in titles
    assert "Tech Lead Full-Stack Rails Engineer" not in titles


def test_cli_run_without_profiles_fails_with_a_message(cli_db: Engine) -> None:
    result = CliRunner().invoke(cli_app, ["run"])
    assert result.exit_code != 0
    assert "no profile" in result.output
