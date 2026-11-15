"""Deduplication: the same job coming from different sources becomes one record.

1. Exact key: normalized company, title and place (country, "remote" or "?").
2. Fuzzy match, when the key misses: same company, compatible place (an unknown place
   matches any) and titles at least FUZZY_THRESHOLD% alike once the words that only
   describe the arrangement ("remote", "LATAM", "hybrid") are left out. Seniority words
   stay, so "Data Engineer" and "Senior Data Engineer" remain two jobs.
"""

import re

from rapidfuzz import fuzz

from job_finder.domain import Job, WorkMode
from job_finder.text import key

# Gender markers common in European postings: (m/f/d), (m/w/d), (h/f), (f/m/x)...
_GENDER = re.compile(r"\(\s*[mfwdhx]\s*(?:/\s*[mfwdhx]\s*)+\)", re.IGNORECASE)
_ABBREVIATIONS = {"sr": "senior", "jr": "junior", "mgr": "manager", "eng": "engineer"}
_COMPANY_SUFFIXES = {
    "inc", "llc", "ltd", "ltda", "lda", "gmbh", "ag", "sa", "bv", "plc", "corp",
    "co", "limited", "sl", "srl", "spa",
}  # fmt: skip


def canonical_title(title: str) -> str:
    without_gender = _GENDER.sub(" ", title)
    words = [_ABBREVIATIONS.get(w, w) for w in key(without_gender).split()]
    return " ".join(words)


def canonical_company(company: str | None) -> str:
    if not company:
        return ""
    # "S.A." becomes "s a" after key(); join it to match the "sa" suffix.
    words = re.sub(r"\bs a$", "sa", key(company)).split()
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def dedup_key(job: Job) -> str:
    if job.country:
        place = job.country.lower()
    elif job.work_mode == WorkMode.REMOTE:
        place = "remote"
    else:
        place = UNKNOWN_PLACE
    return f"{canonical_company(job.company)}|{canonical_title(job.title)}|{place}"


FUZZY_THRESHOLD = 90
UNKNOWN_PLACE = "?"
# Words that say where or how, not what, in titles like "Brand Manager (LATAM - Remote)".
_ARRANGEMENT = {
    "remote", "remoto", "remota", "hybrid", "hibrido", "hibrida", "presencial", "onsite",
    "home", "office", "latam", "latin", "america", "worldwide", "anywhere", "global",
    "brazil", "brasil", "only", "vaga", "job",
}  # fmt: skip


def split_key(key: str) -> tuple[str, str, str]:
    company, title, place = key.split("|")
    return company, title, place


def _comparable(title: str) -> str:
    return " ".join(w for w in title.split() if w not in _ARRANGEMENT)


def is_fuzzy_duplicate(key_a: str, key_b: str) -> bool:
    company_a, title_a, place_a = split_key(key_a)
    company_b, title_b, place_b = split_key(key_b)
    if not company_a or company_a != company_b:
        return False
    if place_a != place_b and UNKNOWN_PLACE not in (place_a, place_b):
        return False
    a, b = _comparable(title_a), _comparable(title_b)
    return bool(a and b) and fuzz.token_sort_ratio(a, b) >= FUZZY_THRESHOLD
