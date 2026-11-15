from datetime import UTC, datetime, timedelta

import pytest

from job_finder.domain import (
    ContractType,
    Filters,
    Job,
    RawJob,
    Salary,
    WorkMode,
)
from job_finder.pipeline.dedup import canonical_company, canonical_title, dedup_key
from job_finder.pipeline.filters import discard_reason
from job_finder.pipeline.locations import WORLDWIDE, parse_location
from job_finder.pipeline.normalize import normalize

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def raw(**kw: object) -> RawJob:
    base: dict[str, object] = {
        "source": "test",
        "source_id": "1",
        "title": "Cloud Architect",
        "company": "Acme",
        "url": "https://example.com/1",
    }
    return RawJob.model_validate(base | kw)


def job(**kw: object) -> Job:
    base: dict[str, object] = {
        "title": "Cloud Architect",
        "company": "Acme",
        "url": "https://example.com/1",
        "posted_at": NOW - timedelta(days=1),
    }
    return Job.model_validate(base | kw)


# --- normalization ------------------------------------------------------------


def test_normalizes_a_remote_remotive_job() -> None:
    j = normalize(
        raw(
            title="  Senior   Data Engineer ",
            company="Lemon.io ",
            location="LATAM, Europe, USA, Canada, APAC",
            work_mode="remote",
            contract_type="full_time",
            salary_text="$170k - $200k",
            posted_at=datetime(2026, 11, 11, 20, 17),  # no tz, as the API sends it
        )
    )
    assert j.title == "Senior Data Engineer"
    assert j.company == "Lemon.io"
    assert j.work_mode == WorkMode.REMOTE
    assert j.contract_type == ContractType.FULL_TIME
    assert j.eligible_locations == ["LATAM", "EUROPE", "US", "CA", "APAC"]
    assert j.country is None
    assert j.salary == Salary(minimum=170_000, maximum=200_000, currency="USD", period="year")
    assert j.posted_at == datetime(2026, 11, 11, 20, 17, tzinfo=UTC)


def test_country_only_when_the_location_is_a_single_country() -> None:
    assert normalize(raw(location="USA")).country == "US"
    assert normalize(raw(location="USA, Canada")).country is None


def test_worldwide_absorbs_the_other_locations() -> None:
    assert normalize(raw(location="Worldwide, USA")).eligible_locations == [WORLDWIDE]


def test_structured_salary_from_the_source_wins() -> None:
    structured = Salary(minimum=1, maximum=2, currency="EUR", period="year")
    assert normalize(raw(salary=structured, salary_text="$9k")).salary == structured


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hybrid", WorkMode.HYBRID),
        ("On-site", WorkMode.ONSITE),
        ("Híbrido", WorkMode.HYBRID),
        ("Presencial", WorkMode.ONSITE),
        ("??", WorkMode.UNKNOWN),
    ],
)
def test_work_mode(text: str, expected: WorkMode) -> None:
    assert normalize(raw(work_mode=text)).work_mode == expected


# --- dedup --------------------------------------------------------------------


def test_canonical_title() -> None:
    assert canonical_title("Sr. Cloud Engineer (m/f/d)") == "senior cloud engineer"
    assert canonical_title("Senior Cloud Engineer (M/W/D)") == "senior cloud engineer"
    # A different seniority is a different job.
    assert canonical_title("Jr. Cloud Engineer") != canonical_title("Sr. Cloud Engineer")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Inc.", "acme"),
        ("Acme GmbH", "acme"),
        ("Natura S.A.", "natura"),
        ("Itaú Unibanco Ltda", "itau unibanco"),
        ("Limited", "limited"),  # does not erase the whole name
        (None, ""),
    ],
)
def test_canonical_company(name: str | None, expected: str) -> None:
    assert canonical_company(name) == expected


def test_same_job_from_different_sources_gets_the_same_key() -> None:
    a = normalize(raw(title="Sr Cloud Architect (m/f/d)", company="Acme GmbH", location="Germany"))
    b = normalize(raw(title="Senior Cloud Architect", company="ACME", location="Deutschland"))
    assert dedup_key(a) == dedup_key(b) == "acme|senior cloud architect|de"


def test_key_of_a_remote_job_without_country() -> None:
    j = normalize(raw(location="Worldwide", work_mode="remote"))
    assert dedup_key(j) == "acme|cloud architect|remote"


# --- filters ------------------------------------------------------------------


def test_job_passes_with_no_filters() -> None:
    assert discard_reason(job(), Filters(), NOW) is None


def test_old_job() -> None:
    old = job(posted_at=NOW - timedelta(days=30))
    assert discard_reason(old, Filters(max_age_days=14), NOW) == "too_old"
    assert discard_reason(job(posted_at=None), Filters(), NOW) is None


def test_unknown_work_mode_passes() -> None:
    filters = Filters(work_modes=[WorkMode.REMOTE])
    assert discard_reason(job(work_mode="onsite"), filters, NOW) == "work_mode"
    assert discard_reason(job(work_mode="unknown"), filters, NOW) is None


def test_remote_must_accept_residents_of_the_country() -> None:
    filters = Filters(remote_from=["BR"])
    us_only = job(work_mode="remote", eligible_locations=["US"])
    latam = job(work_mode="remote", eligible_locations=["LATAM", "EUROPE"])
    unknown = job(work_mode="remote", eligible_locations=[])
    assert discard_reason(us_only, filters, NOW) == "location"
    assert discard_reason(latam, filters, NOW) is None
    assert discard_reason(unknown, filters, NOW) is None


def test_country_applies_to_onsite_and_not_to_remote() -> None:
    filters = Filters(countries=["BR"])
    assert discard_reason(job(work_mode="onsite", country="US"), filters, NOW) == "country"
    assert discard_reason(job(work_mode="hybrid", country="BR"), filters, NOW) is None
    assert discard_reason(job(work_mode="remote", country="US"), filters, NOW) is None


def test_contract_type() -> None:
    filters = Filters(contract_types=[ContractType.FULL_TIME])
    assert discard_reason(job(contract_type="freelance"), filters, NOW) == "contract_type"
    assert discard_reason(job(contract_type=None), filters, NOW) is None


@pytest.mark.parametrize(
    ("salary", "reason"),
    [
        (Salary(minimum=30_000, maximum=40_000, currency="EUR", period="year"), "salary"),
        (Salary(minimum=40_000, maximum=60_000, currency="EUR", period="year"), None),  # max ok
        (Salary(minimum=3_000, maximum=4_000, currency="EUR", period="month"), "salary"),  # 48k
        (Salary(minimum=3_000, maximum=4_500, currency="EUR", period="month"), None),  # 54k: margin
        (Salary(minimum=5_000, maximum=5_500, currency="EUR", period="month"), None),  # 66k
        (Salary(minimum=10, maximum=20, currency="USD", period="hour"), None),  # other currency
        (Salary(minimum=10, maximum=20, currency="EUR", period="hour"), None),  # hourly: LLM judges
        (Salary(minimum=10, maximum=20, currency="EUR", period=None), None),  # unknown period
        (None, None),  # no salary
    ],
)
def test_min_salary(salary: Salary | None, reason: str | None) -> None:
    filters = Filters(min_salary=Salary(minimum=60_000, currency="EUR", period="year"))
    assert discard_reason(job(salary=salary), filters, NOW) == reason


def test_blocked_company_ignores_suffix_and_case() -> None:
    filters = Filters(blocked_companies=["acme"])
    assert discard_reason(job(company="ACME Inc."), filters, NOW) == "blocked_company"
    assert discard_reason(job(company="Acme Labs"), filters, NOW) is None


def test_blocked_keyword_only_in_title_and_whole_word() -> None:
    filters = Filters(blocked_keywords=["intern", "Estágio"])
    assert discard_reason(job(title="Cloud Intern"), filters, NOW) == "blocked_keyword"
    assert discard_reason(job(title="Estagio em Dados"), filters, NOW) == "blocked_keyword"
    assert discard_reason(job(title="International Sales"), filters, NOW) is None
    assert discard_reason(job(description="intern"), filters, NOW) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Contagem - MG, BR", ["BR"]),  # MG would be Madagascar
        ("Vitória, ES, Brasil", ["BR"]),  # ES would be Spain
        ("Madrid, ES", ["ES"]),  # without Brazil spelled out, ES stays Spain
    ],
)
def test_brazilian_state_code_is_not_a_country(text: str, expected: list[str]) -> None:
    assert parse_location(text) == expected


@pytest.mark.parametrize(
    ("contract", "monthly", "reason"),
    [
        ("full_time", 22_000, None),  # CLT above 20k
        ("full_time", 19_000, None),  # within the 10% margin
        ("full_time", 17_000, "salary"),
        ("contract", 25_000, "salary"),  # PJ below 30k
        ("contract", 32_000, None),
        (None, 22_000, None),  # no type: uses the general minimum (20k)
    ],
)
def test_min_salary_by_contract_type(
    contract: str | None, monthly: int, reason: str | None
) -> None:
    filters = Filters(
        min_salary=Salary(minimum=20_000, currency="BRL", period="month"),
        min_salary_by_contract={
            "full_time": Salary(minimum=20_000, currency="BRL", period="month"),
            "contract": Salary(minimum=30_000, currency="BRL", period="month"),
        },
    )
    salary = Salary(minimum=monthly, maximum=monthly, currency="BRL", period="month")
    assert discard_reason(job(salary=salary, contract_type=contract), filters, NOW) == reason


def test_hourly_rate_never_discards_in_code() -> None:
    filters = Filters(
        min_salary_by_contract={
            "contract": Salary(minimum=30_000, currency="BRL", period="month"),
        }
    )
    hourly = Salary(minimum=90, maximum=90, currency="BRL", period="hour")
    assert discard_reason(job(salary=hourly, contract_type="contract"), filters, NOW) is None
