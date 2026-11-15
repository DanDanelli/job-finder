"""Remote OK: remote jobs through the public API.

Terms (the API's own legal notice, checked on 2026-11-15):
- link back to the job on Remote OK and mention Remote OK as the source;
- do not use the Remote OK logo (the name is fine).

The API returns the latest ~100 jobs in one response (the first element is the legal
notice), so we download it once per run and let the pipeline filter. There is no stated
rate limit; we stay at a few calls a day.
"""

import contextlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

from job_finder.connectors.base import register
from job_finder.domain import Query, RawJob, Salary
from job_finder.quota import Quota
from job_finder.text import html_to_text

URL = "https://remoteok.com/api"
# Salaries are yearly USD. Outside this range they are typos or hourly rates.
_YEARLY_RANGE = (1_000, 2_000_000)


class RemoteOK:
    name = "remoteok"
    daily_limit: int | None = 4

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        quota.consume()
        response = self.http.get(URL)
        response.raise_for_status()
        return [self._convert(j) for j in response.json() if j.get("id") and j.get("url")]

    def _convert(self, job: dict[str, Any]) -> RawJob:
        epoch = job.get("epoch")
        return RawJob(
            source=self.name,
            source_id=str(job["id"]),
            title=_text(job.get("position")) or "",
            company=_text(job.get("company")),
            location=_text(job.get("location")),
            description=html_to_text(_text(job.get("description"))),
            url=job["url"],  # terms: link back to Remote OK
            posted_at=datetime.fromtimestamp(int(epoch), UTC) if epoch else None,
            work_mode="remote",
            salary=_salary(job),
            tags=[str(t) for t in job.get("tags") or []],
        )


def _text(value: Any) -> str | None:
    """Strips and undoes the double UTF-8 encoding the API sometimes sends
    ("MÃ¼nchen" -> "München")."""
    if not value:
        return None
    text = str(value).strip()
    with contextlib.suppress(UnicodeEncodeError, UnicodeDecodeError):
        text = text.encode("latin-1").decode("utf-8")
    return text or None


def _salary(job: dict[str, Any]) -> Salary | None:
    low, high = _YEARLY_RANGE
    values = [int(v) for k in ("salary_min", "salary_max") if (v := job.get(k))]
    if not values or not all(low <= v <= high for v in values):
        return None
    return Salary(minimum=min(values), maximum=max(values), currency="USD", period="year")


@register("remoteok")
def _factory(http: httpx.Client) -> RemoteOK:
    return RemoteOK(http)
