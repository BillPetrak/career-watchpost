"""Run-history persistence for collection attempts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def insert_run(
    connection: sqlite3.Connection,
    *,
    source: str,
    started_at: datetime,
) -> int:
    """Record the start of a collection attempt and return its row id."""
    cursor = connection.execute(
        """
        INSERT INTO collection_runs (source, started_at)
        VALUES (?, ?)
        """,
        (source, _serialize(started_at)),
    )
    connection.commit()

    run_id = cursor.lastrowid
    if run_id is None:
        raise RuntimeError("insert_run did not return a row id")
    return run_id


def complete_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    finished_at: datetime,
    succeeded: bool,
    fetched: int,
    inserted: int,
    updated: int,
    error: str | None,
) -> None:
    """Record the outcome of a completed collection attempt."""
    connection.execute(
        """
        UPDATE collection_runs
        SET finished_at = ?,
            succeeded = ?,
            fetched = ?,
            inserted = ?,
            updated = ?,
            error = ?
        WHERE id = ?
        """,
        (
            _serialize(finished_at),
            int(succeeded),
            fetched,
            inserted,
            updated,
            error,
            run_id,
        ),
    )
    connection.commit()


def last_completed_attempt(
    connection: sqlite3.Connection,
    source: str,
) -> datetime | None:
    """Return the start time of the most recent completed attempt, or None.

    Only rows with a non-NULL ``finished_at`` count as completed attempts.
    Malformed stored timestamps are treated as unusable (None), which callers
    interpret as "due".
    """
    row = connection.execute(
        """
        SELECT started_at
        FROM collection_runs
        WHERE source = ? AND finished_at IS NOT NULL
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()

    if row is None:
        return None
    return _parse_utc(row["started_at"])


def _serialize(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _parse_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning None when unusable."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
