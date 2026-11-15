"""Profile form: text fields <-> domain models.

Text formats (also explained on the page):
- queries: one per line, "term" or "term | BR, PT"; a line starting with "#" is paused;
- countries: codes or names separated by commas ("BR, Portugal");
- lists (cities, blocked companies/keywords): one per line;
- amounts: "20000", "20.000" or "20k".
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import pycountry

from job_finder.domain import ContractType, Filters, Query, Salary, WorkMode
from job_finder.pipeline.locations import parse_location

# Contract types with their own minimum on the form (CLT and PJ in Brazil).
SALARY_CONTRACTS = (ContractType.FULL_TIME, ContractType.CONTRACT)
PERIODS = ("month", "year", "hour")


@dataclass
class FormErrors:
    """Field -> problem (already in the page's language) and the offending text."""

    fields: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.fields)


def _split(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]", text or "") if p.strip()]


def lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def parse_countries(text: str) -> tuple[list[str], list[str]]:
    """Returns (codes, pieces not recognized as a country)."""
    codes: list[str] = []
    unknown: list[str] = []
    for piece in _split(text):
        if len(piece) == 2 and piece.isalpha():
            found = pycountry.countries.get(alpha_2=piece.upper())
            code = found.alpha_2 if found else None
        else:
            parsed = parse_location(piece)
            code = parsed[0] if len(parsed) == 1 and len(parsed[0]) == 2 else None
        if code is None:
            unknown.append(piece)
        elif code not in codes:
            codes.append(code)
    return codes, unknown


def countries_text(codes: list[str]) -> str:
    return ", ".join(codes)


def parse_queries(text: str) -> tuple[list[Query], list[str]]:
    queries: list[Query] = []
    unknown: list[str] = []
    for line in lines(text):
        active = not line.startswith("#")
        term, _, places = line.lstrip("#").partition("|")
        codes, bad = parse_countries(places)
        unknown.extend(bad)
        if term.strip():
            queries.append(Query(term=term.strip(), countries=codes, active=active))
    return queries, unknown


def queries_text(queries: list[Query]) -> str:
    out = []
    for q in queries:
        line = q.term + (f" | {', '.join(q.countries)}" if q.countries else "")
        out.append(line if q.active else f"# {line}")
    return "\n".join(out)


def parse_amount(text: str) -> float | None:
    """ "20000", "20.000", "20 000" or "20k" -> 20000. Empty -> None. Raises ValueError."""
    raw = (text or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    factor = 1000 if raw.endswith("k") else 1
    raw = raw.removesuffix("k")
    # "20.000" is twenty thousand; "20,5k" is 20500.
    raw = raw.replace(".", "").replace(",", "") if factor == 1 else raw.replace(",", ".")
    value = float(raw) * factor
    if value <= 0:
        raise ValueError(text)
    return value


def amount_text(value: float | None) -> str:
    return "" if value is None else f"{value:,.0f}".replace(",", ".")


def _salary(form: Mapping[str, str], prefix: str, errors: FormErrors) -> Salary | None:
    try:
        amount = parse_amount(form.get(f"{prefix}_amount", ""))
    except ValueError:
        errors.fields[f"{prefix}_amount"] = form.get(f"{prefix}_amount", "")
        return None
    if amount is None:
        return None
    currency = (form.get(f"{prefix}_currency") or "BRL").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        errors.fields[f"{prefix}_currency"] = currency
        return None
    period = form.get(f"{prefix}_period") or "month"
    return Salary(
        minimum=amount, currency=currency, period=period if period in PERIODS else "month"
    )


@dataclass
class ParsedProfile:
    queries: list[Query]
    filters: Filters
    preferences: str
    paused: bool
    errors: FormErrors


def parse_profile(
    form: Mapping[str, str], checked: Mapping[str, list[str]], current: Filters
) -> ParsedProfile:
    """`form`: single values; `checked`: checkbox groups (work_modes, contract_types).

    Fields the form does not show (languages, minimums for other contract types) keep
    their current values.
    """
    errors = FormErrors()
    queries, bad = parse_queries(form.get("queries", ""))
    if bad:
        errors.fields["queries"] = ", ".join(bad)
    countries, bad = parse_countries(form.get("countries", ""))
    if bad:
        errors.fields["countries"] = ", ".join(bad)
    remote_from, bad = parse_countries(form.get("remote_from", ""))
    if bad:
        errors.fields["remote_from"] = ", ".join(bad)
    try:
        max_age = int(form.get("max_age_days") or current.max_age_days)
        if max_age < 1:
            raise ValueError
    except ValueError:
        errors.fields["max_age_days"] = form.get("max_age_days", "")
        max_age = current.max_age_days

    by_contract = {
        k: v for k, v in current.min_salary_by_contract.items() if k not in SALARY_CONTRACTS
    }
    for contract in SALARY_CONTRACTS:
        if (salary := _salary(form, f"min_{contract.value}", errors)) is not None:
            by_contract[contract] = salary

    modes = [WorkMode(m) for m in checked.get("work_modes", []) if m in WorkMode._value2member_map_]
    contracts = [
        ContractType(c)
        for c in checked.get("contract_types", [])
        if c in ContractType._value2member_map_
    ]
    filters = current.model_copy(
        update={
            "countries": countries,
            "cities": lines(form.get("cities", "")),
            "remote_from": remote_from,
            "work_modes": [m for m in modes if m != WorkMode.UNKNOWN],
            "contract_types": contracts,
            "min_salary": _salary(form, "min_any", errors),
            "min_salary_by_contract": by_contract,
            "blocked_companies": lines(form.get("blocked_companies", "")),
            "blocked_keywords": lines(form.get("blocked_keywords", "")),
            "max_age_days": max_age,
        }
    )
    return ParsedProfile(
        queries=queries,
        filters=Filters.model_validate(filters.model_dump()),
        preferences=(form.get("preferences") or "").strip(),
        paused=form.get("paused") == "on",
        errors=errors,
    )
