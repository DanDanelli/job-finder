"""Common connector interface and the registry of available connectors."""

from collections.abc import Callable, Sequence
from typing import Protocol

import httpx

from job_finder import __version__
from job_finder.domain import Query, RawJob
from job_finder.quota import Quota

USER_AGENT = f"job-finder/{__version__} (personal use)"


class Connector(Protocol):
    name: str
    # HTTP calls allowed per day (source's terms or free tier). None = no limit.
    daily_limit: int | None

    def search(self, queries: Sequence[Query], quota: Quota) -> list[RawJob]:
        """Searches jobs for the set of queries of all profiles.

        Each connector picks its strategy: one call per query, or a whole catalog
        filtered afterwards. Every HTTP call goes through `quota.consume()`.
        """
        ...


ConnectorFactory = Callable[[httpx.Client], Connector]
_REGISTRY: dict[str, ConnectorFactory] = {}


def register(name: str) -> Callable[[ConnectorFactory], ConnectorFactory]:
    def decorator(factory: ConnectorFactory) -> ConnectorFactory:
        _REGISTRY[name] = factory
        return factory

    return decorator


def available() -> list[str]:
    return sorted(_REGISTRY)


def create(name: str, http: httpx.Client) -> Connector:
    try:
        return _REGISTRY[name](http)
    except KeyError:
        raise ValueError(f"unknown connector: {name} (available: {available()})") from None


def http_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    )
