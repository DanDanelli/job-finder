"""Parsing free-text locations and the eligibility of remote jobs.

A location becomes a list of codes: ISO alpha-2 countries ("BR") or regions ("LATAM").
"""

import re
from functools import cache

import pycountry

from job_finder.text import key

WORLDWIDE = "WORLDWIDE"

# fmt: off
_EU = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}
_EUROPE = _EU | {
    "AL", "AD", "BA", "BY", "CH", "FO", "GB", "GI", "IS", "LI", "MC", "MD", "ME", "MK",
    "NO", "RS", "SM", "UA", "VA", "XK",
}
_LATAM = {
    "AR", "BO", "BR", "CL", "CO", "CR", "CU", "DO", "EC", "GT", "HN", "MX", "NI", "PA",
    "PE", "PR", "PY", "SV", "UY", "VE",
}
_NORTH_AMERICA = {"US", "CA", "MX"}
_APAC = {
    "AU", "BD", "CN", "FJ", "HK", "ID", "IN", "JP", "KH", "KR", "LK", "MM", "MN", "MY",
    "NP", "NZ", "PH", "PK", "SG", "TH", "TW", "VN",
}
_MIDDLE_EAST = {"AE", "BH", "IL", "IQ", "JO", "KW", "LB", "OM", "QA", "SA", "TR"}
_AFRICA = {
    "DZ", "AO", "CM", "CI", "EG", "ET", "GH", "KE", "MA", "MU", "MZ", "NG", "RW", "SN",
    "TN", "TZ", "UG", "ZA", "ZM", "ZW",
}
# fmt: on

REGIONS: dict[str, frozenset[str]] = {
    "EU": frozenset(_EU),
    "EUROPE": frozenset(_EUROPE),
    "LATAM": frozenset(_LATAM),
    "NORTH_AMERICA": frozenset(_NORTH_AMERICA),
    "AMERICAS": frozenset(_LATAM | _NORTH_AMERICA),
    "APAC": frozenset(_APAC),
    "EMEA": frozenset(_EUROPE | _MIDDLE_EAST | _AFRICA),
}

_REGION_ALIASES = {
    "worldwide": WORLDWIDE,
    "anywhere": WORLDWIDE,
    "global": WORLDWIDE,
    "qualquer lugar": WORLDWIDE,
    "europe": "EUROPE",
    "europa": "EUROPE",
    "eu": "EU",
    "european union": "EU",
    "uniao europeia": "EU",
    "latam": "LATAM",
    "latin america": "LATAM",
    "south america": "LATAM",
    "america latina": "LATAM",
    "north america": "NORTH_AMERICA",
    "america do norte": "NORTH_AMERICA",
    "americas": "AMERICAS",
    "apac": "APAC",
    "asia pacific": "APAC",
    "asia": "APAC",
    "emea": "EMEA",
}

_COUNTRY_ALIASES = {
    "usa": "US",
    "us": "US",
    "u s": "US",
    "u s a": "US",
    "united states": "US",
    "estados unidos": "US",
    "eua": "US",
    "uk": "GB",
    "england": "GB",
    "great britain": "GB",
    "reino unido": "GB",
    "brasil": "BR",
    "alemanha": "DE",
    "espanha": "ES",
    "franca": "FR",
    "italia": "IT",
    "holanda": "NL",
    "paises baixos": "NL",
    "the netherlands": "NL",
    "irlanda": "IE",
    "suica": "CH",
    "belgica": "BE",
    "canada": "CA",
    "mexico": "MX",
    "colombia": "CO",
    "russia": "RU",
    "south korea": "KR",
    "uae": "AE",
    # Local names common in European postings
    "deutschland": "DE",
    "osterreich": "AT",
    "schweiz": "CH",
    "suisse": "CH",
    "nederland": "NL",
    "espana": "ES",
    "belgie": "BE",
    "belgique": "BE",
    "polska": "PL",
    "sverige": "SE",
    "danmark": "DK",
    "norge": "NO",
}

# Brazilian state codes. Several clash with country codes (MG = Madagascar, ES = Spain,
# PA = Panama, PE = Peru...): "Contagem - MG, Brasil" is not Madagascar.
_BR_STATES = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA",
    "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
}  # fmt: skip

# Words that come with a location but do not change its meaning.
_NOISE = re.compile(r"\b(only|timezones?|time zones?|based|remote|remoto|apenas|somente)\b")
_SEPARATORS = re.compile(r"[,;/|()]|\s+[-\u2013]\s+|\s+(?:or|and|ou|e)\s+|\s+&\s+")


@cache
def _country(name: str) -> str | None:
    if name in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[name]
    try:
        return str(pycountry.countries.lookup(name).alpha_2)
    except LookupError:
        return None


def parse_location(text: str | None) -> list[str]:
    """Turns "LATAM, Europe, USA" into ["LATAM", "EUROPE", "US"].

    Unrecognized pieces (cities, for example) are ignored. An empty list means nothing
    could be parsed.
    """
    if not text:
        return []
    codes: list[str] = []
    two_letter: set[str] = set()
    for piece in _SEPARATORS.split(text):
        if not piece:
            continue
        name = key(_NOISE.sub(" ", key(piece)))
        if not name:
            continue
        code = _REGION_ALIASES.get(name) or _country(name)
        if code and len(name) == 2:
            two_letter.add(code)
        if code and code not in codes:
            codes.append(code)
    if "BR" in codes:
        # With Brazil explicit, a state code is a state, not a country.
        codes = [c for c in codes if not (c in two_letter and c in _BR_STATES and c != "BR")]
    return codes


def is_eligible(locations: list[str], person_countries: list[str]) -> bool | None:
    """Can a person living in one of `person_countries` take the job?

    Returns None when the job's locations are unknown.
    """
    if not locations:
        return None
    if WORLDWIDE in locations:
        return True
    for location in locations:
        covered = REGIONS.get(location, frozenset({location}))
        if any(c in covered for c in person_countries):
            return True
    return False
