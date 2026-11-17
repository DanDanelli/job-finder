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


@pytest.mark.parametrize(
    ("base_url", "headers", "secure"),
    [
        ("https://pi.example", {}, True),
        ("http://pi.example", {"x-forwarded-proto": "https"}, True),  # tailscale serve
        ("http://192.168.0.10:8000", {}, False),  # home network, plain HTTP
    ],
)
def test_cookie_is_secure_over_https(
    engine: Engine, db_url: str, base_url: str, headers: dict[str, str], secure: bool
) -> None:
    add_profile(engine, "daniel")
    app = create_app(Settings(database_url=db_url, schedule_enabled=False))
    with TestClient(app, base_url=base_url, headers=headers, follow_redirects=False) as c:
        r = login(c)
        assert r.status_code == 303
        assert ("Secure" in r.headers["set-cookie"]) is secure


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


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        # what Safari/Chrome send for a form on the page itself, behind tailscale serve
        ({"Sec-Fetch-Site": "same-origin", "Origin": "https://pi.ts.net:8443"}, 303),
        ({"Sec-Fetch-Site": "none"}, 303),  # typed or bookmarked
        ({"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),  # another service on the same tailnet name
        ({"Origin": "null"}, 403),
    ],
)
def test_fetch_metadata(
    engine: Engine, client: TestClient, headers: dict[str, str], status: int
) -> None:
    add_profile(engine, "daniel")
    r = client.post("/login", data={"username": "daniel", "password": PASSWORD}, headers=headers)
    assert r.status_code == status


def test_pages_do_not_make_browsers_send_a_null_origin(client: TestClient) -> None:
    page = client.get("/login").text
    assert 'content="no-referrer"' not in page and 'content="same-origin"' in page


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


def test_applied_leaves_saved_and_can_go_back(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    (match_id,) = add_matches(engine, "daniel", [raw("1", "Cloud Architect")])
    login(client)
    client.post(f"/matches/{match_id}", data={"action": "save"})
    assert 'value="apply"' in client.get("/jobs?tab=saved").text

    client.post(f"/matches/{match_id}", data={"action": "apply"})
    assert "Cloud Architect" not in client.get("/jobs?tab=saved").text
    page = client.get("/jobs?tab=applied").text
    assert "Cloud Architect" in page and 'value="apply"' not in page
    assert '<span class="count">1</span>' in page

    client.post(f"/matches/{match_id}", data={"action": "save"})  # "Back to saved"
    with session(engine) as s:
        assert s.get_one(MatchDB, match_id).status == MatchStatus.SAVED
        actions = [f.action for f in s.scalars(select(FeedbackDB))]
    assert actions == ["save", "apply", "save"]


def test_old_dismissals_leave_the_site_but_not_learning(engine: Engine, client: TestClient) -> None:
    from job_finder.llm import learner

    pid = add_profile(engine, "daniel")
    old, recent = add_matches(
        engine, "daniel", [raw("1", "Old Architect"), raw("2", "Recent Architect")]
    )
    login(client)
    for match_id in (old, recent):
        client.post(f"/matches/{match_id}", data={"action": "dismiss"})
    with session(engine) as s:
        s.get_one(MatchDB, old).updated_at = NOW - timedelta(days=8)

    page = client.get("/jobs?tab=dismissed").text
    assert "Recent Architect" in page and "Old Architect" not in page
    assert "last 7 days" in page
    assert '<span class="count">1</span>' in page
    assert learner.state(engine, pid).dismissed == 2


def test_note_with_a_decision_and_on_its_own(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    (match_id,) = add_matches(engine, "daniel", [raw("1", "Cloud Architect")])
    login(client)
    assert 'name="note" value=""' in client.get("/jobs").text

    form = {"action": "dismiss", "note": "  outsourcing   consultancy "}
    client.post(f"/matches/{match_id}", data=form)
    page = client.get("/jobs?tab=dismissed").text
    assert 'value="outsourcing consultancy"' in page and "Save note" in page
    # The same note sent again with another action is not recorded twice.
    client.post(
        f"/matches/{match_id}", data={"action": "restore", "note": "outsourcing consultancy"}
    )
    client.post(
        f"/matches/{match_id}", data={"action": "dismiss", "note": "outsourcing consultancy"}
    )
    # Editing only the note, then clearing it.
    client.post(f"/matches/{match_id}", data={"action": "note", "note": "body shop"})
    client.post(f"/matches/{match_id}", data={"action": "note", "note": "body shop"})
    with session(engine) as s:
        m = s.get_one(MatchDB, match_id)
        rows = [(f.action, f.comment) for f in m.feedback]
        assert rows == [
            ("dismiss", "outsourcing consultancy"),
            ("restore", None),
            ("dismiss", None),
            ("note", "body shop"),
        ]
        assert repo.latest_note(m.feedback) == "body shop"
        assert m.status == MatchStatus.DISMISSED  # a note does not change the status
    # Without the marker an empty field means "no note field": nothing changes.
    client.post(f"/matches/{match_id}", data={"action": "note", "note": ""})
    with session(engine) as s:
        assert repo.latest_note(s.get_one(MatchDB, match_id).feedback) == "body shop"
    client.post(f"/matches/{match_id}", data={"action": "note", "note": "", "has_note": "1"})
    with session(engine) as s:
        assert repo.latest_note(s.get_one(MatchDB, match_id).feedback) is None
    assert 'name="note" value=""' in client.get("/jobs?tab=dismissed").text


def test_note_is_cut_to_the_limit(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    (match_id,) = add_matches(engine, "daniel", [raw("1", "Cloud Architect")])
    login(client)
    client.post(f"/matches/{match_id}", data={"action": "save", "note": "x" * 2000})
    with session(engine) as s:
        assert len(repo.latest_note(s.get_one(MatchDB, match_id).feedback) or "") == 500


def test_cannot_touch_another_profiles_match(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    add_profile(engine, "partner")
    (theirs,) = add_matches(engine, "partner", [raw("2", "Brand Manager")])
    login(client)

    assert client.post(f"/matches/{theirs}", data={"action": "dismiss"}).status_code == 404
    assert client.post(f"/matches/{theirs}", data={"action": "nonsense"}).status_code == 404
    assert (
        client.post(f"/matches/{theirs}", data={"action": "note", "note": "x"}).status_code == 404
    )
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


# --- CLI ------------------------------------------------------------------------


def _password_hash(engine: Engine, name: str) -> str | None:
    with session(engine) as s:
        db = repo.find_profile(s, name)
        assert db is not None
        return db.password_hash


@pytest.mark.parametrize(
    "line",
    [
        f"{PASSWORD}\n",
        f"{PASSWORD}\r\n",  # line ending through a TTY chain
        f"\x1b[200~{PASSWORD}\x1b[201~\n",  # bracketed paste
    ],
)
def test_cli_password_from_stdin(engine: Engine, db_url: str, line: str) -> None:
    from typer.testing import CliRunner

    from job_finder.cli import app

    add_profile(engine, "daniel", password=None)
    with session(engine) as s:
        p = repo.find_profile(s, "daniel")
        assert p is not None
        s.add(SessionDB(token_hash="x" * 64, profile_id=p.id, expires_at=NOW + timedelta(days=1)))
    result = CliRunner().invoke(
        app,
        ["profile", "password", "daniel", "--stdin"],
        input=line,
        env={"JOBFINDER_DATABASE_URL": db_url},
    )
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output
    assert auth.verify_password(PASSWORD, _password_hash(engine, "daniel"))
    with session(engine) as s:
        assert s.scalars(select(SessionDB)).all() == []  # open sessions signed out


def test_cli_password_prompt_cleans_and_compares(engine: Engine, db_url: str) -> None:
    from typer.testing import CliRunner

    from job_finder.cli import app

    add_profile(engine, "daniel", password=None)
    runner = CliRunner()
    env = {"JOBFINDER_DATABASE_URL": db_url}
    pasted_then_typed = f"\x1b[200~{PASSWORD}\x1b[201~\n{PASSWORD}\n"
    ok = runner.invoke(app, ["profile", "password", "daniel"], input=pasted_then_typed, env=env)
    assert ok.exit_code == 0, ok.output
    assert auth.verify_password(PASSWORD, _password_hash(engine, "daniel"))

    bad = runner.invoke(
        app, ["profile", "password", "daniel"], input=f"{PASSWORD}\nother password\n", env=env
    )
    assert bad.exit_code != 0 and "differ" in bad.output
    assert "short" not in bad.output


def test_cli_short_password_from_stdin_is_refused(engine: Engine, db_url: str) -> None:
    from typer.testing import CliRunner

    from job_finder.cli import app

    add_profile(engine, "daniel", password=None)
    result = CliRunner().invoke(
        app,
        ["profile", "password", "daniel", "--stdin"],
        input="short\n",
        env={"JOBFINDER_DATABASE_URL": db_url},
    )
    assert result.exit_code != 0 and _password_hash(engine, "daniel") is None


# --- sorting ----------------------------------------------------------------------


def _set_scores(engine: Engine, scores: dict[str, int]) -> None:
    with session(engine) as s:
        for m in s.scalars(select(MatchDB)):
            m.score = scores[m.job.title]


def test_jobs_can_be_sorted_by_fit_and_date(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    add_matches(
        engine,
        "daniel",
        [
            raw("1", "Old good", posted_at=NOW - timedelta(days=5)),
            raw("2", "New weak", posted_at=NOW - timedelta(hours=2)),
            raw("3", "Middle", posted_at=NOW - timedelta(days=2)),
        ],
    )
    _set_scores(engine, {"Old good": 90, "New weak": 55, "Middle": 70})
    login(client)

    def order(sort: str | None) -> list[str]:
        page = client.get("/jobs" + (f"?sort={sort}" if sort else "")).text
        titles = ["Old good", "New weak", "Middle"]
        return sorted(titles, key=page.index)

    assert order(None) == ["Old good", "Middle", "New weak"]
    assert order("score_desc") == ["Old good", "Middle", "New weak"]
    assert order("score_asc") == ["New weak", "Middle", "Old good"]
    assert order("date_desc") == ["New weak", "Middle", "Old good"]
    assert order("date_asc") == ["Old good", "Middle", "New weak"]
    assert order("nonsense") == ["Old good", "Middle", "New weak"]
    # the order survives an action
    assert 'name="back" value="tab=new&amp;min_score=0&amp;mode=&amp;sort=date_asc"' in (
        client.get("/jobs?sort=date_asc").text
    )


# --- account --------------------------------------------------------------------


def test_change_password_signs_out_other_devices(engine: Engine, db_url: str) -> None:
    add_profile(engine, "daniel")
    settings = Settings(database_url=db_url, schedule_enabled=False, web_secure_cookie=False)
    app = create_app(settings)
    with TestClient(app, follow_redirects=False) as phone, TestClient(app) as laptop:
        login(phone)
        login(laptop)
        new = "new password 123"
        r = phone.post("/account", data={"current": PASSWORD, "new": new, "repeat": new})
        assert r.status_code == 303 and r.headers["location"] == "/account?changed=1"
        assert phone.get("/jobs").status_code == 200  # this device stays in
        assert laptop.get("/jobs", follow_redirects=False).status_code == 303  # signed out
        assert auth.verify_password(new, _password_hash(engine, "daniel"))


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        ({"current": "wrong password", "new": "abcdefgh1", "repeat": "abcdefgh1"}, 403, "wrong"),
        ({"current": PASSWORD, "new": "abcdefgh1", "repeat": "abcdefgh2"}, 422, "differ"),
        ({"current": PASSWORD, "new": "short", "repeat": "short"}, 422, "at least 8"),
    ],
)
def test_change_password_errors(
    engine: Engine, client: TestClient, form: dict[str, str], status: int, message: str
) -> None:
    add_profile(engine, "daniel")
    login(client)
    r = client.post("/account", data=form)
    assert r.status_code == status and message in r.text
    assert auth.verify_password(PASSWORD, _password_hash(engine, "daniel"))


def test_guessing_the_current_password_is_throttled(engine: Engine, client: TestClient) -> None:
    add_profile(engine, "daniel")
    login(client)
    for _ in range(auth.LoginThrottle.MAX_FAILURES):
        client.post("/account", data={"current": "nope nope", "new": "x" * 9, "repeat": "x" * 9})
    r = client.post("/account", data={"current": PASSWORD, "new": "y" * 9, "repeat": "y" * 9})
    assert r.status_code == 429
    assert auth.verify_password(PASSWORD, _password_hash(engine, "daniel"))


# --- CV ---------------------------------------------------------------------------

PDF = b"%PDF-1.7\n fake but well-formed enough for the upload check\n%%EOF"


def _extraction() -> Any:
    from job_finder.domain import Language, ProfileData, Skill
    from job_finder.llm.profile_extractor import ProfileExtraction

    return ProfileExtraction(
        data=ProfileData(
            summary="Cloud architect with a decade of AWS.",
            roles=["Cloud Architect"],
            seniority="senior",
            years_experience=12,
            skills=[Skill(name="AWS", weight=5), Skill(name="Terraform", weight=4)],
            languages=[Language(code="en", level="fluent")],
        ),
        search_terms=["Cloud Architect", "Platform Engineer"],  # the first is already there
    )


def _wait(client: TestClient, path: str, gone: str, timeout: float = 5) -> str:
    import time

    deadline = time.monotonic() + timeout
    while True:
        text = client.get(path).text
        if gone not in text or time.monotonic() > deadline:
            return text
        time.sleep(0.05)


@pytest.fixture
def cv_client(engine: Engine, db_url: str, tmp_path: Any) -> Iterator[tuple[TestClient, list[int]]]:
    calls: list[int] = []

    def fake_extract(pdf: Any, profile_id: int) -> Any:
        assert pdf.read_bytes() == PDF
        calls.append(profile_id)
        return _extraction()

    settings = Settings(
        database_url=db_url,
        schedule_enabled=False,
        web_secure_cookie=False,
        data_dir=tmp_path / "data",
    )
    with TestClient(create_app(settings, extract=fake_extract), follow_redirects=False) as c:
        yield c, calls


def _profile(engine: Engine, name: str = "daniel") -> Profile:
    with session(engine, commit=False) as s:
        db = repo.find_profile(s, name)
        assert db is not None
        return repo.profile_from(db)


def test_cv_upload_review_and_save(
    engine: Engine, cv_client: tuple[TestClient, list[int]], tmp_path: Any
) -> None:
    client, calls = cv_client
    pid = add_profile(engine, "daniel")
    login(client)
    r = client.post("/cv/upload", files={"file": ("cv.pdf", PDF, "application/pdf")})
    assert r.status_code == 303
    page = _wait(client, "/cv", "is reading your CV")
    assert calls == [pid]
    assert "Review what Claude read" in page
    assert "Cloud architect with a decade of AWS." in page and "AWS | 5" in page
    assert 'value="Platform Engineer"' in page and 'value="Cloud Architect"' not in page
    assert _profile(engine).data.summary == ""  # nothing changes before the review

    form = {
        "from_draft": "1",
        "summary": "Cloud architect, edited.",
        "roles": "Cloud Architect\nPlatform Engineer",
        "seniority": "senior",
        "years_experience": "12",
        "skills": "AWS | 5\nTerraform | 4\nKubernetes",
        "languages": "en | fluent\npt | native",
        "industries": "energy",
        "certifications": "",
        "terms": ["Platform Engineer", "Injected term"],
    }
    r = client.post("/cv", data=form)
    assert r.status_code == 303 and r.headers["location"] == "/cv?saved=1"
    p = _profile(engine)
    assert p.data.summary == "Cloud architect, edited."
    assert [(k.name, k.weight) for k in p.data.skills] == [
        ("AWS", 5),
        ("Terraform", 4),
        ("Kubernetes", 3),
    ]
    assert [(x.code, x.level) for x in p.data.languages] == [("en", "fluent"), ("pt", "native")]
    assert [(q.term, q.active) for q in p.queries] == [
        ("Cloud Architect", True),
        ("Platform Engineer", True),
    ]
    stored = tmp_path / "data" / "resumes" / f"{pid}.pdf"
    assert stored.read_bytes() == PDF
    assert not (tmp_path / "data" / "resumes" / "pending" / f"{pid}.pdf").exists()
    with session(engine, commit=False) as s:
        assert s.get_one(ProfileDB, pid).resume_path == str(stored)
    assert "Review what Claude read" not in client.get("/cv").text


def test_cv_discard_changes_nothing(
    engine: Engine, cv_client: tuple[TestClient, list[int]], tmp_path: Any
) -> None:
    client, _ = cv_client
    pid = add_profile(engine, "daniel")
    login(client)
    client.post("/cv/upload", files={"file": ("cv.pdf", PDF, "application/pdf")})
    _wait(client, "/cv", "is reading your CV")
    assert client.post("/cv/discard").status_code == 303
    assert list((tmp_path / "data" / "resumes" / "pending").iterdir()) == []
    assert _profile(engine).data.summary == ""
    assert [q.term for q in _profile(engine).queries] == ["Cloud Architect"]
    with session(engine, commit=False) as s:
        assert s.get_one(ProfileDB, pid).resume_path is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "Choose a PDF"),
        (b"MZ\x90\x00 not a pdf", "not a PDF"),
        (b"%PDF-" + b"0" * (10 * 1024 * 1024), "larger than 10 MB"),
    ],
)
def test_cv_upload_is_checked(
    engine: Engine, cv_client: tuple[TestClient, list[int]], content: bytes, message: str
) -> None:
    client, calls = cv_client
    add_profile(engine, "daniel")
    login(client)
    r = client.post("/cv/upload", files={"file": ("cv.pdf", content, "application/pdf")})
    assert r.status_code == 422 and message in r.text
    assert calls == []


def test_cv_extraction_failure_is_shown(engine: Engine, db_url: str, tmp_path: Any) -> None:
    def broken(pdf: Any, profile_id: int) -> Any:
        raise RuntimeError("Claude is down")

    add_profile(engine, "daniel")
    settings = Settings(
        database_url=db_url,
        schedule_enabled=False,
        web_secure_cookie=False,
        data_dir=tmp_path / "data",
    )
    with TestClient(create_app(settings, extract=broken), follow_redirects=False) as client:
        login(client)
        client.post("/cv/upload", files={"file": ("cv.pdf", PDF, "application/pdf")})
        page = _wait(client, "/cv", "is reading your CV")
    assert "Could not read the CV: RuntimeError: Claude is down" in page
    assert list((tmp_path / "data" / "resumes" / "pending").iterdir()) == []


def test_profile_data_can_be_edited_without_a_cv(
    engine: Engine, cv_client: tuple[TestClient, list[int]]
) -> None:
    client, _ = cv_client
    add_profile(engine, "daniel")
    login(client)
    r = client.post("/cv", data={"summary": "Hand written.", "skills": "SEO | 9", "languages": ""})
    assert r.status_code == 422 and "SEO | 9" in r.text  # weight out of range, kept as typed
    r = client.post(
        "/cv",
        data={
            "summary": "Hand written.",
            "skills": "SEO | 4",
            "languages": "en | avançado",
            "terms": "Injected term",
        },
    )
    assert r.status_code == 303
    p = _profile(engine)
    assert p.data.summary == "Hand written." and p.data.languages[0].level == "advanced"
    assert [q.term for q in p.queries] == ["Cloud Architect"]  # terms only come with a draft


def test_parse_cv_form() -> None:
    from job_finder.web import cv

    parsed = cv.parse_data(
        {
            "seniority": "wizard",
            "years_experience": "ten",
            "skills": "AWS | 5\naws | 2\n | 3\nGo | 0",
            "languages": "en | fluent\nfr | so-so",
        },
        {"fluente": "fluent"},
    )
    assert parsed.errors == {
        "seniority": "wizard",
        "years_experience": "ten",
        "skills": "Go | 0",
        "languages": "fr | so-so",
    }
    assert [k.name for k in parsed.data.skills] == ["AWS"]  # repeated line dropped


# --- search now and status ---------------------------------------------------------


def test_search_now_runs_in_the_background_and_reports(engine: Engine, db_url: str) -> None:
    import threading

    from job_finder.connectors import base

    release = threading.Event()
    started = threading.Event()

    class Slow:
        name = "slowtest"
        daily_limit = 3

        def search(self, queries: object, quota: Quota) -> list[RawJob]:
            started.set()
            release.wait(5)
            quota.consume()
            return [raw("s1", "Cloud Architect at Slow", source="slowtest")]

    base.register("slowtest")(lambda http: Slow())
    add_profile(engine, "daniel")
    settings = Settings(
        database_url=db_url,
        schedule_enabled=False,
        web_secure_cookie=False,
        connectors=["slowtest"],
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        login(client)
        r = client.post("/search")
        assert r.status_code == 303 and r.headers["location"] == "/status"
        assert started.wait(5)
        page = client.get("/status").text
        assert "Collecting from slowtest (1 of 1)" in page
        assert 'http-equiv="refresh"' in page and 'action="/search"' not in page
        jobs = client.get("/jobs").text
        assert "Search in progress" in jobs and "disabled" in jobs
        assert client.post("/search").status_code == 303  # a second click starts nothing
        release.set()
        page = _wait(client, "/status", 'http-equiv="refresh"')
    assert "1 jobs passed your filters, 0 scored by Claude" in page
    assert "no ANTHROPIC_API_KEY" in page  # the run's warning reaches the page
    assert "1 / 3" in page  # slowtest: one request of the three allowed today
    with session(engine, commit=False) as s:
        from job_finder.db.models import RunDB

        assert [r.kind for r in s.scalars(select(RunDB))] == ["on_demand"]


def test_status_shows_cost_shared_and_yours(engine: Engine, client: TestClient) -> None:
    from job_finder.db.models import LLMCostDB

    me = add_profile(engine, "daniel")
    other = add_profile(engine, "bob")
    with session(engine) as s:
        for pid, cost in ((me, 0.25), (other, 0.5), (me, 0.25)):
            s.add(LLMCostDB(profile_id=pid, operation="scoring", model="m", cost_usd=cost))
        s.add(LLMCostDB(profile_id=other, operation="extract_profile", model="m", cost_usd=1.0))
    login(client)
    page = client.get("/status").text
    assert "US$ 2.00 of US$ 5.00 (yours: US$ 0.50)" in page
    assert "Scoring jobs" in page and "Reading CVs" in page


def test_progress_is_reported(engine: Engine) -> None:
    from job_finder.service import full_run

    add_profile(engine, "daniel")
    steps: list[str] = []
    full_run(
        engine,
        Settings(),
        connectors=[Source([raw("1", "Cloud Architect")])],
        progress=lambda key, params: steps.append(key),
        now=NOW,
    )
    assert steps == ["progress.collect", "progress.store", "progress.filter"]
