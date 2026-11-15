"""Remotive: remote jobs through the public API.

Terms (the API's own legal notice, checked on 2026-11-15):
- at most ~4 requests per day;
- link to the job on Remotive and credit Remotive as the source;
- jobs arrive with a 24h delay.

The public catalog is small (16 jobs on 2026-11-15) and the `search` parameter is
imprecise, so we download the whole catalog once per run and let the pipeline filter.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx

from job_finder.connectors.base import register
from job_finder.domain import Query, RawJob
from job_finder.quota import Quota
from job_finder.text import html_to_text

URL = "https://remotive.com/api/remote-jobs"


class Remotive:
    name = "remotive"
    daily_limit: int | None = 4

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        quota.consume()
        response = self.http.get(URL)
        response.raise_for_status()
        return [self._convert(job) for job in response.json().get("jobs", [])]

    def _convert(self, job: dict[str, Any]) -> RawJob:
        published = job.get("publication_date")
        return RawJob(
            source=self.name,
            source_id=str(job["id"]),
            title=job["title"],
            company=(job.get("company_name") or "").strip() or None,
            location=job.get("candidate_required_location") or None,
            description=html_to_text(job.get("description")),
            url=job["url"],
            posted_at=datetime.fromisoformat(published) if published else None,
            work_mode="remote",
            contract_type=job.get("job_type") or None,
            salary_text=job.get("salary") or None,
            tags=[str(t) for t in job.get("tags") or []],
            raw_data={"category": job.get("category")},
        )


@register("remotive")
def _factory(http: httpx.Client) -> Remotive:
    return Remotive(http)
