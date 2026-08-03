"""Reminder persistence layer.

Stores one or more local follow-up reminders per stored job. Reminder rows are
anchored to the stable ``jobs.id`` and are deliberately NOT linked to the
derived grouping tables, so ``group regroup`` (which deletes and rebuilds
``job_groups``/``job_group_members``) can never delete or orphan them.

Row-level functions here never commit on their own; the service layer owns the
transaction so that a reminder write is committed atomically and rolled back on
failure.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from careeros.database.jobs import (
    _SELECT_COLUMNS,
    StoredJob,
    _stored_job_from_row,
)

_JOB_COLUMNS: tuple[str, ...] = tuple(_SELECT_COLUMNS.split(", "))


@dataclass(frozen=True)
class StoredReminder:
    """One stored reminder, keyed by its own id and the tracked job."""

    id: int
    job_id: int
    kind: str
    message: str | None
    due_at: str
    completed_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReminderListEntry:
    """A stored reminder joined with its job for display."""

    reminder: StoredReminder
    job: StoredJob


def _reminder_from_row(row: sqlite3.Row) -> StoredReminder:
    """Build a StoredReminder from a reminders row."""
    return StoredReminder(
        id=row["id"],
        job_id=row["job_id"],
        kind=row["kind"],
        message=row["message"],
        due_at=row["due_at"],
        completed_at=row["completed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _list_entry_from_row(row: sqlite3.Row) -> ReminderListEntry:
    """Build a ReminderListEntry from a joined reminders/jobs row."""
    return ReminderListEntry(
        reminder=StoredReminder(
            id=row["reminder_id"],
            job_id=row["job_id"],
            kind=row["kind"],
            message=row["message"],
            due_at=row["due_at"],
            completed_at=row["completed_at"],
            created_at=row["reminder_created"],
            updated_at=row["reminder_updated"],
        ),
        job=_stored_job_from_row(row),
    )


def insert_reminder(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    kind: str,
    message: str | None,
    due_at: str,
    created_at: str,
    updated_at: str,
) -> int:
    """Create a reminder row and return its id."""
    cursor = connection.execute(
        """
        INSERT INTO reminders (
            job_id, kind, message, due_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, kind, message, due_at, created_at, updated_at),
    )
    reminder_id = cursor.lastrowid
    if reminder_id is None:
        raise RuntimeError("insert_reminder did not return a row id")
    return int(reminder_id)


def get_reminder(
    connection: sqlite3.Connection,
    reminder_id: int,
) -> StoredReminder | None:
    """Return a reminder by id, or None when it does not exist."""
    row = connection.execute(
        """
        SELECT id, job_id, kind, message, due_at, completed_at, created_at,
               updated_at
        FROM reminders
        WHERE id = ?
        """,
        (reminder_id,),
    ).fetchone()
    if row is None:
        return None
    return _reminder_from_row(row)


def update_reminder(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
    kind: str,
    message: str | None,
    due_at: str,
    updated_at: str,
) -> None:
    """Replace a reminder's mutable fields and update time."""
    connection.execute(
        """
        UPDATE reminders
        SET kind = ?, message = ?, due_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (kind, message, due_at, updated_at, reminder_id),
    )


def set_completed_at(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
    completed_at: str,
    updated_at: str,
) -> None:
    """Mark an open reminder completed; updating its update time too."""
    connection.execute(
        """
        UPDATE reminders
        SET completed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (completed_at, updated_at, reminder_id),
    )


def clear_completed_at(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
    updated_at: str,
) -> None:
    """Reopen a completed reminder by clearing completed_at."""
    connection.execute(
        """
        UPDATE reminders
        SET completed_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        (updated_at, reminder_id),
    )


def delete_reminder(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
) -> None:
    """Delete a reminder by id."""
    connection.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))


def list_reminders(
    connection: sqlite3.Connection,
    *,
    job_id: int | None = None,
    kind: str | None = None,
    state: str | None = None,
    after: str | None = None,
    before: str | None = None,
    now: str | None = None,
    limit: int = 20,
) -> list[ReminderListEntry]:
    """Return reminders joined with their jobs, in a deterministic order.

    Ordering is always ``due_at ASC`` then ``id ASC`` (soonest first, with the
    reminder id as a stable tie-break). The ``state`` filter narrows to open,
    overdue, due, upcoming, or completed reminders; ``now`` is the serialized
    UTC reference time used by the time-based states.
    """
    conditions: list[str] = []
    parameters: list[object] = []
    if job_id is not None:
        conditions.append("r.job_id = ?")
        parameters.append(job_id)
    if kind is not None:
        conditions.append("r.kind = ?")
        parameters.append(kind)
    if state is not None:
        if state == "open":
            conditions.append("r.completed_at IS NULL")
        elif state == "completed":
            conditions.append("r.completed_at IS NOT NULL")
        elif state == "overdue":
            conditions.append("r.completed_at IS NULL AND r.due_at < ?")
            parameters.append(now)
        elif state == "due":
            conditions.append("r.completed_at IS NULL AND r.due_at <= ?")
            parameters.append(now)
        elif state == "upcoming":
            conditions.append("r.completed_at IS NULL AND r.due_at > ?")
            parameters.append(now)
    if after is not None:
        conditions.append("r.due_at >= ?")
        parameters.append(after)
    if before is not None:
        conditions.append("r.due_at <= ?")
        parameters.append(before)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    rows = connection.execute(
        f"""
        SELECT r.id AS reminder_id,
               r.job_id AS job_id,
               r.kind AS kind,
               r.message AS message,
               r.due_at AS due_at,
               r.completed_at AS completed_at,
               r.created_at AS reminder_created,
               r.updated_at AS reminder_updated,
               {", ".join(f"j.{column}" for column in _JOB_COLUMNS)}
        FROM reminders r
        JOIN jobs j ON j.id = r.job_id
        {where_clause}
        ORDER BY r.due_at ASC, r.id ASC
        LIMIT ?
        """,
        (*parameters, limit),
    ).fetchall()
    return [_list_entry_from_row(row) for row in rows]


def reminders_for_job(
    connection: sqlite3.Connection,
    job_id: int,
) -> list[ReminderListEntry]:
    """Return a job's reminders, soonest first."""
    return list_reminders(connection, job_id=job_id)


def list_upcoming(
    connection: sqlite3.Connection,
    *,
    now: str,
    limit: int,
) -> list[ReminderListEntry]:
    """Return open reminders due at or after ``now``, soonest first.

    This is the read-only query the report uses for its reminder action items.
    """
    return list_reminders(connection, state="upcoming", now=now, limit=limit)


def count_open(connection: sqlite3.Connection) -> int:
    """Return the number of open (not completed) reminders."""
    row = connection.execute(
        "SELECT COUNT(*) FROM reminders WHERE completed_at IS NULL"
    ).fetchone()
    return int(row[0])


def count_overdue(connection: sqlite3.Connection, now: str) -> int:
    """Return the number of open reminders whose due time has passed."""
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM reminders
        WHERE completed_at IS NULL AND due_at < ?
        """,
        (now,),
    ).fetchone()
    return int(row[0])


def count_due_today(
    connection: sqlite3.Connection,
    *,
    start: str,
    end: str,
) -> int:
    """Return the number of open reminders due within a UTC day range."""
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM reminders
        WHERE completed_at IS NULL AND due_at >= ? AND due_at < ?
        """,
        (start, end),
    ).fetchone()
    return int(row[0])
