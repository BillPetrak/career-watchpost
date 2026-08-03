"""Reminder workflow service.

This module owns validation, transaction semantics, and the injected-clock
seam. It is presentation-agnostic: it never writes to stdout or stderr. Raw
SQL lives in ``careeros.database.reminders``; the CLI and the reporting service
only call these functions.

Rules:

- Every write commits in a single transaction and rolls back completely on any
  ``sqlite3.Error``; no partial update, completion, or deletion is possible.
- Timestamps are parsed with :func:`careeros.reminders.models.parse_due_at`
  (explicit offsets required; naive/date-only values rejected), normalized to
  UTC, and stored as ISO-8601 UTC strings.
- Completing an already-completed reminder and reopening an already-open one
  are idempotent no-ops that preserve due time, kind, and message.
- ``update_reminder`` only changes explicitly provided fields and supports
  deliberate message clearing; a no-op update leaves ``updated_at`` untouched.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from careeros.database.jobs import get_job_by_id
from careeros.database.reminders import (
    ReminderListEntry,
    StoredReminder,
)
from careeros.database.reminders import (
    clear_completed_at as _clear_completed_at,
)
from careeros.database.reminders import (
    count_due_today as _count_due_today,
)
from careeros.database.reminders import (
    count_open as _count_open,
)
from careeros.database.reminders import (
    count_overdue as _count_overdue,
)
from careeros.database.reminders import (
    delete_reminder as _delete_reminder,
)
from careeros.database.reminders import (
    get_reminder as _get_reminder,
)
from careeros.database.reminders import (
    insert_reminder as _insert_reminder,
)
from careeros.database.reminders import (
    list_reminders as _list_reminders,
)
from careeros.database.reminders import (
    list_upcoming as _list_upcoming,
)
from careeros.database.reminders import (
    reminders_for_job as _reminders_for_job,
)
from careeros.database.reminders import (
    set_completed_at as _set_completed_at,
)
from careeros.database.reminders import (
    update_reminder as _update_reminder,
)
from careeros.reminders.models import (
    REMINDER_KINDS,
    REMINDER_STATES,
    UnknownJobError,
    UnknownReminderError,
    ValidationError,
    parse_due_at,
    validate_kind,
)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _serialize(value: datetime) -> str:
    """Normalize an aware datetime to UTC and serialize as ISO-8601.

    Naive datetimes are rejected: CareerOS never guesses a timezone.
    """
    if value.tzinfo is None:
        raise ValidationError(
            "A naive timestamp without an explicit UTC offset is not accepted "
            "(CareerOS never guesses a timezone)."
        )
    return value.astimezone(UTC).isoformat()


def _validate_message(message: str) -> str:
    """Reject empty or whitespace-only reminder messages."""
    if not message.strip():
        raise ValidationError("Reminder message must not be empty or whitespace-only.")
    return message


def _require_reminder(
    connection: sqlite3.Connection,
    reminder_id: int,
) -> StoredReminder:
    """Return a reminder by id, raising UnknownReminderError when missing."""
    reminder = _get_reminder(connection, reminder_id)
    if reminder is None:
        raise UnknownReminderError(f"No reminder found with ID {reminder_id}.")
    return reminder


def add_reminder(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    kind: str,
    due_at: str,
    message: str | None = None,
    now: datetime | None = None,
) -> StoredReminder:
    """Add a reminder for a stored job and commit it atomically.

    Raises :class:`UnknownJobError` for a missing job and
    :class:`ValidationError` for an invalid kind, message, or due timestamp.
    """
    try:
        validate_kind(kind)
    except ValueError as error:
        raise ValidationError(str(error)) from error
    if get_job_by_id(connection, job_id) is None:
        raise UnknownJobError(f"No job found with ID {job_id}.")
    normalized_message = _validate_message(message) if message is not None else None
    normalized_due = _serialize(parse_due_at(due_at))
    timestamp = _serialize(now if now is not None else _utc_now())

    try:
        reminder_id = _insert_reminder(
            connection,
            job_id=job_id,
            kind=kind,
            message=normalized_message,
            due_at=normalized_due,
            created_at=timestamp,
            updated_at=timestamp,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise

    return StoredReminder(
        id=reminder_id,
        job_id=job_id,
        kind=kind,
        message=normalized_message,
        due_at=normalized_due,
        completed_at=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


def get_reminder(
    connection: sqlite3.Connection,
    reminder_id: int,
) -> StoredReminder | None:
    """Return a reminder by id, or None when it does not exist."""
    return _get_reminder(connection, reminder_id)


def require_reminder(
    connection: sqlite3.Connection,
    reminder_id: int,
) -> StoredReminder:
    """Return a reminder by id, raising UnknownReminderError when missing."""
    return _require_reminder(connection, reminder_id)


def list_reminders(
    connection: sqlite3.Connection,
    *,
    job_id: int | None = None,
    kind: str | None = None,
    state: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
    now: datetime | None = None,
) -> list[ReminderListEntry]:
    """List reminders, soonest first, with deterministic filters.

    ``state`` must be one of :data:`REMINDER_STATES` or ``None`` (no state
    filter). ``after``/``before`` are ISO-8601 timestamps parsed with the same
    strict rules as a due time. ``now`` seeds the time-based states; it
    defaults to the current UTC time.
    """
    if kind is not None:
        try:
            validate_kind(kind)
        except ValueError as error:
            raise ValidationError(str(error)) from error
    if state is not None and state not in REMINDER_STATES:
        choices = ", ".join(REMINDER_STATES)
        raise ValidationError(
            f"Invalid reminder state {state!r}; expected one of: {choices}."
        )
    if limit < 1:
        raise ValidationError("limit must be at least 1.")

    after_value = _serialize(parse_due_at(after)) if after is not None else None
    before_value = _serialize(parse_due_at(before)) if before is not None else None
    now_value: str | None = None
    if state in ("overdue", "due", "upcoming"):
        now_value = _serialize(now if now is not None else _utc_now())

    return _list_reminders(
        connection,
        job_id=job_id,
        kind=kind,
        state=state,
        after=after_value,
        before=before_value,
        now=now_value,
        limit=limit,
    )


def reminders_for_job(
    connection: sqlite3.Connection,
    *,
    job_id: int,
) -> list[ReminderListEntry]:
    """Return a job's reminders, soonest first (read-only)."""
    return _reminders_for_job(connection, job_id)


def complete_reminder(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
    now: datetime | None = None,
) -> tuple[StoredReminder, bool]:
    """Mark an open reminder completed; idempotent for completed reminders.

    Returns ``(stored_reminder, changed)``; ``changed`` is False when the
    reminder was already completed (a no-op that preserves due time, kind,
    message, and the original completion time).
    """
    reminder = _require_reminder(connection, reminder_id)
    if reminder.completed_at is not None:
        return reminder, False

    timestamp = _serialize(now if now is not None else _utc_now())
    try:
        _set_completed_at(
            connection,
            reminder_id=reminder_id,
            completed_at=timestamp,
            updated_at=timestamp,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    updated = _get_reminder(connection, reminder_id)
    if updated is None:
        raise RuntimeError("complete_reminder did not persist the reminder")
    return updated, True


def reopen_reminder(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
    now: datetime | None = None,
) -> tuple[StoredReminder, bool]:
    """Reopen a completed reminder; idempotent for open reminders.

    Returns ``(stored_reminder, changed)``; ``changed`` is False when the
    reminder was already open (a no-op that preserves due time, kind, message,
    and completion state).
    """
    reminder = _require_reminder(connection, reminder_id)
    if reminder.completed_at is None:
        return reminder, False

    timestamp = _serialize(now if now is not None else _utc_now())
    try:
        _clear_completed_at(
            connection,
            reminder_id=reminder_id,
            updated_at=timestamp,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    updated = _get_reminder(connection, reminder_id)
    if updated is None:
        raise RuntimeError("reopen_reminder did not persist the reminder")
    return updated, True


def update_reminder(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
    due_at: str | None = None,
    kind: str | None = None,
    message: str | None = None,
    clear_message: bool = False,
    now: datetime | None = None,
) -> tuple[StoredReminder, bool]:
    """Change a reminder's due time, kind, or message.

    Omitted fields retain their prior values. ``clear_message`` deliberately
    clears the message and cannot be combined with a new ``message``. A
    no-op update (nothing actually changes) succeeds without touching
    ``updated_at`` and returns ``changed=False``.
    """
    reminder = _require_reminder(connection, reminder_id)

    if clear_message and message is not None:
        raise ValidationError("Cannot set and clear the message at the same time.")

    new_kind = kind if kind is not None else reminder.kind
    try:
        validate_kind(new_kind)
    except ValueError as error:
        raise ValidationError(str(error)) from error

    if clear_message:
        new_message: str | None = None
    elif message is not None:
        new_message = _validate_message(message)
    else:
        new_message = reminder.message

    new_due = reminder.due_at
    if due_at is not None:
        new_due = _serialize(parse_due_at(due_at))

    if (
        new_kind == reminder.kind
        and new_message == reminder.message
        and new_due == reminder.due_at
    ):
        return reminder, False

    timestamp = _serialize(now if now is not None else _utc_now())
    try:
        _update_reminder(
            connection,
            reminder_id=reminder_id,
            kind=new_kind,
            message=new_message,
            due_at=new_due,
            updated_at=timestamp,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    updated = _get_reminder(connection, reminder_id)
    if updated is None:
        raise RuntimeError("update_reminder did not persist the reminder")
    return updated, True


def delete_reminder(
    connection: sqlite3.Connection,
    *,
    reminder_id: int,
) -> StoredReminder:
    """Delete a reminder by id and return the removed row."""
    reminder = _require_reminder(connection, reminder_id)
    try:
        _delete_reminder(connection, reminder_id=reminder_id)
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    return reminder


def _read_now(now: datetime | None) -> str:
    """Serialize a reference time for the read-only count/query helpers."""
    return _serialize(now if now is not None else _utc_now())


def count_open_reminders(connection: sqlite3.Connection) -> int:
    """Return the number of open (not completed) reminders."""
    return _count_open(connection)


def count_overdue_reminders(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> int:
    """Return the number of open reminders whose due time has passed."""
    return _count_overdue(connection, _read_now(now))


def count_due_today_reminders(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> int:
    """Return the number of open reminders due within the current UTC day.

    The day boundary is the UTC calendar day containing ``now``; CareerOS does
    not know the user's local timezone, so "today" is defined and reported in
    UTC only.
    """
    reference = now if now is not None else _utc_now()
    start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return _count_due_today(connection, start=_serialize(start), end=_serialize(end))


def list_upcoming_reminders(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> list[ReminderListEntry]:
    """Return open reminders due at or after ``now``, soonest first.

    This is the report's reminder action-item query; it is strictly read-only.
    """
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    return _list_upcoming(connection, now=_read_now(now), limit=limit)


__all__: tuple[str, ...] = (
    "REMINDER_KINDS",
    "REMINDER_STATES",
    "StoredReminder",
    "UnknownJobError",
    "UnknownReminderError",
    "ValidationError",
    "add_reminder",
    "complete_reminder",
    "count_due_today_reminders",
    "count_open_reminders",
    "count_overdue_reminders",
    "delete_reminder",
    "get_reminder",
    "list_reminders",
    "list_upcoming_reminders",
    "reopen_reminder",
    "require_reminder",
    "update_reminder",
    "validate_kind",
)
