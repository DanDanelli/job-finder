"""We Work Remotely: remote jobs through the public RSS feeds.

Terms (weworkremotely.com/remote-job-rss-feed, checked on 2026-11-15 through the
Internet Archive copy of 2026-11-13, since the site blocks automated page views):
"Anyone can use the feed, all we ask is that you attribute the links back to We Work
Remotely." Every job links to its We Work Remotely page and the digest credits the
source. (WWR's separate API is for partners posting jobs; it is not used here.)

There is one feed per category, each with every open job in it (tens to ~150), so each
run downloads the configured category feeds, one request each, and lets the pipeline
filter. Feeds are parsed with defusedxml.

Fields: the title comes as "Company: Title"; `country` lists the countries a person
may live in, each with its flag; `region` is the broad area ("Anywhere in the World",
"USA Only") and counts only when `country` is empty; `state` is the company's
headquarters and is ignored.
"""

import contextlib
import email.utils
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import cache
from xml.etree.ElementTree import Element

import httpx
import pycountry
from defusedxml.ElementTree import fromstring

from job_finder.config import Settings, get_settings
from job_finder.connectors.base import register
from job_finder.domain import Query, RawJob
from job_finder.pipeline.locations import parse_location
from job_finder.quota import Quota, QuotaExhausted
from job_finder.text import html_to_text

FEED = "https://weworkremotely.com/categories/{category}.rss"
# A flag emoji is two regional indicator letters spelling the ISO alpha-2 code.
_FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
_REGIONS = {"anywhere in the world": "Worldwide"}


class WeWorkRemotely:
    name = "weworkremotely"

    def __init__(
        self,
        http: httpx.Client,
        settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http = http
        self.settings = settings
        self.clock = clock
        self.daily_limit: int | None = settings.weworkremotely_daily_limit

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        now = self.clock()
        jobs: dict[str, RawJob] = {}
        for i, category in enumerate(self.settings.weworkremotely_categories):
            try:
                quota.consume()
            except QuotaExhausted:
                if i == 0:
                    raise
                break  # keep the feeds already downloaded
            response = self.http.get(FEED.format(category=category))
            response.raise_for_status()
            for item in fromstring(response.content).iterfind("./channel/item"):
                job = self._convert(item, now)
                if job is not None:
                    jobs.setdefault(job.source_id, job)
        return list(jobs.values())

    def _convert(self, item: Element, now: datetime) -> RawJob | None:
        link, full_title = _field(item, "link"), _field(item, "title")
        if not link or not full_title:
            return None
        expires = _date(_field(item, "expires_at"))
        if expires and expires <= now:
            return None
        company, sep, title = full_title.partition(": ")
        if not sep:
            company, title = "", full_title
        skills = [s.strip() for s in re.split(r",|\band\b", _field(item, "skills") or "")]
        return RawJob(
            source=self.name,
            source_id=(_field(item, "guid") or link).removeprefix(
                "https://weworkremotely.com/remote-jobs/"
            ),
            title=title.strip(),
            company=company.strip() or None,
            location=_location(_field(item, "country"), _field(item, "region")),
            description=html_to_text(_field(item, "description")),
            url=link,  # terms: link back to We Work Remotely
            posted_at=_date(_field(item, "pubDate")),
            work_mode="remote",
            contract_type=_field(item, "type"),
            tags=[t for t in [_field(item, "category"), *skills] if t],
            raw_data={"expires_at": expires.isoformat() if expires else None},
        )


def _field(item: Element, tag: str) -> str | None:
    return (item.findtext(tag) or "").strip() or None


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return email.utils.parsedate_to_datetime(text)
    return None


def _location(countries: str | None, region: str | None) -> str | None:
    codes = [
        "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in flag)
        for flag in _FLAG.findall(countries or "")
    ]
    if codes:
        return ", ".join(_place(code) for code in dict.fromkeys(codes))
    if region:
        return _REGIONS.get(region.lower(), region)
    return None


@cache
def _place(alpha_2: str) -> str:
    """A name the location parser reads back as `alpha_2`: the common name when it
    survives the parser ("Bolivia", "Brazil"), else the alpha-3 code (names such as
    "Bosnia and Herzegovina" get split on "and")."""
    country = pycountry.countries.get(alpha_2=alpha_2)
    if country is None:
        return alpha_2
    name = getattr(country, "common_name", None) or country.name
    return name if parse_location(name) == [alpha_2] else str(country.alpha_3)


@register("weworkremotely")
def _factory(http: httpx.Client) -> WeWorkRemotely:
    return WeWorkRemotely(http, get_settings())
