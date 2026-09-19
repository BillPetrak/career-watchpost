"""The ``review`` command family (personalized daily review queue)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from careeros.cli.common import (
    EXIT_TEMP_FAILURE,
    _lock_conflict_message,
    load_profile_or_error,
    no_job_found_message,
)
from careeros.database.connection import connect, connect_read_only
from careeros.database.jobs import get_job_by_id
from careeros.database.review import latest_event_note as review_latest_event_note
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.presentation import enum_label
from careeros.review.feedback import build_feedback_set
from careeros.review.models import (
    UnknownJobError as ReviewUnknownJobError,
)
from careeros.review.models import (
    ValidationError as ReviewValidationError,
)
from careeros.review.render import render_queue_json, render_queue_text
from careeros.review.service import (
    build_queue,
    clear_decision,
    get_decision,
    list_decision_events,
    list_decisions,
    set_decision,
)


def run_review_set(
    job_id: int,
    decision: str,
    note: str | None,
    database_path: Path,
) -> int:
    """Set or update a job's review decision (write; acquires the lock)."""
    try:
        with collection_lock(database_path, command="review set"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                result = set_decision(
                    connection,
                    job_id=job_id,
                    decision=decision,
                    note=note,
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ReviewUnknownJobError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ReviewValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"Review failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    label = enum_label(decision)
    if result.created:
        print(f"Set decision {label} for job {job_id}.")
    elif result.changed:
        print(f"Updated job {job_id} to {label}.")
    else:
        print(f"Job {job_id} is already {label}; no change.")
    return 0


def run_review_clear(job_id: int, database_path: Path) -> int:
    """Clear a job's review decision (write; acquires the lock)."""
    try:
        with collection_lock(database_path, command="review clear"):
            with connect(database_path) as connection:
                initialize_schema(connection)
                result = clear_decision(connection, job_id=job_id)
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ReviewUnknownJobError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"Review failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if result.changed:
        print(f"Cleared decision for job {job_id}.")
    else:
        print(f"Job {job_id} has no decision; no change.")
    return 0


def run_review_show(job_id: int, database_path: Path) -> int:
    """Show the current review decision for one job (read-only)."""
    try:
        with connect_read_only(database_path) as connection:
            job = get_job_by_id(connection, job_id)
            if job is None:
                print(no_job_found_message(job_id), file=sys.stderr)
                return 1
            decision = get_decision(connection, job_id=job_id)
            note = None
            if decision is not None:
                note = review_latest_event_note(connection, job_id=job_id)
    except (OSError, sqlite3.Error) as error:
        print(f"Review failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if decision is None:
        print(f"Job {job_id} is not currently reviewed.")
        return 0

    print(f"Review decision for job {job_id} ({job.title})")
    print("-" * len(f"Review decision for job {job_id} ({job.title})"))
    print(f"{'Job ID:':<16}{job.id}")
    print(f"{'Title:':<16}{job.title}")
    print(f"{'Company:':<16}{job.company or 'N/A'}")
    print(f"{'Source:':<16}{job.source}")
    print(f"{'URL:':<16}{job.url or 'N/A'}")
    print(f"{'Decision:':<16}{enum_label(decision.decision)}")
    print(f"{'First reviewed:':<16}{decision.created_at}")
    print(f"{'Last updated:':<16}{decision.updated_at}")
    if note is not None:
        print(f"{'Note:':<16}{note}")
    return 0


def run_review_list(
    database_path: Path,
    *,
    decisions: Sequence[str] | None = None,
    limit: int = 20,
) -> int:
    """List review decisions, optionally filtered by decision (read-only)."""
    try:
        with connect_read_only(database_path) as connection:
            items = list_decisions(connection, decisions=decisions, limit=limit)
    except ReviewValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"Review failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Review decisions")
    print("----------------")

    if decisions:
        print(f"Filter: decision={', '.join(enum_label(item) for item in decisions)}")

    if not items:
        print()
        print("No review decisions recorded.")
        return 0

    for item in items:
        decision = item.decision
        job = item.job
        print()
        print(f"{decision.id}. {job.title}")
        print(f"   Job ID:   {job.id}")
        print(f"   Company:  {job.company or 'N/A'}")
        print(f"   Source:   {job.source}")
        print(f"   Decision: {enum_label(decision.decision)}")
        print(f"   First:    {decision.created_at}")
        print(f"   Updated:  {decision.updated_at}")
        if item.latest_note is not None:
            print(f"   Note:     {item.latest_note}")

    return 0


def run_review_history(
    job_id: int,
    database_path: Path,
    *,
    limit: int = 20,
) -> int:
    """Show the decision history for one job (read-only)."""
    try:
        with connect_read_only(database_path) as connection:
            job = get_job_by_id(connection, job_id)
            if job is None:
                print(no_job_found_message(job_id), file=sys.stderr)
                return 1
            events = list_decision_events(connection, job_id=job_id, limit=limit)
    except ReviewValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"Review failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"Review history for job {job_id} ({job.title})")
    print("-" * len(f"Review history for job {job_id} ({job.title})"))

    if not events:
        print()
        print("No history recorded.")
        return 0

    for event in events:
        old_decision = enum_label(event.old_decision, empty="-")
        new_decision = enum_label(event.new_decision, empty="None")
        print()
        print(f"Event {event.id} | {event.created_at}")
        print(f"  Decision: {old_decision} -> {new_decision}")
        if event.note is not None:
            print(f"  Note:   {event.note}")

    return 0


def run_review_next(
    database_path: Path,
    *,
    profile_path: Path,
    state: str,
    min_score: int,
    include_tracked: bool,
    source: str | None,
    location: str | None,
    remote: bool | None,
    limit: int,
    grouped: bool,
    output_format: str,
    use_feedback: bool = True,
    use_adaptive: bool = True,
) -> int:
    """Rank a personalized daily review queue (read-only, no lock)."""
    profile = load_profile_or_error(profile_path)
    if profile is None:
        return 1

    try:
        with connect_read_only(database_path) as connection:
            feedback = build_feedback_set(connection) if use_feedback else None
            outcome = build_queue(
                connection,
                profile=profile,
                state=state,
                min_score=min_score,
                include_tracked=include_tracked,
                source=source,
                location=location,
                remote=remote,
                limit=limit,
                grouped=grouped,
                feedback=feedback,
                use_adaptive=use_adaptive,
            )
    except ReviewValidationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(f"Review failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if output_format == "json":
        print(render_queue_json(outcome), end="")
    else:
        print(render_queue_text(outcome), end="")
    return 0


def run_review_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch a ``review`` subcommand."""
    review_command = arguments.review_command

    if review_command in ("list", "history", "next") and arguments.limit < 1:
        parser.error("--limit must be at least 1")

    if review_command in ("set", "clear", "show", "history") and arguments.job_id < 1:
        parser.error("job-id must be a positive integer")

    if review_command == "next" and not 0 <= arguments.min_score <= 100:
        parser.error("--min-score must be between 0 and 100")

    if review_command == "set":
        return run_review_set(
            job_id=arguments.job_id,
            decision=arguments.decision,
            note=arguments.note or None,
            database_path=arguments.database,
        )

    if review_command == "clear":
        return run_review_clear(arguments.job_id, arguments.database)

    if review_command == "show":
        return run_review_show(arguments.job_id, arguments.database)

    if review_command == "list":
        return run_review_list(
            arguments.database,
            decisions=arguments.decision,
            limit=arguments.limit,
        )

    if review_command == "history":
        return run_review_history(
            arguments.job_id,
            arguments.database,
            limit=arguments.limit,
        )

    return run_review_next(
        arguments.database,
        profile_path=arguments.profile,
        state=arguments.state,
        min_score=arguments.min_score,
        include_tracked=arguments.include_tracked,
        source=arguments.source,
        location=arguments.location,
        remote=arguments.remote,
        limit=arguments.limit,
        grouped=arguments.grouped,
        output_format=arguments.format,
        use_feedback=not arguments.no_feedback,
        use_adaptive=not arguments.no_adaptive,
    )
