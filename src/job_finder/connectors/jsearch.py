"""JSearch (OpenWeb Ninja, direct access): Google for Jobs listings.

Google for Jobs indexes the jobs that LinkedIn, Indeed, Glassdoor, Vagas.com and others
publish; JSearch serves that index through an API. Each job carries its origin site
(`job_publisher`), which shows in the digest.

API: GET https://api.openwebninja.com/jsearch/search-v2, key in the `x-api-key` header
(official OpenAPI spec, checked on 2026-11-15). Each page (up to 10 jobs) uses one
request from the quota.

Quota: the free plan is small, so each run makes only a few searches, rotating through
the (term, country) pairs of every profile. The rotation cursor is the number of calls
already made this month, so no extra state is needed.
"""

import base64
import binascii
import hashlib
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from job_finder.config import Settings, get_settings
from job_finder.connectors.base import register
from job_finder.domain import Query, RawJob, Salary, SalaryPeriod
from job_finder.pipeline.locations import parse_location
from job_finder.quota import Quota

URL = "https://api.openwebninja.com/jsearch/search-v2"
PERIODS: dict[str, SalaryPeriod] = {"YEAR": "year", "MONTH": "month", "HOUR": "hour"}
# "há 22 horas", "há 1 dia", "há 2 semanas", "há 1 mês"; or in English, "3 days ago".
_AGE = re.compile(
    r"(\d+)\+?\s*(minuto|minute|hora|hour|dia|day|semana|week|m[eê]s|meses|month)", re.I
)
_UNITS = {
    "minut": timedelta(minutes=1),
    "hora": timedelta(hours=1),
    "hour": timedelta(hours=1),
    "dia": timedelta(days=1),
    "day": timedelta(days=1),
    "semana": timedelta(weeks=1),
    "week": timedelta(weeks=1),
    "m": timedelta(days=30),  # mês, meses, month
}


class JSearch:
    name = "jsearch"

    def __init__(
        self,
        http: httpx.Client,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http = http
        self.settings = settings
        self.clock = clock
        self.daily_limit: int | None = settings.jsearch_daily_limit
        self.monthly_limit: int | None = settings.jsearch_monthly_limit

    def pairs(self, queries: Sequence[Query]) -> list[tuple[str, str]]:
        """(term, country) pairs, in rotation order. A query with no country uses the
        defaults."""
        pairs: list[tuple[str, str]] = []
        for q in queries:
            for country in q.countries or self.settings.jsearch_countries:
                pair = (q.term, country.lower())
                if pair not in pairs:
                    pairs.append(pair)
        return pairs

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        if self.settings.jsearch_api_key is None:
            raise RuntimeError("JOBFINDER_JSEARCH_API_KEY is not set")
        pairs = self.pairs(queries)
        if not pairs:
            return []
        start = quota.used_this_month()
        n = min(self.settings.jsearch_searches_per_run, len(pairs))
        jobs: dict[str, RawJob] = {}
        for i in range(n):
            term, country = pairs[(start + i) % len(pairs)]
            quota.consume()
            for item in self._search(term, country):
                job = self._convert(item)
                if job is not None:
                    jobs.setdefault(job.source_id, job)
        return list(jobs.values())

    def _search(self, term: str, country: str) -> list[dict[str, Any]]:
        assert self.settings.jsearch_api_key is not None
        response = self.http.get(
            URL,
            params={
                "query": term,
                "country": country,
                "num_pages": 1,
                "date_posted": self.settings.jsearch_date_posted,
            },
            headers={"x-api-key": self.settings.jsearch_api_key.get_secret_value()},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") not in (None, "OK"):
            raise RuntimeError(f"JSearch: {body.get('status')}: {body.get('error')}")
        return list((body.get("data") or {}).get("jobs") or [])

    def _convert(self, job: dict[str, Any]) -> RawJob | None:
        url = job.get("job_apply_link") or job.get("job_google_link")
        if not job.get("job_id") or not job.get("job_title") or not url:
            return None
        direct = next(
            (o.get("apply_link") for o in job.get("apply_options") or [] if o.get("is_direct")),
            None,
        )
        location = job.get("job_location") or ", ".join(
            p for p in (job.get("job_city"), job.get("job_state")) if p
        )
        # "Contagem - MG" does not say the country (and MG looks like Madagascar);
        # job_country does.
        country = job.get("job_country")
        if country and parse_location(location) != [country]:
            location = f"{location}, {country}" if location else country
        return RawJob(
            source=self.name,
            source_id=_stable_id(str(job["job_id"])),
            title=job["job_title"],
            company=job.get("employer_name") or None,
            location=location or None,
            description=job.get("job_description") or None,
            url=url,
            apply_url=direct,
            posted_at=self._posted_at(job),
            work_mode="remote" if job.get("job_is_remote") else None,
            contract_type=job.get("job_employment_type") or None,
            salary=_salary(job),
            salary_text=job.get("job_salary_string") or None,
            raw_data={
                "publisher": job.get("job_publisher"),
                "google_link": job.get("job_google_link"),
            },
        )

    def _posted_at(self, job: dict[str, Any]) -> datetime | None:
        if iso := job.get("job_posted_at_datetime_utc"):
            return datetime.fromisoformat(iso)
        if ts := job.get("job_posted_at_timestamp"):
            return datetime.fromtimestamp(ts, UTC)
        m = _AGE.search(job.get("job_posted_at") or "")
        if not m:
            return None
        unit = next(d for k, d in _UNITS.items() if m.group(2).lower().startswith(k))
        return self.clock() - int(m.group(1)) * unit


def _stable_id(job_id: str) -> str:
    """job_id is base64 of "<Google docid>:<search token>"; the token changes on every
    search, so the same job would come back as new. Uses only the docid."""
    try:
        decoded = base64.urlsafe_b64decode(job_id + "=" * (-len(job_id) % 4))
        docid = decoded.split(b":", 1)[0].decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        docid = ""
    if docid and len(docid) <= 64:
        return docid
    return hashlib.sha256(job_id.encode()).hexdigest()[:32]


def _salary(job: dict[str, Any]) -> Salary | None:
    minimum, maximum = job.get("job_min_salary"), job.get("job_max_salary")
    if minimum is None and maximum is None:
        return None
    return Salary(
        minimum=minimum,
        maximum=maximum,
        currency=job.get("job_salary_currency"),
        period=PERIODS.get(str(job.get("job_salary_period") or "").upper()),
    )


@register("jsearch")
def _factory(http: httpx.Client) -> JSearch:
    return JSearch(http, get_settings())
