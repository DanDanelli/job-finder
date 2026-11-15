"""The site: login, sessions, per-profile isolation, jobs and profile pages."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from job_finder.config import Settings
from job_finder.db import repo
from job_finder.db.models import FeedbackDB, MatchDB, ProfileDB, SessionDB
from job_finder.db.session import session
from job_finder.domain import (
    ContractType,
    Filters,
    MatchStatus,
    Profile,
    Query,
    RawJob,
    Salary,
    WorkMode,
)
from job_finder.pipeline.run import execute_run
from job_finder.quota import Quota
from job_finder.web import auth, forms
from job_finder.web.app import create_app

NOW = datetime.now(UTC)
PASSWORD = "correct horse battery"


def raw(source_id: str, title: str, **kw: Any) -> RawJob:
    base: dict[str, Any] = {
        "source": "remotive",
        "source_id": source_id,
        "title": title,
        "company": "Acme",
        "location": "Brazil",
        "work_mode": "remote",
        "url": f"https://remotive.com/{source_id}",
        "posted_at": NOW - timedelta(days=1),
    }
    return RawJob.model_validate(base | kw)


class Source:
    name = "remotive"
    daily_limit = None

    def __init__(self, jobs: list[RawJob]) -> None:
        self.jobs = jobs

    def search(self, queries: object, quota: Quota) -> list[RawJob]:
        return self.jobs


def add_profile(engine: Engine, name: str, password: str | None = PASSWORD) -> int:
    with session(engine) as s:
        repo.save_profile(s, Profile(name=name, queries=[Query(term="Cloud Architect")]))
        db = repo.find_profile(s, name)
        assert db is not None
        db.password_hash = auth.hash_password(password) if password else None
        return db.id


def add_matches(engine: Engine, name: str, jobs: list[RawJob], score: int = 75) -> list[int]:
    with session(engine) as s:
        execute_run(s, engine, [Source(jobs)], repo.list_profiles(s, [name]), NOW)
    with session(engine) as s:
        profile = repo.find_profile(s, name)
        assert profile is not None
        ids = []
        for m in s.scalars(select(MatchDB).where(MatchDB.profile_id == profile.id)):
            m.status, m.score, m.summary = MatchStatus.NEW, score, f"Fit of {m.job.title}"
            ids.append(m.id)
        return ids


@pytest.fixture
def client(engine: Engine, db_url: str) -> Iterator[TestClient]:
    settings = Settings(database_url=db_url, schedule_enabled=False, web_secure_cookie=False)
    with TestClient(create_app(settings), follow_redirects=False) as c:
        yield c


def login(client: TestClient, name: str = "daniel", password: str = PASSWORD) -> Any:
    return client.post("/login", data={"username": name, "password": password})


# --- passwords and sessions -------------------------------------------------------


def test_password_hash_round_trip() -> None:
    stored = auth.hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert auth.verify_password(PASSWORD, stored)
    assert not auth.verify_password("wrong password!", stored)
    assert not auth.verify_password(PASSWORD, None)
    assert not auth.verify_password(PASSWORD, "garbage")
    assert auth.hash_password(PASSWORD) != stored  # random salt


def test_short_passwords_are_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        auth.hash_password("short")


def test_pages_need_a_session(client: TestClient) -> None:
    for path in ("/", "/jobs", "/profile"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200
    assert client.get("/health").status_code == 200


def test_login_and_logout(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")

    r = login(client, "Daniel ")  # case and spaces do not matter
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    with session(engine) as s:
        row = s.scalars(select(SessionDB)).one()
        assert row.token_hash not in cookie  # only the hash is stored
    assert client.get("/jobs").status_code == 200

    assert client.post("/logout").status_code == 303
    with session(engine) as s:
        assert s.scalars(select(SessionDB)).all() == []
    assert client.get("/jobs").status_code == 303


def test_secure_cookie_by_default(engine: Engine, db_url: str) -> None:
    add_profile(engine, "daniel")
    app = create_app(Settings(database_url=db_url, schedule_enabled=False))
    with TestClient(app, follow_redirects=False) as c:
        assert "Secure" in login(c).headers["set-cookie"]


@pytest.mark.parametrize(
    ("name", "password"), [("daniel", "wrong password!"), ("nobody", PASSWORD)]
)
def test_bad_login(engine: Engine, client: TestClient, name: str, password: str) -> None:
    add_profile(engine, "daniel")
    r = login(client, name, password)
    assert r.status_code == 401
    assert "Wrong username or password" in r.text
    assert "set-cookie" not in r.headers


def test_profile_without_password_cannot_sign_in(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel", password=None)
    assert login(client).status_code == 401


def test_repeated_failures_lock_the_username(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    for _ in range(auth.LoginThrottle.MAX_FAILURES):
        assert login(client, password="wrong password!").status_code == 401
    r = login(client)  # right password, but locked
    assert r.status_code == 200 and "Too many attempts" in r.text
    assert "set-cookie" not in r.headers


def test_throttle_window_expires() -> None:
    throttle = auth.LoginThrottle()
    for _ in range(throttle.MAX_FAILURES):
        throttle.failed("x", now=0)
    assert throttle.blocked("x", now=1)
    assert not throttle.blocked("x", now=throttle.WINDOW + 1)


def test_expired_session_is_refused(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    login(client)
    with session(engine) as s:
        s.scalars(select(SessionDB)).one().expires_at = NOW - timedelta(minutes=1)
    assert client.get("/jobs").status_code == 303


def test_cross_origin_posts_are_refused(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    r = client.post(
        "/login",
        data={"username": "daniel", "password": PASSWORD},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    same = client.post(
        "/login",
        data={"username": "daniel", "password": PASSWORD},
        headers={"Origin": "http://testserver"},
    )
    assert same.status_code == 303


def test_proxy_forwarded_host_counts_as_same_origin(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    r = client.post(
        "/login",
        data={"username": "daniel", "password": PASSWORD},
        headers={
            "Origin": "https://pi.tailnet.ts.net:8443",
            "X-Forwarded-Host": "pi.tailnet.ts.net:8443",
        },
    )
    assert r.status_code == 303


# --- jobs -------------------------------------------------------------------------


def test_jobs_page_lists_only_your_jobs_with_credits(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    add_profile(engine, "partner")
    add_matches(engine, "daniel", [raw("1", "Cloud Architect <AWS>")])
    add_matches(engine, "partner", [raw("2", "Brand Manager")])

    login(client)
    page = client.get("/jobs").text
    assert "Cloud Architect &lt;AWS&gt;" in page  # escaped
    assert "Brand Manager" not in page
    assert "via Remotive" in page
    assert 'href="https://remotive.com/1"' in page


def test_save_dismiss_restore(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    (match_id,) = add_matches(engine, "daniel", [raw("1", "Cloud Architect")])
    login(client)

    r = client.post(f"/matches/{match_id}", data={"action": "save", "back": "tab=new"})
    assert r.status_code == 303 and r.headers["location"] == "/jobs?tab=new"
    assert "Cloud Architect" in client.get("/jobs?tab=saved").text
    assert "Cloud Architect" not in client.get("/jobs?tab=new").text

    client.post(f"/matches/{match_id}", data={"action": "dismiss"})
    client.post(f"/matches/{match_id}", data={"action": "restore"})
    with session(engine) as s:
        assert s.get_one(MatchDB, match_id).status == MatchStatus.NEW
        actions = [(f.action, f.origin) for f in s.scalars(select(FeedbackDB))]
    assert actions == [("save", "web"), ("dismiss", "web"), ("restore", "web")]


def test_cannot_touch_another_profiles_match(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    add_profile(engine, "partner")
    (theirs,) = add_matches(engine, "partner", [raw("2", "Brand Manager")])
    login(client)

    assert client.post(f"/matches/{theirs}", data={"action": "dismiss"}).status_code == 404
    assert client.post(f"/matches/{theirs}", data={"action": "nonsense"}).status_code == 404
    with session(engine) as s:
        assert s.get_one(MatchDB, theirs).status == MatchStatus.NEW


def test_redirect_after_action_stays_on_the_site(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    (match_id,) = add_matches(engine, "daniel", [raw("1", "Cloud Architect")])
    login(client)
    r = client.post(f"/matches/{match_id}", data={"action": "save", "back": "//evil.example"})
    assert r.headers["location"].startswith("/jobs?")
    r = client.post(f"/matches/{match_id}", data={"action": "save", "back": "x=https://evil"})
    assert r.headers["location"] == "/jobs"


def test_quick_filters(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    add_matches(engine, "daniel", [raw("1", "Cloud Architect")], score=55)
    login(client)
    assert "Cloud Architect" in client.get("/jobs?min_score=50").text
    assert "Cloud Architect" not in client.get("/jobs?min_score=60").text
    assert "Cloud Architect" not in client.get("/jobs?mode=hybrid").text


# --- profile ----------------------------------------------------------------------


def profile_form(**overrides: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "queries": "Cloud Architect\nSRE | BR, Portugal\n# Platform Engineer",
        "remote_from": "BR",
        "countries": "Brasil",
        "cities": "São Paulo\nRio de Janeiro",
        "work_modes": ["remote", "hybrid"],
        "contract_types": ["full_time", "contract"],
        "min_any_amount": "",
        "min_full_time_amount": "20.000",
        "min_full_time_currency": "brl",
        "min_full_time_period": "month",
        "min_contract_amount": "30k",
        "min_contract_currency": "BRL",
        "min_contract_period": "month",
        "blocked_companies": "Evil Corp",
        "blocked_keywords": "junior",
        "max_age_days": "10",
        "preferences": "No crypto.",
    }
    return form | overrides


def test_profile_form_saves_everything(engine: Engine, client: TestClient) -> None:
    pid = add_profile(engine, "daniel")
    login(client)

    r = client.post("/profile", data=profile_form())
    assert r.status_code == 303 and r.headers["location"] == "/profile?dropped=0"

    with session(engine) as s:
        p = repo.profile_from(s.get_one(ProfileDB, pid))
        paused = s.get_one(ProfileDB, pid).paused
    assert [(q.term, q.countries, q.active) for q in p.queries] == [
        ("Cloud Architect", [], True),
        ("SRE", ["BR", "PT"], True),
        ("Platform Engineer", [], False),
    ]
    f = p.filters
    assert f.remote_from == ["BR"] and f.countries == ["BR"]
    assert f.cities == ["São Paulo", "Rio de Janeiro"]
    assert f.work_modes == [WorkMode.REMOTE, WorkMode.HYBRID]
    assert f.contract_types == [ContractType.FULL_TIME, ContractType.CONTRACT]
    assert f.min_salary is None
    assert f.min_salary_by_contract == {
        ContractType.FULL_TIME: Salary(minimum=20000, currency="BRL", period="month"),
        ContractType.CONTRACT: Salary(minimum=30000, currency="BRL", period="month"),
    }
    assert (f.blocked_companies, f.blocked_keywords, f.max_age_days) == (
        ["Evil Corp"],
        ["junior"],
        10,
    )
    assert p.preferences == "No crypto." and paused is False

    page = client.get("/profile?dropped=0").text
    assert "SRE | BR, PT" in page and "# Platform Engineer" in page
    assert 'value="20.000"' in page and "Saved." in page


def test_profile_form_errors_keep_what_was_typed(engine: Engine, client: TestClient) -> None:
    pid = add_profile(engine, "daniel")
    login(client)

    r = client.post(
        "/profile", data=profile_form(remote_from="BR, Narnia", min_contract_amount="abc")
    )
    assert r.status_code == 422
    assert "Not recognized: Narnia" in r.text and "Not recognized: abc" in r.text
    assert 'value="BR, Narnia"' in r.text
    with session(engine) as s:
        assert repo.profile_from(s.get_one(ProfileDB, pid)).filters == Filters()


def test_fields_not_on_the_form_are_kept() -> None:
    current = Filters(
        languages=["en"],
        min_salary_by_contract={ContractType.FREELANCE: Salary(minimum=100, period="hour")},
    )
    parsed = forms.parse_profile({"queries": "x"}, {}, current)
    assert parsed.filters.languages == ["en"]
    assert ContractType.FREELANCE in parsed.filters.min_salary_by_contract


def test_tightening_a_filter_drops_unseen_matches_only(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    ids = add_matches(
        engine,
        "daniel",
        [
            raw("1", "Junior Cloud Engineer"),  # unseen: dropped
            raw("2", "Junior Platform Engineer"),  # saved: kept
            raw("3", "Junior SRE"),  # sent in a digest: kept
            raw("4", "Senior Cloud Architect"),  # passes: kept
        ],
    )
    with session(engine) as s:
        by_title = {s.get_one(MatchDB, i).job.title: i for i in ids}
        s.get_one(MatchDB, by_title["Junior Platform Engineer"]).status = MatchStatus.SAVED
        s.add(
            FeedbackDB(match_id=by_title["Junior Platform Engineer"], action="save", origin="web")
        )
        s.get_one(MatchDB, by_title["Junior SRE"]).notified_at = NOW
    login(client)

    r = client.post("/profile", data=profile_form(blocked_keywords="junior", contract_types=[]))
    assert r.headers["location"] == "/profile?dropped=1"
    assert "1 jobs no longer pass" in client.get("/profile?dropped=1").text
    with session(engine) as s:
        left = {m.job.title for m in s.scalars(select(MatchDB))}
    assert left == {"Junior Platform Engineer", "Junior SRE", "Senior Cloud Architect"}


def test_pausing_from_the_site(engine: Engine, client: TestClient) -> None:
    pid = add_profile(engine, "daniel")
    login(client)
    client.post("/profile", data=profile_form(paused="on"))
    with session(engine) as s:
        assert s.get_one(ProfileDB, pid).paused is True


@pytest.mark.parametrize(
    ("text", "value"),
    [("20000", 20000), ("20.000", 20000), ("20 000", 20000), ("20k", 20000), ("20,5k", 20500)],
)
def test_amounts(text: str, value: float) -> None:
    assert forms.parse_amount(text) == value


def test_country_parsing() -> None:
    assert forms.parse_countries("br, Portugal; Estados Unidos") == (["BR", "PT", "US"], [])
    assert forms.parse_countries("LATAM, xx") == ([], ["LATAM", "xx"])
