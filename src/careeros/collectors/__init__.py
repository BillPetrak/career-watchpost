"""Collector registry for CareerOS job sources."""

from __future__ import annotations

from typing import Protocol, TypeAlias

from careeros.collectors.arbeitnow import fetch_jobs as arbeitnow_fetch_jobs
from careeros.collectors.greenhouse import fetch_result as greenhouse_fetch_result
from careeros.collectors.himalayas import fetch_jobs as himalayas_fetch_jobs
from careeros.collectors.http import DEFAULT_TIMEOUT
from careeros.collectors.jobicy import fetch_jobs as jobicy_fetch_jobs
from careeros.collectors.lever import fetch_result as lever_fetch_result
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.collectors.remoteok import fetch_jobs as remoteok_fetch_jobs
from careeros.collectors.remotive import fetch_jobs as remotive_fetch_jobs
from careeros.collectors.weworkremotely import fetch_jobs as weworkremotely_fetch_jobs
from careeros.collectors.workable import fetch_result as workable_fetch_result
from careeros.collectors.workingnomads import fetch_jobs as workingnomads_fetch_jobs
from careeros.source_config import SourceConfig


class Collector(Protocol):
    """Signature shared by all job-board collector fetch functions."""

    def __call__(
        self,
        *,
        limit: int = 25,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> list[CollectedJob] | CollectorOutcome: ...


CollectorName: TypeAlias = str


# Dict preserves insertion order, so the registry order is the stable execution
# order for multi-source collection (arbeitnow, remoteok, weworkremotely,
# jobicy, remotive, workingnomads, himalayas). Orchestration derives its order
# from these keys rather than keeping a separate source of truth.
COLLECTORS: dict[CollectorName, Collector] = {
    "arbeitnow": arbeitnow_fetch_jobs,
    "remoteok": remoteok_fetch_jobs,
    "weworkremotely": weworkremotely_fetch_jobs,
    "jobicy": jobicy_fetch_jobs,
    "remotive": remotive_fetch_jobs,
    "workingnomads": workingnomads_fetch_jobs,
    "himalayas": himalayas_fetch_jobs,
}


def configured_collectors(config: SourceConfig) -> dict[CollectorName, Collector]:
    """Return a runtime collector mapping without changing the built-in registry."""
    result = dict(COLLECTORS)
    if config.lever.present and config.lever.enabled:
        result["lever"] = lambda *, limit=25, timeout=DEFAULT_TIMEOUT: (
            lever_fetch_result(config=config.lever, limit=limit, timeout=timeout)
        )
    if config.greenhouse.present and config.greenhouse.enabled:
        result["greenhouse"] = lambda *, limit=25, timeout=DEFAULT_TIMEOUT: (
            greenhouse_fetch_result(
                config=config.greenhouse, limit=limit, timeout=timeout
            )
        )
    if config.workable.present and config.workable.enabled:
        result["workable"] = lambda *, limit=25, timeout=DEFAULT_TIMEOUT: (
            workable_fetch_result(config=config.workable, limit=limit, timeout=timeout)
        )
    return result


__all__ = ["COLLECTORS", "Collector", "CollectorName", "configured_collectors"]
