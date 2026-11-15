"""Free-text parsers: location, salary, HTML. Cases taken from real postings."""

import pytest

from job_finder.domain import Salary
from job_finder.pipeline.locations import WORLDWIDE, is_eligible, parse_location
from job_finder.pipeline.salary import parse_salary, yearly
from job_finder.text import html_to_text, key


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Worldwide", [WORLDWIDE]),
        ("USA", ["US"]),
        ("LATAM, Europe, USA, Canada, APAC", ["LATAM", "EUROPE", "US", "CA", "APAC"]),
        (
            "Europe, USA, Canada, Australia, Singapore, Mexico, Saudi Arabia",
            ["EUROPE", "US", "CA", "AU", "SG", "MX", "SA"],
        ),
        ("USA, Canada, USA timezones", ["US", "CA"]),
        ("Remote - Europe", ["EUROPE"]),
        ("Germany or Netherlands", ["DE", "NL"]),
        ("Brasil", ["BR"]),
        ("UK only", ["GB"]),
        ("Lisboa, Portugal", ["PT"]),  # city ignored
        ("Remote", []),  # ambiguous: stays unknown
        ("", []),
        (None, []),
    ],
)
def test_parse_location(text: str | None, expected: list[str]) -> None:
    assert parse_location(text) == expected


@pytest.mark.parametrize(
    ("locations", "countries", "expected"),
    [
        ([WORLDWIDE], ["BR"], True),
        (["US"], ["BR"], False),
        (["LATAM", "EUROPE"], ["BR"], True),
        (["EU"], ["GB"], False),  # the UK is not in the EU
        (["EUROPE"], ["GB"], True),
        (["EMEA"], ["PT"], True),
        (["US", "CA"], ["BR", "PT"], False),
        (["LATAM"], ["BR", "PT"], True),
        ([], ["BR"], None),
    ],
)
def test_is_eligible(locations: list[str], countries: list[str], expected: bool | None) -> None:
    assert is_eligible(locations, countries) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$35,3k- $52k", Salary(minimum=35_300, maximum=52_000, currency="USD", period="year")),
        ("$14 - $20/hour", Salary(minimum=14, maximum=20, currency="USD", period="hour")),
        ("OTE $25k - $35k", Salary(minimum=25_000, maximum=35_000, currency="USD", period="year")),
        ("$50-$75 /hour", Salary(minimum=50, maximum=75, currency="USD", period="hour")),
        ("$14/hour", Salary(minimum=14, maximum=14, currency="USD", period="hour")),
        ("$3.5k–$5k/month", Salary(minimum=3_500, maximum=5_000, currency="USD", period="month")),  # noqa: RUF001
        ("70K-80K", Salary(minimum=70_000, maximum=80_000, currency=None, period="year")),
        (
            "$170k - $200k",
            Salary(minimum=170_000, maximum=200_000, currency="USD", period="year"),
        ),
        (
            "$100,000 - $120,000",
            Salary(minimum=100_000, maximum=120_000, currency="USD", period="year"),
        ),
        ("€4.000/mês", Salary(minimum=4_000, maximum=4_000, currency="EUR", period="month")),
        (
            "R$ 8.000 - R$ 12.000",
            Salary(minimum=8_000, maximum=12_000, currency="BRL", period="month"),
        ),
        ("60-70k EUR", Salary(minimum=60_000, maximum=70_000, currency="EUR", period="year")),
        # BRL with no period: monthly salary, or an hourly rate when small.
        ("R$ 150", Salary(minimum=150, maximum=150, currency="BRL", period="hour")),
        (
            "R$ 120 a R$ 180 por hora",
            Salary(minimum=120, maximum=180, currency="BRL", period="hour"),
        ),
        ("R$ 25.000", Salary(minimum=25_000, maximum=25_000, currency="BRL", period="month")),
    ],
)
def test_parse_salary(text: str, expected: Salary) -> None:
    assert parse_salary(text) == expected


@pytest.mark.parametrize("text", ["$1000 per task", "Competitive", "", None])
def test_unparseable_salary(text: str | None) -> None:
    assert parse_salary(text) is None


def test_yearly() -> None:
    assert yearly(20, "hour") == 41_600
    assert yearly(4_000, "month") == 48_000
    assert yearly(50_000, "year") == 50_000


def test_html_to_text() -> None:
    html = "<p>Olá <b>mundo</b></p><script>x()</script><ul><li>AWS</li><li>Terraform</li></ul>"
    assert html_to_text(html) == "Olá\nmundo\nAWS\nTerraform"
    assert html_to_text("") is None
    assert html_to_text("<p>   </p>") is None


def test_key() -> None:
    assert key("  Engenheiro(a) de Dados — São Paulo! ") == "engenheiro a de dados sao paulo"
