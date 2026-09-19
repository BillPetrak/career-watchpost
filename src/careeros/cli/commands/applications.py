"""The ``application`` command family (application workflow state)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from careeros.applications.service import (
    UnknownJobError,
    get_application,
    latest_event_note,
    list_application_events,
    list_applications,
    set_application_status,
)
from careeros.cli.common import (
    EXIT_TEMP_FAILURE,
    _lock_conflict_message,
    no_job_found_message,
)
from careeros.cli.render import _format_application_status, _reminder_state_text
from careeros.database.connection import connect, connect_read_only
from careeros.database.jobs import get_job_by_id
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.presentation import enum_label
from careeros.reminders.service import reminders_for_job


def run_application_redact_note(event_id: int, database_path: Path) -> int:
    """Explicitly erase one private event note while preserving event facts."""
    try:
        with collection_lock(database_path, command="application redact-note"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT note FROM application_events WHERE id=?", (event_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    print("Event not found.", file=sys.stderr)
                    return 1
                if row[0] is not None:
                    connection.execute(
                        "UPDATE application_events SET note=NULL,"
                        "note_redacted_at=COALESCE(note_redacted_at,?) WHERE id=?",
                        (datetime.now(UTC).isoformat(), event_id),
                    )
                connection.commit()
        print(f"Redacted note for event {event_id}.")
        return 0
    except CollectionLockConflict:
        print("Database is busy; retry later.", file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except (OSError, sqlite3.Error) as error:
        print(f"Application failed: {type(error).__name__}", file=sys.stderr)
        return 1


def run_application_set(
    job_id: int,
    status: str,
    note: str | None,
    database_path: Path,
) -> int:
    """Set or update a job's application status (write; acquires the lock)."""
    if note is not None and len(note) > 1000:
        print("Application note exceeds 1000 characters.", file=sys.stderr)
        return 2
    try:
        with collection_lock(database_path, command="application set"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                result = set_application_status(
                    connection,
                    job_id=job_id,
                    status=status,
                    note=note,
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except UnknownJobError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"Application failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    label = enum_label(status)
    if result.created:
        print(f"Tracked job {job_id} as {label}.")
    elif result.changed:
        print(f"Updated job {job_id} to {label}.")
    else:
        print(f"Job {job_id} is already {label}; no change.")
    return 0


def run_application_show(job_id: int, database_path: Path) -> int:
    """Show the current application state for one job (read-only)."""
    try:
        with connect_read_only(database_path) as connection:
            job = get_job_by_id(connection, job_id)
            if job is None:
                print(no_job_found_message(job_id), file=sys.stderr)
                return 1
            application = get_application(connection, job_id=job_id)
            note = None
            if application is not None:
                note = latest_event_note(connection, application_id=application.id)
            reminders = reminders_for_job(connection, job_id=job_id)
    except (OSError, sqlite3.Error) as error:
        print(f"Application failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if application is None:
        print(f"Job {job_id} is not currently tracked.")
        return 0

    print(f"Application for job {job_id} ({job.title})")
    print("-" * len(f"Application for job {job_id} ({job.title})"))
    print(f"{'Job ID:':<16}{job.id}")
    print(f"{'Title:':<16}{job.title}")
    print(f"{'Company:':<16}{job.company or 'N/A'}")
    print(f"{'Source:':<16}{job.source}")
    print(f"{'URL:':<16}{job.url or 'N/A'}")
    print(f"{'Status:':<16}{_format_application_status(application.current_status)}")
    print(f"{'First tracked:':<16}{application.created_at}")
    print(f"{'Last updated:':<16}{application.updated_at}")
    if note is not None:
        print(f"{'Note:':<16}{note}")
    if reminders:
        print()
        print("Reminders:")
        for entry in reminders:
            reminder = entry.reminder
            suffix = ""
            if reminder.message is not None:
                suffix = f"; {reminder.message}"
            print(
                f"  - [{reminder.id}] {enum_label(reminder.kind)}, "
                f"due {reminder.due_at}, {_reminder_state_text(reminder.completed_at)}"
                f"{suffix}"
            )
    return 0


def run_application_list(
    database_path: Path,
    *,
    statuses: Sequence[str] | None = None,
    limit: int = 20,
) -> int:
    """List tracked applications, optionally filtered by status (read-only)."""
    try:
        with connect_read_only(database_path) as connection:
            items = list_applications(connection, statuses=statuses, limit=limit)
    except (OSError, sqlite3.Error) as error:
        print(f"Application failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Tracked applications")
    print("--------------------")

    if statuses:
        print(f"Filter: status={', '.join(enum_label(item) for item in statuses)}")

    if not items:
        print()
        print("No applications tracked.")
        return 0

    for item in items:
        application = item.application
        job = item.job
        print()
        print(f"{application.id}. {job.title}")
        print(f"   Job ID:   {job.id}")
        print(f"   Company:  {job.company or 'N/A'}")
        print(f"   Source:   {job.source}")
        print(f"   Status:   {_format_application_status(application.current_status)}")
        print(f"   First:    {application.created_at}")
        print(f"   Updated:  {application.updated_at}")
        if item.latest_note is not None:
            print(f"   Note:     {item.latest_note}")

    return 0


def run_application_history(
    job_id: int,
    database_path: Path,
    *,
    limit: int = 20,
) -> int:
    """Show the event history for one tracked job (read-only)."""
    try:
        with connect_read_only(database_path) as connection:
            job = get_job_by_id(connection, job_id)
            if job is None:
                print(no_job_found_message(job_id), file=sys.stderr)
                return 1
            application = get_application(connection, job_id=job_id)
            if application is None:
                print(f"Job {job_id} is not currently tracked.")
                return 0
            events = list_application_events(
                connection,
                application_id=application.id,
                limit=limit,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Application failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"Application history for job {job_id} ({job.title})")
    print("-" * len(f"Application history for job {job_id} ({job.title})"))

    if not events:
        print()
        print("No history recorded.")
        return 0

    for event in events:
        old_status = enum_label(event.old_status, empty="-")
        print()
        print(f"Event {event.id} | {event.created_at}")
        print(f"  Status: {old_status} -> {enum_label(event.new_status)}")
        if event.note_redacted_at is not None:
            print("  Note:   [redacted]")
        elif event.note is not None:
            print(f"  Note:   {event.note}")

    return 0


def run_application_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch an ``application`` subcommand."""
    application_command = arguments.application_command

    if application_command in ("list", "history") and arguments.limit < 1:
        parser.error("--limit must be at least 1")

    if application_command in ("set", "show", "history") and arguments.job_id < 1:
        parser.error("job-id must be a positive integer")

    if application_command == "set":
        return run_application_set(
            job_id=arguments.job_id,
            status=arguments.status,
            note=arguments.note or None,
            database_path=arguments.database,
        )

    if application_command == "show":
        return run_application_show(arguments.job_id, arguments.database)

    if application_command == "list":
        return run_application_list(
            arguments.database,
            statuses=arguments.status,
            limit=arguments.limit,
        )

    return run_application_history(
        arguments.job_id,
        arguments.database,
        limit=arguments.limit,
    )
