"""Collector registry for CareerOS job sources."""

from __future__ import annotations

from typing import Protocol

from careeros.collectors.arbeitnow import fetch_jobs as arbeitnow_fetch_jobs
from careeros.collectors.http import DEFAULT_TIMEOUT
from careeros.collectors.models import CollectedJob
from careeros.collectors.remoteok import fetch_jobs as remoteok_fetch_jobs


class Collector(Protocol):
    """Signature shared by all job-board collector fetch functions."""

    def __call__(
        self,
        *,
        limit: int = 25,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> list[CollectedJob]: ...


COLLECTORS: dict[str, Collector] = {
    "arbeitnow": arbeitnow_fetch_jobs,
    "remoteok": remoteok_fetch_jobs,
}
