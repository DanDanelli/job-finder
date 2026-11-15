"""Himalayas: remote jobs through the public search API.

Terms (himalayas.app/terms#api and the API docs, checked on 2026-11-15):
- personal and commercial use allowed, shown only in apps we operate;
- every "apply" must use the API's `applicationLink`;
- credit Himalayas with a visible link to the job or to himalayas.app;
- do not pass the jobs on to other job boards, aggregators or search engines;
- cache where possible, stay within the limits and drop expired jobs.

API: GET https://himalayas.app/jobs/api/search?q=<term>&country=<ISO code>, 20 jobs per
page. An optional free key (`Authorization: Bearer`) raises the limit to 10,000
requests/day; without one, every caller shares a public allowance. The data refreshes
about once a day.

Each run makes a few searches, rotating through the (term, country) pairs of every
profile, like JSearch. Results include jobs open worldwide.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from job_finder.config import Settings, get_settings
from job_finder.connectors.base import register
from job_finder.domain import Query, RawJob, Salary, SalaryPeriod
from job_finder.quota import Quota
from job_finder.text import html_to_text

URL = "https://himalayas.app/jobs/api/search"
# Weekly and fortnightly salaries are converted to yearly.
_PERIODS: dict[str, tuple[SalaryPeriod, int]] = {
    "annual": ("year", 1),
    "monthly": ("month", 1),
    "hourly": ("hour", 1),
    "weekly": ("year", 52),
    "fortnightly": ("year", 26),
}


class Himalayas:
    name = "himalayas"

    def __init__(
        self,
        http: httpx.Client,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http = http
        self.settings = settings
        self.clock = clock
        self.daily_limit: int | None = settings.himalayas_daily_limit

    def pairs(self, queries: Sequence[Query]) -> list[tuple[str, str]]:
        """(term, country) pairs, in rotation order. A query with no country uses the
        defaults."""
        pairs: list[tuple[str, str]] = []
        for q in queries:
            for country in q.countries or self.settings.himalayas_countries:
                pair = (q.term, country.upper())
                if pair not in pairs:
                    pairs.append(pair)
        return pairs

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        pairs = self.pairs(queries)
        if not pairs:
            return []
        start = quota.used_this_month()
        n = min(self.settings.himalayas_searches_per_run, len(pairs))
        now = self.clock()
        jobs: dict[str, RawJob] = {}
        for i in range(n):
            term, country = pairs[(start + i) % len(pairs)]
            quota.consume()
            for item in self._search(term, country):
                job = self._convert(item, now)
                if job is not None:
                    jobs.setdefault(job.source_id, job)
        return list(jobs.values())

    def _search(self, term: str, country: str) -> list[dict[str, Any]]:
        headers = {}
        if key := self.settings.himalayas_api_key:
            headers["Authorization"] = f"Bearer {key.get_secret_value()}"
        response = self.http.get(URL, params={"q": term, "country": country}, headers=headers)
        response.raise_for_status()
        return list(response.json().get("jobs") or [])

    def _convert(self, job: dict[str, Any], now: datetime) -> RawJob | None:
        link, guid = job.get("applicationLink"), job.get("guid")
        title = (job.get("title") or "").strip()
        if not link or not guid or not title:
            return None
        expiry = job.get("expiryDate")
        if expiry and datetime.fromtimestamp(int(expiry), UTC) <= now:
            return None  # terms: drop expired jobs
        published = job.get("pubDate")
        return RawJob(
            source=self.name,
            source_id=str(guid).removeprefix("https://himalayas.app/companies/"),
            title=title,
            company=(job.get("companyName") or "").strip() or None,
            # Country names; an empty list means open worldwide.
            location=", ".join(job.get("locationRestrictions") or []) or "Worldwide",
            description=html_to_text(job.get("description")),
            url=link,  # terms: apply through applicationLink (a Himalayas page)
            posted_at=datetime.fromtimestamp(int(published), UTC) if published else None,
            work_mode="remote",
            contract_type=job.get("employmentType") or None,
            salary=_salary(job),
            tags=[str(c) for c in job.get("categories") or []],
            raw_data={
                "seniority": job.get("seniority"),
                "expires_at": int(expiry) if expiry else None,
            },
        )


def _salary(job: dict[str, Any]) -> Salary | None:
    minimum, maximum = job.get("minSalary"), job.get("maxSalary")
    period = _PERIODS.get(str(job.get("salaryPeriod") or "annual").lower())
    if (minimum is None and maximum is None) or period is None:
        return None
    unit, factor = period
    return Salary(
        minimum=minimum * factor if minimum is not None else None,
        maximum=maximum * factor if maximum is not None else None,
        currency=job.get("currency"),
        period=unit,
    )


@register("himalayas")
def _factory(http: httpx.Client) -> Himalayas:
    return Himalayas(http, get_settings())
