"""Parsing free-text salaries, e.g. "$35,3k - $52k", "€4.000/mês", "R$ 20 mil"."""

import re

from job_finder.domain import Salary, SalaryPeriod
from job_finder.text import strip_accents

HOURS_PER_YEAR = 2080
MONTHS_PER_YEAR = 12

_CURRENCIES = [  # order matters: "R$" and "US$" before "$"
    ("R$", "BRL"),
    ("US$", "USD"),
    ("$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
]
_CODES = re.compile(r"\b(USD|EUR|GBP|BRL|CHF|CAD|AUD)\b", re.IGNORECASE)

# Patterns in English and Portuguese.
_PERIODS: list[tuple[re.Pattern[str], SalaryPeriod]] = [
    (re.compile(r"/\s*(hour|hr|h)\b|per hour|hourly|por hora|/hora"), "hour"),
    (re.compile(r"/\s*(month|mo|mes)\b|per month|monthly|por mes|mensal"), "month"),
    (re.compile(r"/\s*(year|yr|ano)\b|per year|annual|yearly|a year|por ano|anual"), "year"),
]
# Units that cannot be compared with a salary.
_NOT_SALARY = re.compile(r"per (task|project|word|article)|por (tarefa|projeto)")

_NUMBER = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k\b|mil\b)?", re.IGNORECASE)


def _amount(number: str, suffix: str | None) -> float:
    if suffix:  # "35,3k" / "3.5k": comma or dot is the decimal separator
        return float(number.replace(",", ".")) * 1000
    parts = re.split(r"[.,]", number)
    if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
        return float("".join(parts))  # "100,000" / "4.000": thousands separator
    return float(number.replace(",", "."))


def parse_salary(text: str | None) -> Salary | None:
    if not text:
        return None
    clean = strip_accents(text).lower()
    if _NOT_SALARY.search(clean):
        return None

    currency = next((code for symbol, code in _CURRENCIES if symbol in text), None)
    if found := _CODES.search(text):
        currency = found.group(1).upper()

    matches = _NUMBER.findall(clean)
    amounts = [_amount(n, s) for n, s in matches]
    # "60-70k": the "k" on one side of the range applies to the other.
    if any(s for _, s in matches):
        amounts = [
            v * 1000 if not s and v < 1000 else v
            for v, (_, s) in zip(amounts, matches, strict=True)
        ]
    amounts = [v for v in amounts if v > 0]
    if not amounts:
        return None

    period = next((p for regex, p in _PERIODS if regex.search(clean)), None)
    if period is None:
        if currency == "BRL":
            # In Brazil salaries are quoted per month; a bare "R$ 150" is an hourly rate.
            period = "month" if max(amounts) >= 1_000 else "hour"
        elif max(amounts) >= 10_000:
            period = "year"

    return Salary(minimum=min(amounts), maximum=max(amounts), currency=currency, period=period)


def yearly(amount: float, period: SalaryPeriod) -> float:
    if period == "hour":
        return amount * HOURS_PER_YEAR
    if period == "month":
        return amount * MONTHS_PER_YEAR
    return amount
