"""Unified multi-source collection orchestration.

This module coordinates collecting from every registered source into a single
database. It is presentation-agnostic: it yields per-source results and never
writes to stdout or stderr. All CLI formatting lives in the CLI layer.

Every source attempt is recorded in the run-history table, including manual
collections and forced ``collect all`` runs, so scheduling stays coherent.
"""

from __future__ import annotations

import http.client
import sqlite3
import urllib.error
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeAlias

from careeros.collection_policies import SOURCE_POLICIES, SourcePolicy
from careeros.collectors import COLLECTORS, Collector, CollectorName
from careeros.collectors.models import CollectorOutcome
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.runs import (
    complete_run,
    insert_run,
    last_completed_attempt,
    latest_attempt_is_incomplete,
)
from careeros.database.schema import initialize_schema
from careeros.scheduling import due_sources


@dataclass(frozen=True)
class CollectionResult:
    """Per-source outcome of a multi-source collection run."""

    source: CollectorName
    fetched: int
    inserted: int
    updated: int
    succeeded: bool
    error: str | None = None


CollectorRegistry: TypeAlias = Mapping[CollectorName, Collector]

# Narrow, documented boundary for expected operational failures. Network errors
# (urllib.error.URLError and its HTTPError subclass) are listed explicitly even
# though both are OSError subclasses. Parsing failures surface as RuntimeError
# (the collectors' documented malformed-payload signal) or ValueError, which
# also covers json.JSONDecodeError. sqlite3.Error covers persistence failures,
# and http.client.HTTPException covers broken connections mid-response.
# Unexpected programming errors (TypeError, AttributeError, ...) intentionally
# propagate so they stay visible during development.
_EXPECTED_COLLECT_EXCEPTIONS = (
    urllib.error.HTTPError,
    urllib.error.URLError,
    OSError,
    ValueError,
    RuntimeError,
    sqlite3.Error,
    http.client.HTTPException,
)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _repair_grouping_after_interruption(
    connection: sqlite3.Connection,
    sources: Sequence[CollectorName],
) -> None:
    """Repair derived grouping when the newest attempt was interrupted."""
    if any(latest_attempt_is_incomplete(connection, source) for source in sources):
        ensure_grouped(connection)


def _run_collection(
    connection: sqlite3.Connection,
    *,
    sources: Sequence[CollectorName],
    collectors: CollectorRegistry,
    limit: int,
    clock: Callable[[], datetime],
) -> Iterator[CollectionResult]:
    """Execute one collection attempt per source and record run history.

    This is the single internal execution pipeline shared by single-source
    collection, ``collect all``, and ``collect due``. Each source attempt is
    isolated: expected operational failures (network, HTTP, parsing, sqlite)
    mark only that source as failed and are recorded; programming errors
    propagate and leave the run row incomplete (finished_at NULL).
    """
    for source in sources:
        run_id = insert_run(connection, source=source, started_at=clock())
        try:
            collected = collectors[source](limit=limit)
            outcome = (
                collected
                if isinstance(collected, CollectorOutcome)
                else CollectorOutcome(tuple(collected))
            )
            jobs = outcome.jobs
            connection.execute("BEGIN IMMEDIATE")
            saved = save_jobs(connection, jobs, commit=False)
            ensure_grouped(connection, commit=False)
            complete_run(
                connection,
                run_id=run_id,
                finished_at=clock(),
                succeeded=outcome.succeeded,
                fetched=len(jobs),
                inserted=saved.inserted,
                updated=saved.updated,
                error=outcome.error,
                commit=False,
            )
            connection.commit()
            yield CollectionResult(
                source=source,
                fetched=len(jobs),
                inserted=saved.inserted,
                updated=saved.updated,
                succeeded=outcome.succeeded,
                error=outcome.error,
            )
        except _EXPECTED_COLLECT_EXCEPTIONS as error:
            connection.rollback()
            error_text = f"{type(error).__name__}: {error}"
            complete_run(
                connection,
                run_id=run_id,
                finished_at=clock(),
                succeeded=False,
                fetched=0,
                inserted=0,
                updated=0,
                error=error_text,
            )
            yield CollectionResult(
                source=source,
                fetched=0,
                inserted=0,
                updated=0,
                succeeded=False,
                error=error_text,
            )


def collect_all(
    *,
    limit: int,
    database_path: Path,
    collectors: CollectorRegistry | None = None,
    order: Sequence[CollectorName] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[CollectionResult]:
    """Collect from every source sequentially, isolating per-source failures.

    Yields one CollectionResult per source as it finishes. Sources run in
    ``order`` (default: the ``collectors`` mapping's insertion order) and each
    collector is called at most once. A single database connection is used and
    the schema is initialized once for the whole run.
    """
    if collectors is None:
        collectors = COLLECTORS
    if order is None:
        order = tuple(collectors)
    if clock is None:
        clock = _utc_now

    with connect(database_path) as connection:
        initialize_schema(connection)
        _repair_grouping_after_interruption(connection, order)
        yield from _run_collection(
            connection,
            sources=order,
            collectors=collectors,
            limit=limit,
            clock=clock,
        )


def collect_due(
    *,
    limit: int,
    database_path: Path,
    collectors: CollectorRegistry | None = None,
    policies: Mapping[CollectorName, SourcePolicy] | None = None,
    order: Sequence[CollectorName] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[CollectionResult]:
    """Collect only sources whose polling interval has expired.

    A source with no completed run is due immediately. Scheduling is based on
    the last completed attempt (success or failure), so a failed run delays the
    next automatic attempt by exactly one interval.
    """
    if collectors is None:
        collectors = COLLECTORS
    if policies is None:
        policies = SOURCE_POLICIES
    if order is None:
        order = tuple(collectors)
    if clock is None:
        clock = _utc_now

    with connect(database_path) as connection:
        initialize_schema(connection)
        _repair_grouping_after_interruption(connection, order)
        last_attempts = {
            source: (
                None
                if latest_attempt_is_incomplete(connection, source)
                else last_completed_attempt(connection, source)
            )
            for source in order
        }
        due = due_sources(
            now=clock(),
            policies=policies,
            last_attempts=last_attempts,
            order=order,
        )
        yield from _run_collection(
            connection,
            sources=due,
            collectors=collectors,
            limit=limit,
            clock=clock,
        )


def collect_one(
    connection: sqlite3.Connection,
    *,
    source: CollectorName,
    collectors: CollectorRegistry,
    limit: int,
    clock: Callable[[], datetime] | None = None,
) -> CollectionResult:
    """Run a single-source collection attempt on an open connection."""
    if clock is None:
        clock = _utc_now
    # Grouping is a global derived view. A manual run for one source must also
    # repair a pre-hardening partial commit left by another registered source.
    _repair_grouping_after_interruption(connection, tuple(collectors))
    result = next(
        iter(
            _run_collection(
                connection,
                sources=[source],
                collectors=collectors,
                limit=limit,
                clock=clock,
            )
        )
    )
    return result
