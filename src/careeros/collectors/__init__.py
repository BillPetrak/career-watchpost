"""Collector registry for CareerOS job sources."""

from __future__ import annotations

from typing import Protocol, TypeAlias

from careeros.collectors.arbeitnow import fetch_jobs as arbeitnow_fetch_jobs
from careeros.collectors.http import DEFAULT_TIMEOUT
from careeros.collectors.jobicy import fetch_jobs as jobicy_fetch_jobs
from careeros.collectors.models import CollectedJob
from careeros.collectors.remoteok import fetch_jobs as remoteok_fetch_jobs
from careeros.collectors.remotive import fetch_jobs as remotive_fetch_jobs
from careeros.collectors.weworkremotely import fetch_jobs as weworkremotely_fetch_jobs
from careeros.collectors.workingnomads import fetch_jobs as workingnomads_fetch_jobs


class Collector(Protocol):
    """Signature shared by all job-board collector fetch functions."""

    def __call__(
        self,
        *,
        limit: int = 25,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> list[CollectedJob]: ...


CollectorName: TypeAlias = str


# Dict preserves insertion order, so the registry order is the stable execution
# order for multi-source collection (arbeitnow, remoteok, weworkremotely,
# jobicy, remotive, workingnomads). Orchestration derives its order from these
# keys rather than keeping a separate source of truth.
COLLECTORS: dict[CollectorName, Collector] = {
    "arbeitnow": arbeitnow_fetch_jobs,
    "remoteok": remoteok_fetch_jobs,
    "weworkremotely": weworkremotely_fetch_jobs,
    "jobicy": jobicy_fetch_jobs,
    "remotive": remotive_fetch_jobs,
    "workingnomads": workingnomads_fetch_jobs,
}
