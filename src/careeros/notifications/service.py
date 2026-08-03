"""Notification-delivery workflow service.

Owns the claim/finalize protocol, the injected-clock seam, and the read-only
due-reminder query. It is presentation-agnostic: it never prints. Raw SQL lives
in ``careeros.database.notifications``; backends live in
``careeros.notifications.backend``.

Concurrency design (claim/finalize):

- :func:`claim_due` runs inside a single ``BEGIN IMMEDIATE`` transaction so two
  workers cannot both claim the same occurrence: it first expires stale
  in-flight rows, then selects occurrences with no delivered row and no fresh
  in-flight row, then inserts one ``attempted`` row per candidate and commits.
- :func:`finalize_attempt`` is guarded by ``WHERE status = 'attempted'`` so a
  superseded or already-expired claim is a no-op rather than a double finalize.
- The caller (CLI) holds :func:`careeros.locking.notification_lock` only around
  each short claim/finalize transaction, never while the external notification
  subprocess runs.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from careeros.database.notifications import (
    StoredAttempt,
)
from careeros.database.notifications import (
    attempts_for_reminder as _attempts_for_reminder,
)
from careeros.database.notifications import (
    count_pending_due as _count_pending_due,
)
from careeros.database.notifications import (
    expire_stale_claims as _expire_stale_claims,
)
from careeros.database.notifications import (
    finalize_attempt as _finalize_attempt,
)
from careeros.database.notifications import (
    insert_attempt as _insert_attempt,
)
from careeros.database.notifications import (
    list_attempts as _list_attempts,
)
from careeros.database.notifications import (
    notification_summary as _notification_summary,
)
from careeros.database.notifications import (
    select_due_occurrences as _select_due_occurrences,
)
from careeros.notifications.models import (
    ClaimedReminder,
    DueOccurrence,
    NotificationSummary,
    ValidationError,
)

# A claim that is not finalized within this window is treated as abandoned and
# its occurrence becomes eligible again. Far larger than the bounded notify-send
# timeout, so a normal run can never be reclaimed mid-dispatch.
CLAIM_TIMEOUT = timedelta(minutes=5)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _serialize(value: datetime) -> str:
    """Normalize an aware datetime to UTC and serialize as ISO-8601."""
    if value.tzinfo is None:
        raise ValidationError(
            "Notification now must be timezone-aware (CareerOS never guesses "
            "a timezone)."
        )
    return value.astimezone(UTC).isoformat()


def _occurrence_from_row(row: sqlite3.Row) -> DueOccurrence:
    return DueOccurrence(
        reminder_id=row["reminder_id"],
        due_at=row["due_at"],
        kind=row["kind"],
        message=row["message"],
        job_id=row["job_id"],
        job_title=row["job_title"],
        job_company=row["job_company"],
        job_source=row["job_source"],
    )


def list_due_occurrences(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
    claim_timeout: timedelta = CLAIM_TIMEOUT,
) -> list[DueOccurrence]:
    """Return open, due reminder occurrences still awaiting notification.

    Read-only and lock-free. Excludes occurrences already delivered
    successfully, and occurrences currently in flight (whose claim is fresher
    than ``claim_timeout``). Ordering is deterministic: ``due_at ASC`` then
    ``reminder id ASC``. This is the exact candidate set that
    :meth:`claim_due` would claim, so ``--dry-run`` can preview truthfully.
    """
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    now_str = _serialize(now)
    cutoff = _serialize(now - claim_timeout)
    rows = _select_due_occurrences(
        connection, now=now_str, stale_cutoff=cutoff, limit=limit
    )
    return [_occurrence_from_row(row) for row in rows]


def claim_due(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
    backend: str,
    claim_timeout: timedelta = CLAIM_TIMEOUT,
) -> list[ClaimedReminder]:
    """Claim up to ``limit`` due occurrences for delivery this run.

    Runs one ``BEGIN IMMEDIATE`` transaction: expire stale in-flight attempts,
    select eligible occurrences, and insert an ``attempted`` row for each.
    Commits atomically, rolling back completely on any ``sqlite3.Error``.
    """
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    now_str = _serialize(now)
    cutoff = _serialize(now - claim_timeout)

    claimed: list[ClaimedReminder] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        _expire_stale_claims(connection, stale_cutoff=cutoff)
        rows = _select_due_occurrences(
            connection, now=now_str, stale_cutoff=cutoff, limit=limit
        )
        for row in rows:
            occurrence = _occurrence_from_row(row)
            attempt_id = _insert_attempt(
                connection,
                reminder_id=occurrence.reminder_id,
                scheduled_due_at=occurrence.due_at,
                attempted_at=now_str,
                backend=backend,
            )
            claimed.append(
                ClaimedReminder(
                    attempt_id=attempt_id,
                    reminder_id=occurrence.reminder_id,
                    due_at=occurrence.due_at,
                    kind=occurrence.kind,
                    message=occurrence.message,
                    job_id=occurrence.job_id,
                    job_title=occurrence.job_title,
                    job_company=occurrence.job_company,
                    job_source=occurrence.job_source,
                )
            )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    return claimed


def finalize_attempt(
    connection: sqlite3.Connection,
    *,
    attempt_id: int,
    outcome: str,
    delivered_at: str | None,
    error: str | None,
) -> bool:
    """Record a claim's terminal outcome, committing in one transaction.

    ``delivered_at`` must be the serialized delivery time for a successful
    outcome and ``None`` otherwise. Returns True when this call actually
    updated the row; False when the claim was already finalized or expired by
    another worker (in which case the occurrence is left eligible for a retry).
    """
    try:
        changed = _finalize_attempt(
            connection,
            attempt_id=attempt_id,
            status=outcome,
            delivered_at=delivered_at,
            error=error,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    return changed


def list_attempts(
    connection: sqlite3.Connection,
    *,
    reminder_id: int | None = None,
    failed_only: bool = False,
    limit: int = 20,
) -> list[StoredAttempt]:
    """Return notification attempts, newest first (read-only)."""
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    return _list_attempts(
        connection,
        reminder_id=reminder_id,
        failed_only=failed_only,
        limit=limit,
    )


def attempts_for_reminder(
    connection: sqlite3.Connection,
    reminder_id: int,
    limit: int = 5,
) -> list[StoredAttempt]:
    """Return a reminder's most recent attempts (read-only)."""
    return _attempts_for_reminder(connection, reminder_id, limit)


def notification_summary(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    claim_timeout: timedelta = CLAIM_TIMEOUT,
) -> NotificationSummary:
    """Return delivery totals for the read-only report."""
    now_str = _serialize(now)
    cutoff = _serialize(now - claim_timeout)
    pending, delivered, failed, last_delivered, last_failed = _notification_summary(
        connection, now=now_str, stale_cutoff=cutoff
    )
    return NotificationSummary(
        pending_due=pending,
        delivered_total=delivered,
        failed_total=failed,
        last_delivered_at=last_delivered,
        last_failed_at=last_failed,
    )


def count_pending_due(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    claim_timeout: timedelta = CLAIM_TIMEOUT,
) -> int:
    """Return the number of due reminders still awaiting a notification."""
    return _count_pending_due(
        connection,
        now=_serialize(now),
        stale_cutoff=_serialize(now - claim_timeout),
    )


__all__: tuple[str, ...] = (
    "CLAIM_TIMEOUT",
    "StoredAttempt",
    "attempts_for_reminder",
    "claim_due",
    "count_pending_due",
    "finalize_attempt",
    "list_attempts",
    "list_due_occurrences",
    "notification_summary",
)
