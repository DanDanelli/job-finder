"""Dedup key: the same job coming from different sources yields the same key.

Fuzzy matching between similar titles comes in milestone 5.
"""

import re

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
        place = "?"
    return f"{canonical_company(job.company)}|{canonical_title(job.title)}|{place}"
