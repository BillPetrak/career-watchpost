"""The ``reminder`` command family (local follow-up reminders)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from careeros.cli import common
from careeros.cli.common import (
    EXIT_TEMP_FAILURE,
    _lock_conflict_message,
    no_job_found_message,
)
from careeros.cli.render import _reminder_state_text
from careeros.database.connection import connect, connect_read_only
from careeros.database.jobs import get_job_by_id
from careeros.database.schema import initialize_schema
from careeros.locking import (
    CollectionLockConflict,
    NotificationLockConflict,
    collection_lock,
    notification_lock,
)
from careeros.notifications.backend import (
    BackendUnavailable,
    deliver,
    format_occurrence_line,
    resolve_backend,
)
from careeros.notifications.models import ClaimedReminder, DeliveryResult
from careeros.notifications.service import (
    attempts_for_reminder,
    claim_due,
    finalize_attempt,
    list_attempts,
    list_due_occurrences,
)
from careeros.reminders.models import (
    UnknownJobError as ReminderUnknownJobError,
)
from careeros.reminders.models import (
    UnknownReminderError as ReminderUnknownReminderError,
)
from careeros.reminders.models import (
    ValidationError as ReminderValidationError,
)
from careeros.reminders.service import (
    add_reminder,
    complete_reminder,
    delete_reminder,
    list_reminders,
    reopen_reminder,
    require_reminder,
    update_reminder,
)


def run_reminder_add(
    job_id: int,
    kind: str,
    due_at: str,
    message: str | None,
    database_path: Path,
) -> int:
    """Add a reminder for a stored job (write; acquires the lock)."""
    try:
        with collection_lock(database_path, command="reminder add"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                result = add_reminder(
                    connection,
                    job_id=job_id,
                    kind=kind,
                    due_at=due_at,
                    message=message,
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ReminderUnknownJobError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ReminderValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"Added reminder {result.id} for job {job_id} (due {result.due_at}).")
    return 0


def run_reminder_list(
    database_path: Path,
    *,
    state: str | None,
    job_id: int | None,
    kind: str | None,
    after: str | None,
    before: str | None,
    limit: int,
) -> int:
    """List reminders with optional filters (read-only, no lock)."""
    try:
        with connect_read_only(database_path) as connection:
            entries = list_reminders(
                connection,
                job_id=job_id,
                kind=kind,
                state=state,
                after=after,
                before=before,
                limit=limit,
            )
    except ReminderValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Reminders")
    print("---------")

    filters = []
    if state is not None:
        filters.append(f"state={state}")
    if job_id is not None:
        filters.append(f"job={job_id}")
    if kind is not None:
        filters.append(f"kind={kind}")
    if after is not None:
        filters.append(f"after={after}")
    if before is not None:
        filters.append(f"before={before}")
    if filters:
        print(f"Filter: {', '.join(filters)}")
    else:
        print(
            "Showing open reminders (soonest first); use --completed to list completed."
        )

    if not entries:
        print()
        print("No reminders found.")
        return 0

    for i, entry in enumerate(entries, start=1):
        reminder = entry.reminder
        print()
        print(
            f"  {i}. Reminder {reminder.id} | {reminder.kind} | job {reminder.job_id}"
        )
        print(f"     Title:   {entry.job.title}")
        print(f"     Company: {entry.job.company or 'N/A'}")
        print(f"     Due:     {reminder.due_at}")
        print(f"     Status:  {_reminder_state_text(reminder.completed_at)}")
        if reminder.message is not None:
            print(f"     Message: {reminder.message}")

    return 0


def run_reminder_show(reminder_id: int, database_path: Path) -> int:
    """Show one reminder by ID (read-only, no lock)."""
    try:
        with connect_read_only(database_path) as connection:
            reminder = require_reminder(connection, reminder_id)
            job = get_job_by_id(connection, reminder.job_id)
            if job is None:
                print(no_job_found_message(reminder.job_id), file=sys.stderr)
                return 1
            attempts = attempts_for_reminder(connection, reminder_id, limit=5)
    except ReminderUnknownReminderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"Reminder {reminder.id}")
    print("-" * len(f"Reminder {reminder.id}"))
    print(
        f"{'Job:':<9}{reminder.job_id} "
        f"({job.title}, {job.company or 'N/A'}, {job.source})"
    )
    print(f"{'Kind:':<9}{reminder.kind}")
    print(f"{'Due:':<9}{reminder.due_at}")
    print(f"{'Status:':<9}{_reminder_state_text(reminder.completed_at)}")
    if reminder.message is not None:
        print(f"{'Message:':<9}{reminder.message}")
    print(f"{'Created:':<9}{reminder.created_at}")
    print(f"{'Updated:':<9}{reminder.updated_at}")
    if attempts:
        print()
        print("Notifications:")
        for attempt in attempts:
            error_part = f", error: {attempt.error}" if attempt.error else ""
            print(
                f"  - #{attempt.id} {attempt.status}, "
                f"attempted {attempt.attempted_at}, "
                f"backend {attempt.backend}{error_part}"
            )
    return 0


def run_reminder_complete(reminder_id: int, database_path: Path) -> int:
    """Mark an open reminder completed (write; acquires the lock)."""
    try:
        with collection_lock(database_path, command="reminder complete"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                _reminder, changed = complete_reminder(
                    connection, reminder_id=reminder_id
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ReminderUnknownReminderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if changed:
        print(f"Completed reminder {reminder_id}.")
    else:
        print(f"Reminder {reminder_id} is already completed; no change.")
    return 0


def run_reminder_reopen(reminder_id: int, database_path: Path) -> int:
    """Reopen a completed reminder (write; acquires the lock)."""
    try:
        with collection_lock(database_path, command="reminder reopen"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                _reminder, changed = reopen_reminder(
                    connection, reminder_id=reminder_id
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ReminderUnknownReminderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if changed:
        print(f"Reopened reminder {reminder_id}.")
    else:
        print(f"Reminder {reminder_id} is already open; no change.")
    return 0


def run_reminder_update(
    reminder_id: int,
    database_path: Path,
    *,
    due_at: str | None,
    kind: str | None,
    message: str | None,
    clear_message: bool,
) -> int:
    """Change a reminder's due time, kind, or message (write; acquires the lock)."""
    try:
        with collection_lock(database_path, command="reminder update"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                _reminder, changed = update_reminder(
                    connection,
                    reminder_id=reminder_id,
                    due_at=due_at,
                    kind=kind,
                    message=message,
                    clear_message=clear_message,
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ReminderUnknownReminderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ReminderValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if changed:
        print(f"Updated reminder {reminder_id}.")
    else:
        print(f"No changes to reminder {reminder_id}.")
    return 0


def run_reminder_delete(reminder_id: int, database_path: Path) -> int:
    """Delete a reminder by ID (write; acquires the lock)."""
    try:
        with collection_lock(database_path, command="reminder delete"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                delete_reminder(connection, reminder_id=reminder_id)
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ReminderUnknownReminderError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"Deleted reminder {reminder_id}.")
    return 0


def _finalize_attempt_cli(
    database_path: Path,
    item: ClaimedReminder,
    result: DeliveryResult,
) -> tuple[bool, str | None]:
    """Persist one claim's outcome under the notification lock.

    Returns ``(recorded, db_error)``. ``recorded`` is False when the claim was
    already finalized/expired by another worker, or when the database write
    itself failed (the occurrence then stays eligible for a retry).
    """
    try:
        with notification_lock(database_path, command="reminder notify-due"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                recorded = finalize_attempt(
                    connection,
                    attempt_id=item.attempt_id,
                    outcome="delivered" if result.success else "failed",
                    delivered_at=common._utc_now().isoformat()
                    if result.success
                    else None,
                    error=result.error if not result.success else None,
                )
    except (OSError, sqlite3.Error) as error:
        return False, f"delivery not recorded: {type(error).__name__}: {error}"
    if recorded:
        return True, None
    return False, "delivery not recorded (claim superseded); will retry"


def run_reminder_notify_due(
    database_path: Path,
    *,
    limit: int,
    dry_run: bool,
    backend_preference: str,
) -> int:
    """Deliver local desktop notifications for due reminders (write run).

    Claims occurrences under the notification lock, dispatches them with no
    lock held, then finalizes each outcome. Successful deliveries are recorded
    and never repeated; failures are recorded and remain retryable. Never
    completes reminders and never touches application status.
    """
    try:
        backend, backend_note = resolve_backend(backend_preference)
    except BackendUnavailable as error:
        print(str(error), file=sys.stderr)
        return 1

    now = common._utc_now()

    if dry_run:
        return run_reminder_notify_dry_run(
            database_path, now=now, limit=limit, backend=backend, note=backend_note
        )

    claimed: list[ClaimedReminder] = []
    try:
        with notification_lock(database_path, command="reminder notify-due"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                claimed = claim_due(connection, now=now, limit=limit, backend=backend)
    except NotificationLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Notifying due reminders")
    print("-----------------------")
    print(f"As of: {now.isoformat()}")
    print("Mode: live")
    print(f"Backend: {backend}" + (f" ({backend_note})" if backend_note else ""))
    print(f"Due reminders: {len(claimed)}")
    print()

    delivered = 0
    failed = 0
    failures: list[tuple[ClaimedReminder, str]] = []
    for item in claimed:
        result = deliver(backend, item)
        recorded, db_error = _finalize_attempt_cli(database_path, item, result)
        if result.success and recorded:
            delivered += 1
        elif result.success:
            failed += 1
            failures.append((item, db_error or "delivery not recorded; will retry"))
        else:
            failed += 1
            failures.append((item, result.error or "delivery failed"))

    print(f"Delivered: {delivered}")
    print(f"Failed:    {failed}")
    if failures:
        print("Failed details:")
        for item, failure in failures:
            print(f"  - reminder {item.reminder_id} (job {item.job_id}): {failure}")
    if backend == "desktop":
        print()
        print(
            "Delivered means notify-send (the desktop notification service) "
            "accepted the notification;"
        )
        print("CareerOS cannot prove that a visible banner was rendered.")

    return 0 if failed == 0 else 1


def run_reminder_notify_dry_run(
    database_path: Path,
    *,
    now: datetime,
    limit: int,
    backend: str,
    note: str,
) -> int:
    """Preview which due reminders would be notified (read-only, no writes)."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            occurrences = list_due_occurrences(connection, now=now, limit=limit)
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Notifying due reminders")
    print("-----------------------")
    print(f"As of: {now.isoformat()}")
    print("Mode: dry-run")
    print(f"Backend: {backend}" + (f" ({note})" if note else ""))
    print(f"Due reminders: {len(occurrences)}")
    print()

    for occurrence in occurrences:
        print(format_occurrence_line(occurrence))

    print()
    print("Delivered: 0")
    print("Failed:    0")
    print("Dry run: no notification was sent and nothing was written.")
    return 0


def run_reminder_notify_history(
    database_path: Path,
    *,
    reminder_id: int | None,
    failed_only: bool,
    limit: int,
) -> int:
    """Show notification delivery history (read-only, no lock)."""
    try:
        with connect_read_only(database_path) as connection:
            attempts = list_attempts(
                connection,
                reminder_id=reminder_id,
                failed_only=failed_only,
                limit=limit,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Reminder failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Notification history")
    print("--------------------")

    filters = []
    if reminder_id is not None:
        filters.append(f"reminder={reminder_id}")
    if failed_only:
        filters.append("failed only")
    if filters:
        print(f"Filter: {', '.join(filters)}")

    if not attempts:
        print()
        print("No notification attempts found.")
        return 0

    for attempt in attempts:
        print()
        print(
            f"Attempt {attempt.id} | reminder {attempt.reminder_id} | {attempt.status}"
        )
        print(f"  Attempted: {attempt.attempted_at}")
        print(f"  Due:       {attempt.scheduled_due_at}")
        print(f"  Delivered: {attempt.delivered_at or '-'}")
        print(f"  Backend:   {attempt.backend}")
        if attempt.error:
            print(f"  Error:     {attempt.error}")

    return 0


def run_reminder_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch a ``reminder`` subcommand."""
    reminder_command = arguments.reminder_command

    if (
        reminder_command in ("list", "notify-due", "notify-history")
        and arguments.limit < 1
    ):
        parser.error("--limit must be at least 1")

    if reminder_command == "add" and arguments.job_id < 1:
        parser.error("job-id must be a positive integer")

    if (
        reminder_command in ("show", "update", "complete", "reopen", "delete")
        and arguments.reminder_id < 1
    ):
        parser.error("reminder-id must be a positive integer")

    if reminder_command == "add":
        return run_reminder_add(
            job_id=arguments.job_id,
            kind=arguments.kind,
            due_at=arguments.due_at,
            message=arguments.message,
            database_path=arguments.database,
        )

    if reminder_command == "show":
        return run_reminder_show(arguments.reminder_id, arguments.database)

    if reminder_command == "list":
        return run_reminder_list(
            arguments.database,
            state=arguments.state,
            job_id=arguments.job_id,
            kind=arguments.kind,
            after=arguments.after,
            before=arguments.before,
            limit=arguments.limit,
        )

    if reminder_command == "complete":
        return run_reminder_complete(arguments.reminder_id, arguments.database)

    if reminder_command == "reopen":
        return run_reminder_reopen(arguments.reminder_id, arguments.database)

    if reminder_command == "update":
        return run_reminder_update(
            arguments.reminder_id,
            arguments.database,
            due_at=arguments.due_at,
            kind=arguments.kind,
            message=arguments.message,
            clear_message=arguments.clear_message,
        )

    if reminder_command == "notify-due":
        return run_reminder_notify_due(
            arguments.database,
            limit=arguments.limit,
            dry_run=arguments.dry_run,
            backend_preference=arguments.backend,
        )

    if reminder_command == "notify-history":
        return run_reminder_notify_history(
            arguments.database,
            reminder_id=arguments.notify_reminder_id,
            failed_only=arguments.failed,
            limit=arguments.limit,
        )

    return run_reminder_delete(arguments.reminder_id, arguments.database)
