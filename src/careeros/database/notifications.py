"""Notification-delivery persistence layer (schema v7).

Stores an append-only, durable audit trail of reminder-notification attempts in
``reminder_notifications``. Each row is one attempt for one reminder
*occurrence*, where an occurrence is the pair ``(reminder_id, due_at)``. A
delivered occurrence is unique (partial unique index), while failed/attempted
attempts may repeat so failures are retried on a later run. Rows are anchored
to ``reminders.id`` with ``ON DELETE RESTRICT`` so the audit trail is never
cascade-deleted.

Row-level functions here never commit on their own; the notifications service
layer owns the transaction (including the deliberate ``BEGIN IMMEDIATE`` used
to serialize the claim step).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredAttempt:
    """One durable notification-attempt audit row."""

    id: int
    reminder_id: int
    scheduled_due_at: str
    attempted_at: str
    delivered_at: str | None
    status: str
    error: str | None
    backend: str


def _attempt_from_row(row: sqlite3.Row) -> StoredAttempt:
    return StoredAttempt(
        id=row["id"],
        reminder_id=row["reminder_id"],
        scheduled_due_at=row["scheduled_due_at"],
        attempted_at=row["attempted_at"],
        delivered_at=row["delivered_at"],
        status=row["status"],
        error=row["error"],
        backend=row["backend"],
    )


def select_due_occurrences(
    connection: sqlite3.Connection,
    *,
    now: str,
    stale_cutoff: str,
    limit: int,
) -> list[sqlite3.Row]:
    """Return open reminders due at/after ``now`` that need a notification.

    Excludes occurrences that already have a delivered attempt and occurrences
    with a fresh (non-stale) in-flight attempt. Stale in-flight rows (older than
    ``stale_cutoff``) do not block a reclaim. Ordering is deterministic:
    ``due_at ASC`` then ``id ASC``.
    """
    return connection.execute(
        """
        SELECT r.id AS reminder_id,
               r.due_at AS due_at,
               r.kind AS kind,
               r.message AS message,
               j.id AS job_id,
               j.title AS job_title,
               j.company AS job_company,
               j.source AS job_source
        FROM reminders r
        JOIN jobs j ON j.id = r.job_id
        WHERE r.completed_at IS NULL
          AND r.due_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM reminder_notifications n
              WHERE n.reminder_id = r.id
                AND n.scheduled_due_at = r.due_at
                AND n.status = 'delivered'
          )
          AND NOT EXISTS (
              SELECT 1 FROM reminder_notifications n
              WHERE n.reminder_id = r.id
                AND n.scheduled_due_at = r.due_at
                AND n.status = 'attempted'
                AND n.attempted_at >= ?
          )
        ORDER BY r.due_at ASC, r.id ASC
        LIMIT ?
        """,
        (now, stale_cutoff, limit),
    ).fetchall()


def expire_stale_claims(
    connection: sqlite3.Connection,
    *,
    stale_cutoff: str,
) -> int:
    """Finalize in-flight attempts that a worker never completed.

    A worker that crashed between claiming and finalizing leaves an
    ``attempted`` row; marking it failed lets the occurrence be reclaimed by a
    later run. Returns the number of rows expired.
    """
    cursor = connection.execute(
        """
        UPDATE reminder_notifications
        SET status = 'failed', error = 'claim-expired'
        WHERE status = 'attempted' AND attempted_at < ?
        """,
        (stale_cutoff,),
    )
    return cursor.rowcount


def insert_attempt(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
    scheduled_due_at: str,
    attempted_at: str,
    backend: str,
) -> int:
    """Create an in-flight attempt row and return its id."""
    cursor = connection.execute(
        """
        INSERT INTO reminder_notifications (
            reminder_id, scheduled_due_at, attempted_at, status, backend
        )
        VALUES (?, ?, ?, 'attempted', ?)
        """,
        (reminder_id, scheduled_due_at, attempted_at, backend),
    )
    attempt_id = cursor.lastrowid
    if attempt_id is None:
        raise RuntimeError("insert_attempt did not return a row id")
    return int(attempt_id)


def finalize_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: int,
    status: str,
    delivered_at: str | None,
    error: str | None,
) -> bool:
    """Move an in-flight attempt to a terminal status.

    The ``WHERE status = 'attempted'`` guard makes this idempotent: updating a
    row that was already expired or finalized by another worker is a no-op and
    returns False (the caller then leaves the occurrence eligible for a retry).
    """
    cursor = connection.execute(
        """
        UPDATE reminder_notifications
        SET status = ?, delivered_at = ?, error = ?
        WHERE id = ? AND status = 'attempted'
        """,
        (status, delivered_at, error, attempt_id),
    )
    return cursor.rowcount == 1


def list_attempts(
    connection: sqlite3.Connection,
    *,
    reminder_id: int | None,
    failed_only: bool,
    limit: int,
) -> list[StoredAttempt]:
    """Return notification attempts, newest first, with optional filters."""
    conditions: list[str] = []
    parameters: list[object] = []
    if reminder_id is not None:
        conditions.append("reminder_id = ?")
        parameters.append(reminder_id)
    if failed_only:
        conditions.append("status = 'failed'")
    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)
    rows = connection.execute(
        f"""
        SELECT id, reminder_id, scheduled_due_at, attempted_at, delivered_at,
               status, error, backend
        FROM reminder_notifications
        {where_clause}
        ORDER BY id DESC
        LIMIT ?
        """,
        (*parameters, limit),
    ).fetchall()
    return [_attempt_from_row(row) for row in rows]


def attempts_for_reminder(
    connection: sqlite3.Connection,
    reminder_id: int,
    limit: int,
) -> list[StoredAttempt]:
    """Return a reminder's most recent notification attempts (read-only)."""
    return list_attempts(
        connection, reminder_id=reminder_id, failed_only=False, limit=limit
    )


def count_pending_due(
    connection: sqlite3.Connection,
    *,
    now: str,
    stale_cutoff: str,
) -> int:
    """Count open, due reminders that still need a notification."""
    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM reminders r
        WHERE r.completed_at IS NULL
          AND r.due_at <= ?
          AND NOT EXISTS (
              SELECT 1 FROM reminder_notifications n
              WHERE n.reminder_id = r.id
                AND n.scheduled_due_at = r.due_at
                AND n.status = 'delivered'
          )
          AND NOT EXISTS (
              SELECT 1 FROM reminder_notifications n
              WHERE n.reminder_id = r.id
                AND n.scheduled_due_at = r.due_at
                AND n.status = 'attempted'
                AND n.attempted_at >= ?
          )
        """,
        (now, stale_cutoff),
    ).fetchone()
    return int(row[0])


def notification_summary(
    connection: sqlite3.Connection,
    *,
    now: str,
    stale_cutoff: str,
) -> tuple[int, int, int, str | None, str | None]:
    """Return delivery totals for reporting (``(pending, delivered, failed,
    last_delivered, last_failed)``)."""
    pending = count_pending_due(connection, now=now, stale_cutoff=stale_cutoff)
    delivered = connection.execute(
        "SELECT COUNT(*) FROM reminder_notifications WHERE status = 'delivered'"
    ).fetchone()
    failed = connection.execute(
        "SELECT COUNT(*) FROM reminder_notifications WHERE status = 'failed'"
    ).fetchone()
    last_delivered = connection.execute(
        "SELECT MAX(delivered_at) FROM reminder_notifications"
        " WHERE status = 'delivered'"
    ).fetchone()
    last_failed = connection.execute(
        "SELECT MAX(attempted_at) FROM reminder_notifications WHERE status = 'failed'"
    ).fetchone()
    return (
        pending,
        int(delivered[0]),
        int(failed[0]),
        last_delivered[0],
        last_failed[0],
    )
