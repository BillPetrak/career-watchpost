"""Read-only collection status aggregation.

This module combines polling policies, run history, and due calculations into
per-source status snapshots. It is presentation-agnostic: it performs no I/O
beyond the queries it is given and never writes to stdout or stderr. All
formatting lives in the CLI layer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from careeros.collection_policies import SOURCE_POLICIES, SourcePolicy
from careeros.collectors import CollectorName
from careeros.database.runs import (
    CollectionRun,
    incomplete_run_count,
    last_completed_run,
    latest_attempt_is_incomplete,
    parse_utc,
)
from careeros.scheduling import due_sources
from careeros.sources import CONFIGURABLE_SOURCES, SourceCatalog, resolve_source_catalog


@dataclass(frozen=True)
class SourceStatus:
    """A snapshot of a single source's collection schedule and history.

    ``last_attempt`` is the start time of the last completed attempt (None
    when never run or when the stored timestamp is unusable). ``next_due`` is
    ``last_attempt + min_interval``. ``due`` reflects the same scheduling rule
    used by ``collect due``.
    """

    source: CollectorName
    min_interval: timedelta
    documented: bool
    last_attempt: datetime | None
    last_succeeded: bool | None
    last_fetched: int
    last_inserted: int
    last_updated: int
    last_error: str | None
    next_due: datetime | None
    due: bool
    incomplete_runs: int
    configuration: str = "enabled"
    state: str = "active"


def build_statuses(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    policies: Mapping[CollectorName, SourcePolicy] | None = None,
    order: Sequence[CollectorName] | None = None,
    catalog: SourceCatalog | None = None,
    config_path: str | None = None,
) -> list[SourceStatus]:
    """Return one status snapshot per registered source, in ``order``.

    A single fixed ``now`` drives every due and next-due calculation. Sources
    in ``policies``/``order`` mirror ``collect due``: the same ``due_sources``
    rule decides due state, so status never disagrees with scheduling.
    """
    if policies is None:
        policies = SOURCE_POLICIES
    if catalog is None:
        catalog = resolve_source_catalog(connection, config_path=config_path)
    if order is None:
        order = catalog.status

    last_runs: dict[CollectorName, CollectionRun | None] = {}
    for source in order:
        last_runs[source] = last_completed_run(connection, source=source)

    last_attempts: dict[CollectorName, datetime | None] = {
        source: (
            parse_utc(run.started_at)
            if run is not None and run.started_at is not None
            else None
        )
        for source, run in last_runs.items()
    }
    latest_incomplete = {
        source: latest_attempt_is_incomplete(connection, source) for source in order
    }
    scheduling_attempts = {
        source: None if latest_incomplete[source] else last_attempt
        for source, last_attempt in last_attempts.items()
    }

    due_set = set(
        due_sources(
            now=now,
            policies=policies,
            last_attempts=scheduling_attempts,
            order=order,
        )
    )

    statuses: list[SourceStatus] = []
    for source in order:
        policy = policies[source]
        configured = source in catalog.active
        is_configurable = source in CONFIGURABLE_SOURCES
        source_config = (
            getattr(catalog.config, source, None)
            if catalog.config is not None
            else None
        )
        configuration = (
            (
                "enabled"
                if configured
                else (
                    "disabled"
                    if source_config is not None and source_config.present
                    else "historical only"
                )
            )
            if is_configurable
            else "built-in"
        )
        state = (
            "active"
            if configured
            else (
                "disabled" if configuration == "disabled" else "inactive (historical)"
            )
        )
        run = last_runs[source]
        last_attempt = last_attempts[source]
        next_due = (
            None
            if not configured or latest_incomplete[source]
            else (
                last_attempt + policy.min_interval if last_attempt is not None else None
            )
        )
        statuses.append(
            SourceStatus(
                source=source,
                min_interval=policy.min_interval,
                documented=policy.documented,
                last_attempt=last_attempt,
                last_succeeded=None if run is None else run.succeeded == 1,
                last_fetched=run.fetched if run is not None else 0,
                last_inserted=run.inserted if run is not None else 0,
                last_updated=run.updated if run is not None else 0,
                last_error=run.error if run is not None else None,
                next_due=next_due,
                due=source in due_set if configured else False,
                incomplete_runs=incomplete_run_count(connection, source=source),
                configuration=configuration,
                state=state,
            )
        )
    return statuses
