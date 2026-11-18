"""Adzuna: job search engine with an official API (Brazil and ~18 other countries).

Terms (developer.adzuna.com/docs/terms_of_service, checked on 2026-11-15):
- personal job search is a permitted use; commercial use needs a licence;
- every displayed job carries the word "Adzuna" linking to Adzuna;
- Adzuna's salary estimates need their own credit, so predicted salaries are dropped;
- no resale of the data; data is removed if the key is revoked.

API: GET https://api.adzuna.com/v1/api/jobs/<country>/search/<page>, with `app_id` and
`app_key` in the query string (the only way the API accepts them). The free plan's
limits show only in the account; we stay well below the commonly reported 250/day.

Because the key travels in the URL, HTTP errors are re-raised without it: run errors
are stored and shown in the digest.

Each run makes a few searches, rotating through the (term, country) pairs of every
profile, like JSearch. A failed search is logged with its term and skipped; the
connector fails only when every search of the run failed (seen in November 2026:
HTTP 503 on Brazil, which used to throw away the searches that had worked).
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from job_finder.config import Settings, get_settings
from job_finder.connectors.base import register
from job_finder.domain import Query, RawJob, Salary
from job_finder.pipeline.locations import parse_location
from job_finder.quota import Quota
from job_finder.text import html_to_text

log = logging.getLogger(__name__)

URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
# Country -> (Adzuna site, salary currency).
COUNTRIES: dict[str, tuple[str, str]] = {
    "at": ("https://www.adzuna.at", "EUR"),
    "au": ("https://www.adzuna.com.au", "AUD"),
    "be": ("https://www.adzuna.be", "EUR"),
    "br": ("https://www.adzuna.com.br", "BRL"),
    "ca": ("https://www.adzuna.ca", "CAD"),
    "ch": ("https://www.adzuna.ch", "CHF"),
    "de": ("https://www.adzuna.de", "EUR"),
    "es": ("https://www.adzuna.es", "EUR"),
    "fr": ("https://www.adzuna.fr", "EUR"),
    "gb": ("https://www.adzuna.co.uk", "GBP"),
    "in": ("https://www.adzuna.in", "INR"),
    "it": ("https://www.adzuna.it", "EUR"),
    "mx": ("https://www.adzuna.com.mx", "MXN"),
    "nl": ("https://www.adzuna.nl", "EUR"),
    "nz": ("https://www.adzuna.co.nz", "NZD"),
    "pl": ("https://www.adzuna.pl", "PLN"),
    "sg": ("https://www.adzuna.sg", "SGD"),
    "us": ("https://www.adzuna.com", "USD"),
    "za": ("https://www.adzuna.co.za", "ZAR"),
}
RESULTS_PER_PAGE = 50


class Adzuna:
    name = "adzuna"

    def __init__(self, http: httpx.Client, settings: Settings) -> None:
        self.http = http
        self.settings = settings
        self.daily_limit: int | None = settings.adzuna_daily_limit
        self.monthly_limit: int | None = settings.adzuna_monthly_limit

    def pairs(self, queries: Sequence[Query]) -> list[tuple[str, str]]:
        """(term, country) pairs, in rotation order. A query with no country uses the
        defaults; countries Adzuna does not cover are left out."""
        pairs: list[tuple[str, str]] = []
        for q in queries:
            for country in q.countries or self.settings.adzuna_countries:
                pair = (q.term, country.lower())
                if pair[1] in COUNTRIES and pair not in pairs:
                    pairs.append(pair)
        return pairs

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        if self.settings.adzuna_app_id is None or self.settings.adzuna_app_key is None:
            raise RuntimeError("JOBFINDER_ADZUNA_APP_ID / JOBFINDER_ADZUNA_APP_KEY are not set")
        pairs = self.pairs(queries)
        if not pairs:
            return []
        start = quota.used_this_month()
        n = min(self.settings.adzuna_searches_per_run, len(pairs))
        jobs: dict[str, RawJob] = {}
        failures: list[RuntimeError] = []
        for i in range(n):
            term, country = pairs[(start + i) % len(pairs)]
            quota.consume()
            try:
                items = self._search(term, country)
            except RuntimeError as e:
                log.warning("%s, search %r skipped", e, term)
                failures.append(e)
                continue
            for item in items:
                job = self._convert(item, country)
                if job is not None:
                    jobs.setdefault(job.source_id, job)
        if failures and len(failures) == n:
            raise failures[-1]
        return list(jobs.values())

    def _search(self, term: str, country: str) -> list[dict[str, Any]]:
        assert self.settings.adzuna_app_id and self.settings.adzuna_app_key
        try:
            response = self.http.get(
                URL.format(country=country),
                params={
                    "app_id": self.settings.adzuna_app_id.get_secret_value(),
                    "app_key": self.settings.adzuna_app_key.get_secret_value(),
                    "what": term,
                    "results_per_page": RESULTS_PER_PAGE,
                    "max_days_old": self.settings.adzuna_max_days_old,
                    "sort_by": "date",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"Adzuna: HTTP {e.response.status_code} ({country})") from None
        except httpx.HTTPError as e:
            raise RuntimeError(f"Adzuna: {type(e).__name__} ({country})") from None
        return list(response.json().get("results") or [])

    def _convert(self, job: dict[str, Any], country: str) -> RawJob | None:
        url, title = job.get("redirect_url"), (job.get("title") or "").strip()
        if not job.get("id") or not url or not title:
            return None
        site, currency = COUNTRIES[country]
        location = ((job.get("location") or {}).get("display_name") or "").strip()
        # "São Paulo, SP" does not say the country; the search does.
        code = country.upper()
        if parse_location(location) != [code]:
            location = f"{location}, {code}" if location else code
        created = job.get("created")
        return RawJob(
            source=self.name,
            source_id=f"{country}:{job['id']}",
            # Titles come with <strong> around the search terms.
            title=" ".join((html_to_text(title) or title).split()),
            company=((job.get("company") or {}).get("display_name") or "").strip() or None,
            location=location,
            description=html_to_text(job.get("description")),
            url=url,  # an Adzuna page that leads to the job
            posted_at=datetime.fromisoformat(created) if created else None,
            contract_type=job.get("contract_type") or job.get("contract_time") or None,
            salary=_salary(job, currency),
            tags=[label] if (label := (job.get("category") or {}).get("label")) else [],
            raw_data={"site": site, "contract_time": job.get("contract_time")},
        )


def _salary(job: dict[str, Any], currency: str) -> Salary | None:
    if str(job.get("salary_is_predicted") or "0") != "0":
        return None  # Adzuna's estimate: shown only with its own credit
    minimum, maximum = job.get("salary_min"), job.get("salary_max")
    if minimum is None and maximum is None:
        return None
    return Salary(minimum=minimum, maximum=maximum, currency=currency, period="year")


@register("adzuna")
def _factory(http: httpx.Client) -> Adzuna:
    return Adzuna(http, get_settings())
