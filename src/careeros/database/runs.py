"""Run-history persistence for collection attempts."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class CollectionRun:
    """A single collection attempt as stored in the run-history table.

    Timestamps are kept as raw stored strings so malformed values survive to
    the presentation layer without crashing; use :func:`parse_utc` to obtain
    usable datetimes. ``succeeded`` is ``None`` for incomplete (crashed) runs.
    """

    run_id: int
    source: str
    started_at: str | None
    finished_at: str | None
    succeeded: int | None
    fetched: int
    inserted: int
    updated: int
    error: str | None


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
    commit: bool = True,
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
    if commit:
        connection.commit()


def latest_attempt_is_incomplete(
    connection: sqlite3.Connection,
    source: str,
) -> bool:
    """Return whether the newest recorded attempt needs an immediate retry."""
    row = connection.execute(
        """
        SELECT finished_at
        FROM collection_runs
        WHERE source = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()
    return row is not None and row["finished_at"] is None


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
    return parse_utc(row["started_at"])


def last_completed_run(
    connection: sqlite3.Connection,
    *,
    source: str,
) -> CollectionRun | None:
    """Return the full row of the most recent completed attempt, or None.

    Only rows with a non-NULL ``finished_at`` count as completed attempts.
    """
    row = connection.execute(
        """
        SELECT id, source, started_at, finished_at, succeeded,
               fetched, inserted, updated, error
        FROM collection_runs
        WHERE source = ? AND finished_at IS NOT NULL
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (source,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_run(row)


def list_runs(
    connection: sqlite3.Connection,
    *,
    source: str | None = None,
    limit: int = 20,
    failed_only: bool = False,
) -> list[CollectionRun]:
    """Return collection attempts, newest first.

    By default both completed and incomplete attempts are returned. When
    ``failed_only`` is set, only completed failed attempts are returned.
    ``source`` optionally restricts to a single registered source. Ordering
    is deterministic: ``started_at DESC`` then ``id DESC``.
    """
    conditions: list[str] = []
    parameters: list[object] = []
    if source is not None:
        conditions.append("source = ?")
        parameters.append(source)
    if failed_only:
        conditions.append("finished_at IS NOT NULL AND succeeded = 0")

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    rows = connection.execute(
        f"""
        SELECT id, source, started_at, finished_at, succeeded,
               fetched, inserted, updated, error
        FROM collection_runs
        {where_clause}
        ORDER BY started_at DESC, id DESC
        LIMIT ?
        """,
        (*parameters, limit),
    ).fetchall()
    return [_row_to_run(row) for row in rows]


def incomplete_run_count(
    connection: sqlite3.Connection,
    *,
    source: str,
) -> int:
    """Return the number of incomplete (crashed) attempts for a source."""
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM collection_runs
        WHERE source = ? AND finished_at IS NULL
        """,
        (source,),
    ).fetchone()
    return int(row[0])


def parse_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning None when unusable."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _serialize(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _row_to_run(row: sqlite3.Row) -> CollectionRun:
    """Convert a collection_runs row to a CollectionRun."""
    return CollectionRun(
        run_id=row["id"],
        source=row["source"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        succeeded=row["succeeded"],
        fetched=row["fetched"],
        inserted=row["inserted"],
        updated=row["updated"],
        error=row["error"],
    )
