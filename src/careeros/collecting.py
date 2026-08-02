"""Unified multi-source collection orchestration.

This module coordinates collecting from every registered source into a single
database. It is presentation-agnostic: it yields per-source results and never
writes to stdout or stderr. All CLI formatting lives in the CLI layer.
"""

from __future__ import annotations

import http.client
import sqlite3
import urllib.error
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from careeros.collectors import COLLECTORS, Collector, CollectorName
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema


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


def collect_all(
    *,
    limit: int,
    database_path: Path,
    collectors: CollectorRegistry | None = None,
    order: Sequence[CollectorName] | None = None,
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

    with connect(database_path) as connection:
        initialize_schema(connection)

        for source in order:
            try:
                jobs = collectors[source](limit=limit)
                saved = save_jobs(connection, jobs)
                yield CollectionResult(
                    source=source,
                    fetched=len(jobs),
                    inserted=saved.inserted,
                    updated=saved.updated,
                    succeeded=True,
                )
            except _EXPECTED_COLLECT_EXCEPTIONS as error:
                connection.rollback()
                yield CollectionResult(
                    source=source,
                    fetched=0,
                    inserted=0,
                    updated=0,
                    succeeded=False,
                    error=f"{type(error).__name__}: {error}",
                )
