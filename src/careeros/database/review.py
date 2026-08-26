"""Review-decision persistence layer.

Stores one current decision per reviewed job plus an append-only history of
every decision transition (including clears, which are recorded as a
``new_decision`` of ``None`` after the current row is removed). Review records
are anchored to the stable ``jobs.id`` and are deliberately NOT linked to the
derived grouping tables, so ``group regroup`` (which deletes and rebuilds
``job_groups``/``job_group_members``) can never delete or orphan them.

Row-level functions here never commit on their own; callers own the
transaction so that a decision-row change and its history event are committed
together atomically.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from careeros.database.jobs import (
    JOB_COLUMNS,
    StoredJob,
    job_from_row,
)

_JOB_COLUMNS: tuple[str, ...] = tuple(JOB_COLUMNS.split(", "))


@dataclass(frozen=True)
class StoredDecision:
    """One current review decision, keyed by the reviewed job."""

    id: int
    job_id: int
    decision: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewEvent:
    """A single append-only decision-transition history entry."""

    id: int
    job_id: int
    old_decision: str | None
    new_decision: str | None
    note: str | None
    created_at: str


@dataclass(frozen=True)
class DecisionListItem:
    """A current decision joined with its stored job for display."""

    decision: StoredDecision
    job: StoredJob
    latest_note: str | None


@dataclass(frozen=True)
class SetDecisionResult:
    """Outcome of a set-decision operation."""

    decision_id: int
    created: bool
    changed: bool


@dataclass(frozen=True)
class ClearDecisionResult:
    """Outcome of a clear-decision operation."""

    changed: bool


def _decision_from_row(row: sqlite3.Row) -> StoredDecision:
    """Build a StoredDecision from a SQLite row."""
    return StoredDecision(
        id=row["id"],
        job_id=row["job_id"],
        decision=row["decision"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row) -> ReviewEvent:
    """Build a ReviewEvent from a SQLite row."""
    return ReviewEvent(
        id=row["id"],
        job_id=row["job_id"],
        old_decision=row["old_decision"],
        new_decision=row["new_decision"],
        note=row["note"],
        created_at=row["created_at"],
    )


def _list_item_from_row(row: sqlite3.Row) -> DecisionListItem:
    """Build a DecisionListItem from a joined decisions/jobs row."""
    return DecisionListItem(
        decision=StoredDecision(
            id=row["decision_id"],
            job_id=row["job_id"],
            decision=row["decision"],
            created_at=row["decision_created_at"],
            updated_at=row["decision_updated_at"],
        ),
        job=job_from_row(row),
        latest_note=row["latest_note"],
    )


def get_decision(
    connection: sqlite3.Connection,
    job_id: int,
) -> StoredDecision | None:
    """Return the current decision for a job, or None when it is unreviewed."""
    row = connection.execute(
        """
        SELECT id, job_id, decision, created_at, updated_at
        FROM review_decisions
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return _decision_from_row(row)


def insert_decision(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    decision: str,
    created_at: str,
    updated_at: str,
) -> int:
    """Create a decision row for a job and return its id."""
    cursor = connection.execute(
        """
        INSERT INTO review_decisions (job_id, decision, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (job_id, decision, created_at, updated_at),
    )
    decision_id = cursor.lastrowid
    if decision_id is None:
        raise RuntimeError("insert_decision did not return a row id")
    return int(decision_id)


def update_decision(
    connection: sqlite3.Connection,
    *,
    decision_id: int,
    decision: str,
    updated_at: str,
) -> None:
    """Update a stored decision's value and update time."""
    connection.execute(
        """
        UPDATE review_decisions
        SET decision = ?, updated_at = ?
        WHERE id = ?
        """,
        (decision, updated_at, decision_id),
    )


def delete_decision(
    connection: sqlite3.Connection,
    *,
    job_id: int,
) -> None:
    """Remove a job's current decision row (its history events survive)."""
    connection.execute(
        "DELETE FROM review_decisions WHERE job_id = ?",
        (job_id,),
    )


def append_event(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    old_decision: str | None,
    new_decision: str | None,
    note: str | None,
    created_at: str,
) -> int:
    """Append an immutable history event and return its id."""
    cursor = connection.execute(
        """
        INSERT INTO review_events (
            job_id, old_decision, new_decision, note, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (job_id, old_decision, new_decision, note, created_at),
    )
    event_id = cursor.lastrowid
    if event_id is None:
        raise RuntimeError("append_event did not return a row id")
    return int(event_id)


def latest_event_note(
    connection: sqlite3.Connection,
    *,
    job_id: int,
) -> str | None:
    """Return the most recent non-empty note, or None.

    ``latest`` here means the newest event that actually carries a note, so a
    later note-less transition never hides an earlier note.
    """
    row = connection.execute(
        """
        SELECT note
        FROM review_events
        WHERE job_id = ? AND note IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    value = row["note"]
    return str(value) if value is not None else None


def list_decisions(
    connection: sqlite3.Connection,
    *,
    job_id: int | None = None,
    decisions: Sequence[str] | None = None,
    limit: int = 20,
) -> list[DecisionListItem]:
    """Return current decisions joined with their jobs.

    Ordering is deterministic: ``updated_at DESC`` then ``id DESC``. An
    optional ``decisions`` filter restricts results to those decision values.
    An optional ``job_id`` filter restricts results to one job's current
    decision (at most one per job, enforced by the ``UNIQUE`` job_id
    constraint; unreviewed and unknown jobs both return an empty list).
    """
    conditions: list[str] = []
    parameters: list[object] = []
    if job_id is not None:
        conditions.append("d.job_id = ?")
        parameters.append(job_id)
    if decisions:
        placeholders = ", ".join("?" * len(decisions))
        conditions.append(f"d.decision IN ({placeholders})")
        parameters.extend(decisions)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    rows = connection.execute(
        f"""
        SELECT d.id AS decision_id,
               d.job_id AS job_id,
               d.decision AS decision,
               d.created_at AS decision_created_at,
               d.updated_at AS decision_updated_at,
               {", ".join(f"j.{column}" for column in _JOB_COLUMNS)},
               (SELECT e.note
                FROM review_events e
                WHERE e.job_id = d.job_id AND e.note IS NOT NULL
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT 1) AS latest_note
        FROM review_decisions d
        JOIN jobs j ON j.id = d.job_id
        {where_clause}
        ORDER BY d.updated_at DESC, d.id DESC
        LIMIT ?
        """,
        (*parameters, limit),
    ).fetchall()
    return [_list_item_from_row(row) for row in rows]


def list_decision_events(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    limit: int = 20,
) -> list[ReviewEvent]:
    """Return a job's decision history events, newest first.

    Ordering is deterministic: ``created_at DESC`` then ``id DESC``.
    """
    rows = connection.execute(
        """
        SELECT id, job_id, old_decision, new_decision, note, created_at
        FROM review_events
        WHERE job_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (job_id, limit),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def decisions_for_jobs(
    connection: sqlite3.Connection,
    job_ids: Iterable[int],
) -> dict[int, str]:
    """Return a map of job id -> decision for the reviewed subset.

    Unreviewed jobs are absent from the returned mapping.
    """
    ids = tuple(dict.fromkeys(int(job_id) for job_id in job_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" * len(ids))
    rows = connection.execute(
        f"""
        SELECT job_id, decision
        FROM review_decisions
        WHERE job_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["job_id"]): row["decision"] for row in rows}


def decision_records_for_jobs(
    connection: sqlite3.Connection,
    job_ids: Iterable[int],
) -> dict[int, StoredDecision]:
    """Return complete current decision records for a batch of jobs."""
    ids = tuple(dict.fromkeys(int(job_id) for job_id in job_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" * len(ids))
    rows = connection.execute(
        f"""
        SELECT id, job_id, decision, created_at, updated_at
        FROM review_decisions
        WHERE job_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["job_id"]): _decision_from_row(row) for row in rows}
