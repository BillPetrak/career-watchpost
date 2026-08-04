"""Command-line interface for CareerOS."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from careeros.applications.models import APPLICATION_STATUSES
from careeros.applications.service import (
    GroupTrackingInfo,
    UnknownJobError,
    get_application,
    group_tracking_info,
    latest_event_note,
    list_application_events,
    list_applications,
    set_application_status,
)
from careeros.collecting import (
    CollectionResult,
    collect_all,
    collect_due,
    collect_one,
)
from careeros.collectors import COLLECTORS
from careeros.database.connection import connect
from careeros.database.groups import (
    GroupedJob,
    ensure_grouped,
    grouping_summary,
    list_group_infos,
)
from careeros.database.jobs import StoredJob, get_job_by_id
from careeros.database.runs import CollectionRun, list_runs, parse_utc
from careeros.database.schema import initialize_schema
from careeros.locking import (
    CollectionLockConflict,
    LockConflict,
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
from careeros.notifications.models import (
    ClaimedReminder,
    DeliveryResult,
)
from careeros.notifications.service import (
    attempts_for_reminder,
    claim_due,
    finalize_attempt,
    list_attempts,
    list_due_occurrences,
)
from careeros.ranking.profile import load_profile
from careeros.ranking.scorer import (
    CandidateProfile,
    MatchResult,
    RankedGroup,
    RankedJob,
)
from careeros.reminders.models import (
    REMINDER_KINDS,
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
    reminders_for_job,
    reopen_reminder,
    require_reminder,
    update_reminder,
)
from careeros.reporting.models import Report
from careeros.reporting.render import render_report, render_report_json
from careeros.reporting.service import build_report
from careeros.saved_searches.execution import (
    execute_rank,
    execute_search,
    rank_candidates,
    search_candidates,
)
from careeros.saved_searches.models import (
    SEARCH_MODES,
    DuplicateNameError,
    SavedSearch,
    SavedSearchFields,
    SavedSearchPatch,
    UnknownSearchError,
    ValidationError,
)
from careeros.saved_searches.service import (
    create_search,
    delete_search,
    get_search,
    list_searches,
    rename_search,
    update_search,
)
from careeros.status import build_statuses
from careeros.text import clean_description

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "careeros.sqlite3"
DEFAULT_PROFILE_PATH = PROJECT_ROOT / "config" / "candidate_profile.yaml"

# EX_TEMPFAIL from sysexits.h: a conventional "temporary failure, retry later"
# code, distinct from 1 (collection/setup failure) and 2 (argparse errors).
EXIT_TEMP_FAILURE = 75


def print_diagnostics() -> int:
    """Print basic CareerOS environment diagnostics."""
    database_directory = PROJECT_ROOT / "data" / "database"

    print("CareerOS diagnostics")
    print("--------------------")
    print(f"Project root:      {PROJECT_ROOT}")
    print(f"Python version:    {sys.version.split()[0]}")
    print(f"Python executable: {sys.executable}")
    print(f"SQLite library:    {sqlite3.sqlite_version}")
    print(f"Database directory exists: {database_directory.is_dir()}")
    print()
    print("Environment check passed.")

    return 0


_SOURCE_LABELS = {
    "arbeitnow": "Arbeitnow",
    "jobicy": "Jobicy",
    "remoteok": "Remote OK",
    "remotive": "Remotive",
    "weworkremotely": "We Work Remotely",
}


def run_collect(source: str, limit: int, database_path: Path) -> int:
    """Collect jobs from a registered source and save them to SQLite."""
    label = _SOURCE_LABELS.get(source, source)
    print(f"Collecting up to {limit} jobs from {label}...")

    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            result = collect_one(
                connection,
                source=source,
                collectors=COLLECTORS,
                limit=limit,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Collection failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if not result.succeeded:
        print(f"Collection failed: {result.error}", file=sys.stderr)
        return 1

    print()
    print("Collection complete")
    print("-------------------")
    print(f"Fetched:  {result.fetched}")
    print(f"Inserted: {result.inserted}")
    print(f"Updated:  {result.updated}")
    print(f"Database: {database_path}")

    return 0


def run_collect_all(limit: int, database_path: Path) -> int:
    """Collect from every registered source sequentially and print summaries."""
    results: list[CollectionResult] = []

    try:
        for result in collect_all(
            limit=limit,
            database_path=database_path,
            collectors=COLLECTORS,
        ):
            results.append(result)
            print()
            print(result.source)
            print("-" * len(result.source))

            if result.succeeded:
                print(f"Fetched:  {result.fetched}")
                print(f"Inserted: {result.inserted}")
                print(f"Updated:  {result.updated}")
                print("Status:   success")
            else:
                print("Status: failed")
                print(f"Error:  {result.error}")
                print(
                    f"{result.source} collection failed: {result.error}",
                    file=sys.stderr,
                )
    except (OSError, sqlite3.Error) as error:
        print(f"Collection failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    succeeded = sum(1 for result in results if result.succeeded)
    print()
    print("Multi-source collection complete")
    print("-" * len("Multi-source collection complete"))
    print(f"Sources attempted: {len(results)}")
    print(f"Succeeded:         {succeeded}")
    print(f"Failed:            {len(results) - succeeded}")
    print(f"Fetched:           {sum(result.fetched for result in results)}")
    print(f"Inserted:          {sum(result.inserted for result in results)}")
    print(f"Updated:           {sum(result.updated for result in results)}")
    print(f"Database:          {database_path}")

    return 0 if succeeded == len(results) else 1


def run_collect_due(limit: int, database_path: Path) -> int:
    """Collect only sources whose polling interval has expired."""
    results: list[CollectionResult] = []

    try:
        for result in collect_due(
            limit=limit,
            database_path=database_path,
            collectors=COLLECTORS,
        ):
            results.append(result)
            print()
            print(result.source)
            print("-" * len(result.source))

            if result.succeeded:
                print(f"Fetched:  {result.fetched}")
                print(f"Inserted: {result.inserted}")
                print(f"Updated:  {result.updated}")
                print("Status:   success")
            else:
                print("Status: failed")
                print(f"Error:  {result.error}")
                print(
                    f"{result.source} collection failed: {result.error}",
                    file=sys.stderr,
                )
    except (OSError, sqlite3.Error) as error:
        print(f"Collection failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    succeeded = sum(1 for result in results if result.succeeded)
    print()
    print("Due-source collection complete")
    print("-" * len("Due-source collection complete"))

    if not results:
        print("No sources are due.")

    print(f"Sources attempted: {len(results)}")
    print(f"Succeeded:         {succeeded}")
    print(f"Failed:            {len(results) - succeeded}")
    print(f"Fetched:           {sum(result.fetched for result in results)}")
    print(f"Inserted:          {sum(result.inserted for result in results)}")
    print(f"Updated:           {sum(result.updated for result in results)}")
    print(f"Database:          {database_path}")

    return 0 if succeeded == len(results) else 1


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _format_interval(interval: timedelta) -> str:
    """Render a polling interval as a short human-readable string."""
    total_seconds = int(interval.total_seconds())
    if total_seconds % 3600 == 0:
        hours = total_seconds // 3600
        return "1 hour" if hours == 1 else f"{hours} hours"
    minutes = total_seconds // 60
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _format_duration(started_at: str | None, finished_at: str | None) -> str:
    """Format elapsed wall time between two stored timestamps, or '-'.

    Returns '-' when either timestamp is missing or malformed.
    """
    if started_at is None or finished_at is None:
        return "-"
    start = parse_utc(started_at)
    finish = parse_utc(finished_at)
    if start is None or finish is None:
        return "-"
    return f"{(finish - start).total_seconds():.1f}s"


def _run_status(run: CollectionRun) -> str:
    """Derive the display status of a stored run."""
    if run.succeeded == 1:
        return "success"
    if run.succeeded == 0:
        return "failed"
    return "incomplete"


def run_collect_status(database_path: Path) -> int:
    """Show polling status for every registered source without mutating state."""
    now = _utc_now()
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            statuses = build_statuses(connection, now=now)
    except (OSError, sqlite3.Error) as error:
        print(f"Status failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Collection status")
    print("-----------------")
    print(f"As of: {now.isoformat()}")

    for status in statuses:
        print()
        print(_SOURCE_LABELS.get(status.source, status.source))
        print(
            f"  Interval:       {_format_interval(status.min_interval)}"
            f" ({'documented' if status.documented else 'provisional'})"
        )
        if status.last_attempt is not None:
            print(f"  Last attempt:   {status.last_attempt.isoformat()}")
            print(
                "  Last status:    "
                + ("success" if status.last_succeeded else "failed")
            )
            print(f"  Fetched:        {status.last_fetched}")
            print(f"  Inserted:       {status.last_inserted}")
            print(f"  Updated:        {status.last_updated}")
            print(f"  Error:          {status.last_error or '-'}")
        else:
            print("  Last attempt:   never")
            print("  Last status:    never")
            print("  Fetched:        -")
            print("  Inserted:       -")
            print("  Updated:        -")
            print("  Error:          -")
        print(
            "  Next due:       "
            + (status.next_due.isoformat() if status.next_due is not None else "now")
        )
        print(f"  State:          {'due' if status.due else 'not due'}")
        if status.incomplete_runs:
            print(f"  Incomplete runs: {status.incomplete_runs}")

    return 0


def run_collect_history(
    *,
    source: str | None,
    limit: int,
    failed: bool,
    database_path: Path,
) -> int:
    """Show recent collection run history without mutating state."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            runs = list_runs(
                connection,
                source=source,
                limit=limit,
                failed_only=failed,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"History failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Run history")
    print("-----------")

    filters = []
    if source is not None:
        filters.append(f"source={source}")
    if failed:
        filters.append("failed only")
    if filters:
        print(f"Filter: {', '.join(filters)}")

    if not runs:
        print()
        print("No collection runs found.")
        return 0

    for run in runs:
        print()
        print(f"Run {run.run_id} ({run.source})")
        print(f"  Status:   {_run_status(run)}")
        print(f"  Started:  {run.started_at or '-'}")
        print(f"  Finished: {run.finished_at or '-'}")
        print(f"  Elapsed:  {_format_duration(run.started_at, run.finished_at)}")
        print(f"  Fetched:  {run.fetched}")
        print(f"  Inserted: {run.inserted}")
        print(f"  Updated:  {run.updated}")
        if run.error:
            print(f"  Error:    {run.error}")

    return 0


def _sources_line(group: GroupedJob) -> str:
    """Render a human-readable source list for a grouped result."""
    if len(group.sources) == 1:
        return str(group.sources[0])
    return ", ".join(str(s) for s in group.sources)


def _print_grouped_results(
    results: Sequence[GroupedJob],
    tracking_statuses: dict[int, str] | None = None,
) -> None:
    """Print grouped search results with per-group member details."""
    for i, group in enumerate(results, start=1):
        rep = group.representative
        location_part = rep.location or "N/A"
        if rep.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        print(f"  {i}. {rep.title}")
        print(f"     Company:  {rep.company or 'N/A'}")
        print(f"     Location: {location_part}")
        print(f"     Source:   {_sources_line(group)}")
        if tracking_statuses is not None:
            print(
                "     Application: "
                f"{_application_line(group_tracking_info(group, tracking_statuses))}"
            )
        member_count = len(group.members)
        if member_count > 1:
            print(f"     Group:    {member_count} listings grouped")
        print()

        if member_count > 1:
            print("     Members:")
            for member in group.members:
                print(f"       - [{member.id}] {member.source}: {member.url or 'N/A'}")
        print()


def _render_search_flat(
    results: Sequence[StoredJob],
    tracking_statuses: dict[int, str] | None,
) -> None:
    """Render flat search results (shared by search and saved-search run)."""
    print(f"Found {len(results)} matching job{'s' if len(results) != 1 else ''}:")
    print()

    for i, job in enumerate(results, start=1):
        location_part = job.location or "N/A"
        if job.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        print(f"  {i}. {job.title}")
        print(f"     Company:  {job.company or 'N/A'}")
        print(f"     Location: {location_part}")
        print(f"     Source:   {job.source}")
        if tracking_statuses is not None:
            print(
                "     Application: "
                f"{_format_application_status(tracking_statuses.get(job.id))}"
            )
        print()


def _render_search_grouped(
    results: Sequence[GroupedJob],
    tracking_statuses: dict[int, str] | None,
) -> None:
    """Render grouped search results (shared by search and saved-search run)."""
    print(f"Found {len(results)} matching group{'s' if len(results) != 1 else ''}:")
    print()
    _print_grouped_results(results, tracking_statuses)


def _render_rank_flat(
    ranked: Sequence[RankedJob],
    tracking_statuses: dict[int, str] | None,
    *,
    min_score: int,
    candidate_count: int,
) -> None:
    """Render flat rank results (shared by rank and saved-search run)."""
    if candidate_count == 0:
        print("No matching jobs found.")
        return
    if not ranked:
        if min_score > 0:
            print(f"No jobs meet the minimum score of {min_score}.")
        else:
            print("No matching jobs found.")
        return

    print(f"Ranked {len(ranked)} job{'s' if len(ranked) != 1 else ''}:")
    print()

    for i, job in enumerate(ranked, start=1):
        location_part = job.job.location or "N/A"
        if job.job.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        print(f"{i}. [Score {job.match_score}/100] {job.job.title}")
        print(f"   Match:    {job.match_score}/100")
        print(f"   ID:       {job.job.id}")
        print(f"   Company:  {job.job.company or 'N/A'}")
        print(f"   Location: {location_part}")
        print(f"   Remote:   {job.job.remote_status or 'N/A'}")
        print(f"   Source:   {job.job.source}")
        print(f"   URL:      {job.job.url or 'N/A'}")
        if tracking_statuses is not None:
            print(
                "   Application: "
                f"{_format_application_status(tracking_statuses.get(job.job.id))}"
            )
        _print_match_sections(job.result)
        print("   Reasons:")
        for reason in job.reasons:
            print(f"     {reason}")
        print()


def _render_rank_grouped(
    ranked: Sequence[RankedGroup],
    tracking_statuses: dict[int, str] | None,
    *,
    min_score: int,
    candidate_count: int,
) -> None:
    """Render grouped rank results (shared by rank and saved-search run)."""
    if candidate_count == 0:
        print("No matching jobs found.")
        return
    if not ranked:
        if min_score > 0:
            print(f"No groups meet the minimum score of {min_score}.")
        else:
            print("No matching jobs found.")
        return

    print(f"Ranked {len(ranked)} group{'s' if len(ranked) != 1 else ''}:")
    print()

    for i, ranked_group in enumerate(ranked, start=1):
        group = ranked_group.group
        rep = group.representative
        location_part = rep.location or "N/A"
        if rep.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        urls = ", ".join(url for url in group.urls if url)
        print(f"{i}. [Score {ranked_group.match_score}/100] {rep.title}")
        print(f"   Match:    {ranked_group.match_score}/100")
        print(f"   ID:       {rep.id}")
        print(f"   Company:  {rep.company or 'N/A'}")
        print(f"   Location: {location_part}")
        print(f"   Remote:   {rep.remote_status or 'N/A'}")
        print(f"   Source:   {_sources_line(group)}")
        print(f"   URL:      {urls or 'N/A'}")
        if tracking_statuses is not None:
            application_label = _application_line(
                group_tracking_info(group, tracking_statuses)
            )
            print(f"   Application: {application_label}")
        member_count = len(group.members)
        if member_count > 1:
            print(f"   Group:    {member_count} listings grouped")
        _print_match_sections(ranked_group.result)
        print("   Reasons:")
        for reason in ranked_group.reasons:
            print(f"     {reason}")
        print()


def run_group_regroup(database_path: Path) -> int:
    """Recompute canonical duplicate groups and persist them."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            ensure_grouped(connection)
            summary = grouping_summary(connection)
    except (OSError, sqlite3.Error) as error:
        print(f"Regroup failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Regrouping complete")
    print("-------------------")
    print(f"Total jobs:      {summary.total_jobs}")
    print(f"Groups:          {summary.groups}")
    print(f"Grouped members: {summary.grouped_jobs}")

    return 0


def run_group_explain(
    database_path: Path,
    *,
    group_id: str | None,
    limit: int,
) -> int:
    """Show canonical groups (with members) without mutating state."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            infos = list_group_infos(
                connection,
                group_id=group_id,
                limit=limit,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Explain failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Canonical groups")
    print("----------------")

    if not infos:
        print()
        print("No groups found.")
        return 0

    for info in infos:
        print()
        print(f"Group {info.group_id}")
        print(f"  Reason:    {info.reason}")
        print(f"  Sources:   {', '.join(str(s) for s in info.sources)}")
        print(f"  Created:   {info.created_at}")
        print("  Members:")
        for member in info.members:
            print(f"    - [{member.id}] {member.source}: {member.title}")

    return 0


def _format_application_status(status: str | None) -> str:
    """Render a stored application status (or 'Untracked') for display."""
    return status.capitalize() if status else "Untracked"


def _application_line(info: GroupTrackingInfo) -> str:
    """Render one 'Application:' line for a grouped result.

    Distinguishes untracked results, results tracked directly on the displayed
    representative, and results tracked through another member of the group.
    """
    if info.status is None:
        return "Untracked"
    label = info.status.capitalize()
    if info.on_representative or info.owner is None:
        return label
    return f"{label} (via job {info.owner.id}, {info.owner.source})"


def run_application_set(
    job_id: int,
    status: str,
    note: str | None,
    database_path: Path,
) -> int:
    """Set or update a job's application status (write; acquires the lock)."""
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

    if result.created:
        print(f"Tracked job {job_id} as {status}.")
    elif result.changed:
        print(f"Updated job {job_id} to {status}.")
    else:
        print(f"Job {job_id} is already {status}; no change.")
    return 0


def run_application_show(job_id: int, database_path: Path) -> int:
    """Show the current application state for one job (read-only)."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            job = get_job_by_id(connection, job_id)
            if job is None:
                print(f"No job found with ID {job_id}.", file=sys.stderr)
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
                f"  - [{reminder.id}] {reminder.kind}, "
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
        with connect(database_path) as connection:
            initialize_schema(connection)
            items = list_applications(connection, statuses=statuses, limit=limit)
    except (OSError, sqlite3.Error) as error:
        print(f"Application failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Tracked applications")
    print("--------------------")

    if statuses:
        print(f"Filter: status={', '.join(statuses)}")

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
        with connect(database_path) as connection:
            initialize_schema(connection)
            job = get_job_by_id(connection, job_id)
            if job is None:
                print(f"No job found with ID {job_id}.", file=sys.stderr)
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
        old_status = event.old_status or "-"
        print()
        print(f"Event {event.id} | {event.created_at}")
        print(f"  Status: {old_status} -> {event.new_status}")
        if event.note is not None:
            print(f"  Note:   {event.note}")

    return 0


def _reminder_state_text(completed_at: str | None) -> str:
    """Render a reminder's completion state for display."""
    return "open" if completed_at is None else f"completed {completed_at}"


def _reminder_message_line(message: str | None) -> str | None:
    """Render a reminder message line, or None when absent."""
    return f"Message: {message}" if message is not None else None


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
        with connect(database_path) as connection:
            initialize_schema(connection)
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
        with connect(database_path) as connection:
            initialize_schema(connection)
            reminder = require_reminder(connection, reminder_id)
            job = get_job_by_id(connection, reminder.job_id)
            if job is None:
                print(f"No job found with ID {reminder.job_id}.", file=sys.stderr)
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
                    delivered_at=_utc_now().isoformat() if result.success else None,
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

    now = _utc_now()

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
        with connect(database_path) as connection:
            initialize_schema(connection)
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


def run_search(
    keyword: str,
    database_path: Path,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    limit: int = 20,
    grouped: bool = False,
    untracked: bool = False,
    include_statuses: Sequence[str] = (),
    exclude_statuses: Sequence[str] = (),
) -> int:
    """Search jobs in the SQLite database."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            outcome = search_candidates(
                connection,
                query=keyword,
                remote=remote,
                location=location,
                source=source,
                limit=limit,
                grouped=grouped,
                untracked=untracked,
                include_statuses=tuple(include_statuses),
                exclude_statuses=tuple(exclude_statuses),
            )
    except Exception as error:
        print(f"Search failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if outcome.grouped:
        _render_search_grouped(outcome.groups, outcome.tracking_statuses)
    else:
        _render_search_flat(outcome.flat, outcome.tracking_statuses)

    return 0


def run_show(job_id: int, database_path: Path) -> int:
    """Display a single job by its database ID."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            job = get_job_by_id(connection, job_id)
    except Exception as error:
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if job is None:
        print(f"No job found with ID {job_id}.", file=sys.stderr)
        return 1

    print(f"ID:               {job.id}")
    print(f"Title:            {job.title}")
    print(f"Company:          {job.company or 'N/A'}")
    print(f"Location:         {job.location or 'N/A'}")
    print(f"Remote status:    {job.remote_status or 'N/A'}")
    print(f"Employment type:  {job.employment_type or 'N/A'}")
    print(f"Source:           {job.source}")
    print(f"Source job ID:    {job.source_job_id or 'N/A'}")
    print(f"Published:        {job.published_at or 'N/A'}")
    print(f"Collected:        {job.collected_at}")
    print(f"URL:              {job.url or 'N/A'}")
    print()
    print("Description:")
    print(clean_description(job.description) if job.description else "N/A")

    return 0


_PREFERRED_MISSING_DISPLAY_CAP = 8


def _print_match_sections(result: MatchResult) -> None:
    """Print the Matched / Missing or unmatched summary for a scored result."""
    print("     Matched:")
    printed = False
    for title in result.matched_titles:
        print(f"       - Title: '{title}'")
        printed = True
    for skill in result.matched_skills:
        print(f"       - {skill}")
        printed = True
    if result.location_matched is not None:
        print(f"       - Location: {result.location_matched}")
        printed = True
    if result.remote_matched:
        print("       - Remote position")
        printed = True
    if result.employment_matched is not None:
        print(f"       - Employment type: {result.employment_matched}")
        printed = True
    if result.seniority_matched is not None:
        print(f"       - Seniority: {result.seniority_matched}")
        printed = True
    if not printed:
        print("       - none")

    print("     Missing or unmatched:")
    printed = False
    if result.description_unavailable:
        print("       - Description unavailable; skills not evaluated")
        printed = True
    else:
        for skill in result.missing_required_skills:
            print(f"       - {skill} (required, not found)")
            printed = True
        preferred = result.preferred_skills_not_observed
        for skill in preferred[:_PREFERRED_MISSING_DISPLAY_CAP]:
            print(f"       - {skill} (preferred, not observed)")
            printed = True
        remaining = len(preferred) - _PREFERRED_MISSING_DISPLAY_CAP
        if remaining > 0:
            print(
                f"       - ... and {remaining} more preferred skill"
                f"{'s' if remaining != 1 else ''}"
            )
            printed = True
    if result.employment_mismatch:
        print("       - Employment type mismatch")
        printed = True
    if result.seniority_mismatch:
        print("       - Seniority mismatch")
        printed = True
    for term in result.excluded_terms:
        print(f"       - Contains excluded term '{term}'")
        printed = True
    for term in result.hard_excluded_terms:
        print(f"       - Hard excluded: contains '{term}'")
        printed = True
    if not printed:
        print("       - none")


def run_rank(
    keyword: str,
    profile_path: Path,
    database_path: Path,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    limit: int = 20,
    grouped: bool = False,
    min_score: int = 0,
    untracked: bool = False,
    include_statuses: Sequence[str] = (),
    exclude_statuses: Sequence[str] = (),
) -> int:
    """Search jobs and rank them against a candidate profile."""
    try:
        profile = load_profile(profile_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Failed to load profile: {error}", file=sys.stderr)
        return 1

    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            outcome = rank_candidates(
                connection,
                query=keyword,
                profile=profile,
                remote=remote,
                location=location,
                source=source,
                limit=limit,
                grouped=grouped,
                min_score=min_score,
                untracked=untracked,
                include_statuses=tuple(include_statuses),
                exclude_statuses=tuple(exclude_statuses),
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Search failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if outcome.grouped:
        _render_rank_grouped(
            outcome.grouped_ranked,
            outcome.tracking_statuses,
            min_score=min_score,
            candidate_count=outcome.grouped_candidate_count,
        )
    else:
        _render_rank_flat(
            outcome.flat_ranked,
            outcome.tracking_statuses,
            min_score=min_score,
            candidate_count=outcome.flat_candidate_count,
        )

    return 0


_PROFILE_YAML_SUFFIXES = {".yaml", ".yml"}
_PROFILE_JSON_SUFFIXES = {".json"}

_YAML_PROFILE_TEMPLATE = """\
# CareerOS candidate profile.
#
# Describes the roles you are looking for. All fields are optional and unknown
# top-level fields are ignored. This file is private: it is listed in
# .gitignore and must never be committed to version control.
#
#   desired_titles:             ["Machine Learning Engineer", "Data Scientist"]
#   skills:                     ["Python", "SQL"]
#   required_skills:            ["Python"]
#   preferred_locations:        ["Greece", "Remote"]
#   remote_preference:          "remote" or "onsite_or_unspecified" or null
#   excluded_terms:             ["intern"]
#   hard_excluded_terms:        ["commission"]
#   preferred_employment_types: ["Full-time"]
#   preferred_seniority:        ["Senior"]

desired_titles: []
skills: []
required_skills: []
preferred_locations: []
remote_preference: null
excluded_terms: []
hard_excluded_terms: []
preferred_employment_types: []
preferred_seniority: []
"""

_JSON_PROFILE_TEMPLATE = """\
{
  "desired_titles": [],
  "skills": [],
  "required_skills": [],
  "preferred_locations": [],
  "remote_preference": null,
  "excluded_terms": [],
  "hard_excluded_terms": [],
  "preferred_employment_types": [],
  "preferred_seniority": []
}
"""


def _render_profile(profile: CandidateProfile) -> None:
    """Render a candidate profile as readable labeled lines."""
    print(f"{'Desired titles:':<24}{', '.join(profile.desired_titles) or '-'}")
    print(f"{'Skills:':<24}{', '.join(profile.skills) or '-'}")
    print(f"{'Required skills:':<24}{', '.join(profile.required_skills) or '-'}")
    print(
        f"{'Preferred locations:':<24}{', '.join(profile.preferred_locations) or '-'}"
    )
    print(f"{'Remote preference:':<24}{profile.remote_preference or '-'}")
    print(f"{'Excluded terms:':<24}{', '.join(profile.excluded_terms) or '-'}")
    print(
        f"{'Hard excluded terms:':<24}{', '.join(profile.hard_excluded_terms) or '-'}"
    )
    print(
        f"{'Employment types:':<24}"
        f"{', '.join(profile.preferred_employment_types) or '-'}"
    )
    print(f"{'Seniority:':<24}{', '.join(profile.preferred_seniority) or '-'}")


def run_profile_init(profile_path: Path | None, *, force: bool) -> int:
    """Create a starter candidate profile at a path (default: private path).

    The format is chosen by file extension: ``.yaml``/``.yml`` write a YAML
    starter and ``.json`` a JSON starter. Refuses to overwrite an existing file
    unless ``--force`` is passed.
    """
    path = profile_path if profile_path is not None else DEFAULT_PROFILE_PATH

    suffix = path.suffix.lower()
    if suffix in _PROFILE_YAML_SUFFIXES:
        template = _YAML_PROFILE_TEMPLATE
    elif suffix in _PROFILE_JSON_SUFFIXES:
        template = _JSON_PROFILE_TEMPLATE
    else:
        print(
            f"Unsupported profile file extension {path.suffix!r}; "
            "expected .json, .yaml, or .yml.",
            file=sys.stderr,
        )
        return 2

    if path.exists() and not force:
        print(f"Profile already exists: {path}", file=sys.stderr)
        print("Use --force to overwrite it.", file=sys.stderr)
        return 1

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template, encoding="utf-8")
    except OSError as error:
        print(
            f"Failed to create profile: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print(f"Created candidate profile: {path}")
    return 0


def run_profile_show(profile_path: Path | None) -> int:
    """Show a candidate profile from a JSON or YAML file (read-only, no DB)."""
    path = profile_path if profile_path is not None else DEFAULT_PROFILE_PATH
    try:
        profile = load_profile(path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Failed to load profile: {error}", file=sys.stderr)
        return 1

    print(f"Profile: {path}")
    print("-" * len(f"Profile: {path}"))
    _render_profile(profile)
    return 0


def run_profile_validate(profile_path: Path | None) -> int:
    """Validate a candidate profile file without displaying its contents."""
    path = profile_path if profile_path is not None else DEFAULT_PROFILE_PATH
    try:
        load_profile(path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Failed to load profile: {error}", file=sys.stderr)
        return 1

    print(f"Profile {path} is valid.")
    return 0


def run_report(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Show a read-only Career Snapshot report (never writes or locks)."""
    if arguments.limit < 1:
        parser.error("--limit must be at least 1")
    if arguments.saved_search is not None and arguments.grouped:
        parser.error("--grouped cannot be combined with --saved-search")

    saved_search: SavedSearch | None = None
    profile: CandidateProfile | None = None
    report: Report | None = None

    try:
        with connect(arguments.database) as connection:
            initialize_schema(connection)

            if arguments.saved_search is not None:
                definition = get_search(connection, arguments.saved_search)
                if definition is None:
                    print(
                        f"No saved search named {arguments.saved_search!r}.",
                        file=sys.stderr,
                    )
                    return 1
                if definition.mode != "rank":
                    print(
                        f"Saved search {definition.name!r} is mode={definition.mode}; "
                        "report --saved-search requires a rank-mode saved search.",
                        file=sys.stderr,
                    )
                    return 2
                saved_search = definition

            if arguments.profile is not None:
                profile = load_profile(arguments.profile)
            elif saved_search is not None:
                if not saved_search.profile_path:
                    print(
                        f"Rank saved search {saved_search.name!r} has no profile path.",
                        file=sys.stderr,
                    )
                    return 1
                profile = load_profile(Path(saved_search.profile_path))

            report = build_report(
                connection,
                now=_utc_now(),
                profile=profile,
                saved_search=saved_search,
                limit=arguments.limit,
                grouped=arguments.grouped,
            )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Failed to load profile: {error}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as error:
        print(f"Report failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    assert report is not None
    if arguments.format == "json":
        print(render_report_json(report), end="")
    else:
        print(render_report(report), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CareerOS command-line parser."""
    parser = argparse.ArgumentParser(
        prog="careeros",
        description="Local-first career intelligence tools.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "diagnose",
        help="Check the CareerOS development environment.",
    )

    collect_parser = subparsers.add_parser(
        "collect",
        help="Collect jobs from an external source.",
        epilog=(
            "For 'all', sources run sequentially in a single invocation, each "
            "called once. For 'due', only sources whose last completed run is "
            "older than their polling interval are collected. Do not run "
            "repeated all-source collections more often than the most "
            "restrictive source permits (Jobicy allows at most one poll per "
            "hour)."
        ),
    )
    collect_commands = collect_parser.add_subparsers(
        dest="collect_command",
        required=True,
    )

    for name in ("all", "due", *COLLECTORS):
        if name == "all":
            command_help = "Collect from every registered source."
        elif name == "due":
            command_help = "Collect only sources whose polling interval has expired."
        else:
            command_help = f"Collect jobs from {_SOURCE_LABELS.get(name, name)}."
        command_parser = collect_commands.add_parser(name, help=command_help)
        command_parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum jobs to collect (default: 25).",
        )
        command_parser.add_argument(
            "--database",
            type=Path,
            default=DEFAULT_DATABASE_PATH,
            help="SQLite database path.",
        )

    status_parser = collect_commands.add_parser(
        "status",
        help="Show collection status for every registered source.",
    )
    status_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    history_parser = collect_commands.add_parser(
        "history",
        help="Show recent collection run history.",
    )
    history_parser.add_argument(
        "--source",
        choices=[*COLLECTORS],
        help="Only show runs for this source.",
    )
    history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum runs to show (default: 20).",
    )
    history_parser.add_argument(
        "--failed",
        action="store_true",
        help="Only show completed failed runs.",
    )
    history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    application_parser = subparsers.add_parser(
        "application",
        help="Track application workflow state for stored jobs.",
    )
    application_commands = application_parser.add_subparsers(
        dest="application_command",
        required=True,
    )

    set_parser = application_commands.add_parser(
        "set",
        help="Set or update the application status for a stored job.",
    )
    set_parser.add_argument(
        "job_id",
        type=int,
        help="Database job ID.",
    )
    set_parser.add_argument(
        "status",
        choices=APPLICATION_STATUSES,
        help="Application status (one of: %(choices)s).",
    )
    set_parser.add_argument(
        "--note",
        default=None,
        help="Optional note attached to this status change.",
    )
    set_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = application_commands.add_parser(
        "show",
        help="Show the current application state for one job.",
    )
    show_parser.add_argument(
        "job_id",
        type=int,
        help="Database job ID.",
    )
    show_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    list_parser = application_commands.add_parser(
        "list",
        help="List tracked applications.",
    )
    list_parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Only show applications with this status (repeatable).",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum applications to show (default: 20).",
    )
    list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    history_parser = application_commands.add_parser(
        "history",
        help="Show the event history for one tracked job.",
    )
    history_parser.add_argument(
        "job_id",
        type=int,
        help="Database job ID.",
    )
    history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum events to show (default: 20).",
    )
    history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    search_parser = subparsers.add_parser(
        "search",
        help="Search collected jobs.",
    )
    search_parser.add_argument(
        "keyword",
        help="Search keyword matching title, company, or description.",
    )
    search_parser.add_argument(
        "--remote",
        action="store_true",
        help="Only show remote jobs.",
    )
    search_parser.add_argument(
        "--location",
        help="Filter by location (partial match).",
    )
    search_parser.add_argument(
        "--source",
        help="Filter by source (e.g. arbeitnow).",
    )
    search_parser.add_argument(
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates into one result per group.",
    )
    search_parser.add_argument(
        "--untracked",
        action="store_true",
        help="Only show jobs with no application record.",
    )
    search_parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Only show jobs with this application status (repeatable).",
    )
    search_parser.add_argument(
        "--exclude-status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Hide jobs with this application status (repeatable).",
    )
    search_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum results (default: 20).",
    )
    search_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = subparsers.add_parser(
        "show",
        help="Show a single job by database ID.",
    )
    show_parser.add_argument(
        "job_id",
        type=int,
        help="Database job ID.",
    )
    show_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    rank_parser = subparsers.add_parser(
        "rank",
        help="Search and rank jobs against a candidate profile.",
    )
    rank_parser.add_argument(
        "keyword",
        help="Search keyword matching title, company, or description.",
    )
    rank_parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Path to a candidate profile JSON file.",
    )
    rank_parser.add_argument(
        "--remote",
        action="store_true",
        help="Only show remote jobs.",
    )
    rank_parser.add_argument(
        "--location",
        help="Filter by location (partial match).",
    )
    rank_parser.add_argument(
        "--source",
        help="Filter by source (e.g. arbeitnow).",
    )
    rank_parser.add_argument(
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates into one result per group.",
    )
    rank_parser.add_argument(
        "--untracked",
        action="store_true",
        help="Only show jobs with no application record.",
    )
    rank_parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Only show jobs with this application status (repeatable).",
    )
    rank_parser.add_argument(
        "--exclude-status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Hide jobs with this application status (repeatable).",
    )
    rank_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum results (default: 20).",
    )
    rank_parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Only show results with a match score of at least N (0-100).",
    )
    rank_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    profile_parser = subparsers.add_parser(
        "profile",
        help="Inspect, validate, and initialize candidate profile files.",
    )
    profile_commands = profile_parser.add_subparsers(
        dest="profile_command",
        required=True,
    )

    init_parser = profile_commands.add_parser(
        "init",
        help="Create a starter candidate profile at the private path (or PATH).",
    )
    init_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Destination path (.json/.yaml/.yml); defaults to the private "
            "config/candidate_profile.yaml."
        ),
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the file if it already exists.",
    )

    show_parser = profile_commands.add_parser(
        "show",
        help="Show a candidate profile from a JSON or YAML file.",
    )
    show_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Path to a candidate profile JSON or YAML file; defaults to the "
            "private config/candidate_profile.yaml."
        ),
    )

    validate_parser = profile_commands.add_parser(
        "validate",
        help="Validate a candidate profile file.",
    )
    validate_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Path to a candidate profile JSON or YAML file; defaults to the "
            "private config/candidate_profile.yaml."
        ),
    )

    report_parser = subparsers.add_parser(
        "report",
        help=(
            "Show a read-only Career Snapshot of jobs, applications, and "
            "collection state."
        ),
    )
    report_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )
    report_parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help=(
            "Path to a candidate profile JSON file "
            "(overrides a saved search's profile)."
        ),
    )
    report_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum recommendations and action items to show (default: 10).",
    )
    report_parser.add_argument(
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates in recommendations (flat default).",
    )
    report_parser.add_argument(
        "--saved-search",
        default=None,
        help="Name of a rank-mode saved search whose filters drive recommendations.",
    )
    report_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )

    group_parser = subparsers.add_parser(
        "group",
        help="Manage canonical cross-source duplicate groups.",
    )
    group_commands = group_parser.add_subparsers(
        dest="group_command",
        required=True,
    )

    regroup_parser = group_commands.add_parser(
        "regroup",
        help="Recompute canonical groups from stored jobs.",
    )
    regroup_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    explain_parser = group_commands.add_parser(
        "explain",
        help="Show canonical groups without mutating state.",
    )
    explain_parser.add_argument(
        "--group-id",
        help="Only show this group.",
    )
    explain_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum groups to show (default: 20).",
    )
    explain_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    reminder_parser = subparsers.add_parser(
        "reminder",
        help="Manage local follow-up reminders for stored jobs.",
    )
    reminder_commands = reminder_parser.add_subparsers(
        dest="reminder_command",
        required=True,
    )

    add_parser = reminder_commands.add_parser(
        "add",
        help="Add a reminder for a stored job.",
    )
    add_parser.add_argument("job_id", type=int, help="Database job ID.")
    add_parser.add_argument(
        "kind",
        choices=REMINDER_KINDS,
        help="Reminder kind (one of: %(choices)s).",
    )
    add_parser.add_argument(
        "due_at",
        help=(
            "Due timestamp as ISO-8601 with an explicit UTC offset, e.g. "
            "2026-08-10T14:00:00Z."
        ),
    )
    add_parser.add_argument(
        "--message", default=None, help="Optional reminder message."
    )
    add_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    list_parser = reminder_commands.add_parser(
        "list",
        help="List reminders for stored jobs.",
    )
    state_group = list_parser.add_mutually_exclusive_group()
    state_group.add_argument(
        "--open",
        action="store_const",
        const="open",
        dest="state",
        help="Only show open (not completed) reminders (default).",
    )
    state_group.add_argument(
        "--overdue",
        action="store_const",
        const="overdue",
        dest="state",
        help="Only show open reminders whose due time has passed.",
    )
    state_group.add_argument(
        "--due",
        action="store_const",
        const="due",
        dest="state",
        help="Only show open reminders due now or earlier.",
    )
    state_group.add_argument(
        "--upcoming",
        action="store_const",
        const="upcoming",
        dest="state",
        help="Only show open reminders due in the future.",
    )
    state_group.add_argument(
        "--completed",
        action="store_const",
        const="completed",
        dest="state",
        help="Only show completed reminders.",
    )
    list_parser.add_argument(
        "--job",
        type=int,
        dest="job_id",
        default=None,
        help="Only show reminders for this job.",
    )
    list_parser.add_argument(
        "--kind",
        choices=REMINDER_KINDS,
        default=None,
        help="Only show reminders of this kind.",
    )
    list_parser.add_argument(
        "--after",
        default=None,
        help="Only show reminders due at or after this ISO-8601 timestamp.",
    )
    list_parser.add_argument(
        "--before",
        default=None,
        help="Only show reminders due at or before this ISO-8601 timestamp.",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum reminders to show (default: 20).",
    )
    list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = reminder_commands.add_parser(
        "show",
        help="Show one reminder by ID.",
    )
    show_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    show_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    update_parser = reminder_commands.add_parser(
        "update",
        help="Change a reminder's due time, kind, or message.",
    )
    update_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    update_parser.add_argument(
        "--due-at",
        dest="due_at",
        default=None,
        help="New due timestamp (ISO-8601 with an explicit UTC offset).",
    )
    update_parser.add_argument(
        "--kind",
        choices=REMINDER_KINDS,
        default=None,
        help="New reminder kind.",
    )
    message_group = update_parser.add_mutually_exclusive_group()
    message_group.add_argument(
        "--message",
        dest="message",
        default=None,
        help="New reminder message.",
    )
    message_group.add_argument(
        "--clear-message",
        action="store_true",
        help="Clear the reminder message.",
    )
    update_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    complete_parser = reminder_commands.add_parser(
        "complete",
        help="Mark an open reminder completed.",
    )
    complete_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    complete_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    reopen_parser = reminder_commands.add_parser(
        "reopen",
        help="Reopen a completed reminder.",
    )
    reopen_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    reopen_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    delete_parser = reminder_commands.add_parser(
        "delete",
        help="Delete a reminder by ID.",
    )
    delete_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    delete_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    notify_due_parser = reminder_commands.add_parser(
        "notify-due",
        help="Deliver local desktop notifications for due reminders.",
    )
    notify_due_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum reminders to notify per run (default: 20).",
    )
    notify_due_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which reminders would be notified without delivering or writing.",
    )
    notify_due_parser.add_argument(
        "--backend",
        choices=("auto", "desktop", "stdout"),
        default="auto",
        help="Notification backend (default: auto).",
    )
    notify_due_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    notify_history_parser = reminder_commands.add_parser(
        "notify-history",
        help="Show notification delivery history.",
    )
    notify_history_parser.add_argument(
        "--reminder",
        type=int,
        dest="notify_reminder_id",
        default=None,
        help="Only show attempts for this reminder.",
    )
    notify_history_parser.add_argument(
        "--failed",
        action="store_true",
        help="Only show failed attempts.",
    )
    notify_history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum attempts to show (default: 20).",
    )
    notify_history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    saved_search_parser = subparsers.add_parser(
        "saved-search",
        help="Manage reusable search/rank workflow definitions.",
    )
    saved_search_commands = saved_search_parser.add_subparsers(
        dest="saved_search_command",
        required=True,
    )

    create_parser = saved_search_commands.add_parser(
        "create",
        help="Create a saved search or ranking workflow.",
    )
    create_parser.add_argument("name", help="Unique workflow name.")
    create_parser.add_argument("query", help="Search keyword or query.")
    create_parser.add_argument(
        "--mode",
        choices=SEARCH_MODES,
        required=True,
        help="Workflow mode (search or rank).",
    )
    _add_saved_search_filter_flags(create_parser)

    list_parser = saved_search_commands.add_parser(
        "list",
        help="List saved workflows.",
    )
    list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = saved_search_commands.add_parser(
        "show",
        help="Show a saved workflow definition.",
    )
    show_parser.add_argument("name", help="Workflow name.")
    show_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    run_parser = saved_search_commands.add_parser(
        "run",
        help="Execute a saved workflow against stored jobs.",
    )
    run_parser.add_argument("name", help="Workflow name.")
    run_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    update_parser = saved_search_commands.add_parser(
        "update",
        help="Change a saved workflow definition.",
    )
    update_parser.add_argument("name", help="Workflow name.")
    update_parser.add_argument(
        "--query",
        help="Replace the search keyword or query.",
    )
    update_parser.add_argument(
        "--mode",
        choices=SEARCH_MODES,
        help="Workflow mode (immutable after creation; changing is rejected).",
    )
    _add_saved_search_filter_flags(update_parser)
    update_parser.add_argument(
        "--clear-source",
        action="store_true",
        help="Clear the source filter.",
    )
    update_parser.add_argument(
        "--clear-location",
        action="store_true",
        help="Clear the location filter.",
    )
    update_parser.add_argument(
        "--clear-remote",
        action="store_true",
        help="Clear the remote filter.",
    )
    update_parser.add_argument(
        "--clear-min-score",
        action="store_true",
        help="Clear the minimum-score filter.",
    )
    update_parser.add_argument(
        "--clear-profile",
        action="store_true",
        help="Clear the rank profile path.",
    )
    update_parser.add_argument(
        "--clear-status-filters",
        action="store_true",
        help="Clear both --status and --exclude-status filters.",
    )

    rename_parser = saved_search_commands.add_parser(
        "rename",
        help="Rename a saved workflow.",
    )
    rename_parser.add_argument("name", help="Current workflow name.")
    rename_parser.add_argument("new_name", help="New workflow name.")
    rename_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    delete_parser = saved_search_commands.add_parser(
        "delete",
        help="Delete a saved workflow.",
    )
    delete_parser.add_argument("name", help="Workflow name.")
    delete_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    return parser


def _add_saved_search_filter_flags(parser: argparse.ArgumentParser) -> None:
    """Add the shared filter flags used by saved-search create and update."""
    parser.add_argument(
        "--grouped",
        action="store_true",
        dest="grouped_flag",
        help="Collapse cross-source duplicates into one result per group.",
    )
    parser.add_argument(
        "--no-grouped",
        action="store_true",
        dest="no_grouped_flag",
        help="Do not collapse cross-source duplicates.",
    )
    parser.add_argument(
        "--profile",
        help="Candidate profile JSON for rank mode (required in rank mode).",
    )
    parser.add_argument(
        "--source",
        help="Only include results from this source.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Only include remote jobs.",
    )
    parser.add_argument(
        "--location",
        help="Only include results matching this location.",
    )
    parser.add_argument(
        "--untracked",
        action="store_true",
        dest="untracked_flag",
        help="Only include jobs with no application record.",
    )
    parser.add_argument(
        "--no-untracked",
        action="store_true",
        dest="no_untracked_flag",
        help="Do not restrict to untracked jobs.",
    )
    parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        dest="status_list",
        help="Only show jobs with this application status (repeatable).",
    )
    parser.add_argument(
        "--exclude-status",
        action="append",
        choices=APPLICATION_STATUSES,
        dest="exclude_status_list",
        help="Hide jobs with this application status (repeatable).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        help="Only show results with a match score of at least N (0-100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum results.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )


def _lock_conflict_message(error: LockConflict) -> str:
    """Format a concise lock-contention message for stderr."""
    lines = [
        f"{error.lock_name.capitalize()} already running for database:",
        str(error.database_path),
        f"Lock file: {error.lock_path}",
    ]
    owner: list[str] = []
    if error.metadata.get("pid"):
        owner.append(f"pid={error.metadata['pid']}")
    if error.metadata.get("command"):
        owner.append(f"command={error.metadata['command']!r}")
    if error.metadata.get("acquired_at"):
        owner.append(f"acquired_at={error.metadata['acquired_at']}")
    if owner:
        lines.append("Owner: " + ", ".join(owner))
    return "\n".join(lines)


def _validate_application_filter_flags(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> None:
    """Reject contradictory combinations of tracking filter flags."""
    include_statuses = arguments.status or ()
    exclude_statuses = arguments.exclude_status or ()
    if arguments.untracked and (include_statuses or exclude_statuses):
        parser.error("--untracked cannot be combined with --status or --exclude-status")
    if include_statuses and exclude_statuses:
        parser.error("--status and --exclude-status cannot be combined")


def _tri_state(
    parser: argparse.ArgumentParser,
    *,
    positive: bool,
    negative: bool,
    name: str,
) -> bool | None:
    """Resolve a --flag/--no-flag pair to True/False/None (unspecified)."""
    if positive and negative:
        parser.error(f"--{name} and --no-{name} cannot be combined")
    if positive:
        return True
    if negative:
        return False
    return None


def _build_creation_fields(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> SavedSearchFields:
    """Validate create flags and build a fresh saved-search definition."""
    grouped = _tri_state(
        parser,
        positive=arguments.grouped_flag,
        negative=arguments.no_grouped_flag,
        name="grouped",
    )
    untracked = _tri_state(
        parser,
        positive=arguments.untracked_flag,
        negative=arguments.no_untracked_flag,
        name="untracked",
    )
    if grouped is None:
        grouped = False
    if untracked is None:
        untracked = False

    include_statuses = arguments.status_list or ()
    exclude_statuses = arguments.exclude_status_list or ()
    if include_statuses and exclude_statuses:
        parser.error("--status and --exclude-status cannot be combined")
    if untracked and (include_statuses or exclude_statuses):
        parser.error("--untracked cannot be combined with --status or --exclude-status")

    mode = arguments.mode
    if mode == "search":
        if arguments.profile is not None:
            parser.error("--profile is only valid in rank mode")
        if arguments.min_score is not None:
            parser.error("--min-score is only valid in rank mode")
    else:
        if arguments.profile is None:
            parser.error("rank mode requires --profile")

    if arguments.min_score is not None and not (0 <= arguments.min_score <= 100):
        parser.error("--min-score must be between 0 and 100")
    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be at least 1")

    return SavedSearchFields(
        name=arguments.name,
        query=arguments.query,
        mode=mode,
        profile_path=arguments.profile,
        grouped=grouped,
        source=arguments.source,
        remote=True if arguments.remote else None,
        location=arguments.location,
        untracked=untracked,
        include_statuses=tuple(include_statuses),
        exclude_statuses=tuple(exclude_statuses),
        min_score=arguments.min_score,
        result_limit=arguments.limit if arguments.limit is not None else 20,
    )


def _build_update_patch(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    *,
    mode: str,
) -> tuple[SavedSearchPatch, frozenset[str]]:
    """Validate update arguments and build a patch plus explicit clears."""
    grouped = _tri_state(
        parser,
        positive=arguments.grouped_flag,
        negative=arguments.no_grouped_flag,
        name="grouped",
    )
    untracked = _tri_state(
        parser,
        positive=arguments.untracked_flag,
        negative=arguments.no_untracked_flag,
        name="untracked",
    )

    include_statuses = arguments.status_list or ()
    exclude_statuses = arguments.exclude_status_list or ()
    if arguments.clear_status_filters and (include_statuses or exclude_statuses):
        parser.error(
            "--clear-status-filters cannot be combined with "
            "--status or --exclude-status"
        )
    if untracked is True and (include_statuses or exclude_statuses):
        parser.error("--untracked cannot be combined with --status or --exclude-status")
    if arguments.clear_status_filters and untracked is True:
        parser.error("--clear-status-filters cannot be combined with --untracked")
    if include_statuses and exclude_statuses:
        parser.error("--status and --exclude-status cannot be combined")

    if mode == "search":
        if arguments.profile is not None:
            parser.error("--profile is only valid in rank mode")
        if arguments.min_score is not None:
            parser.error("--min-score is only valid in rank mode")
    if arguments.min_score is not None and not (0 <= arguments.min_score <= 100):
        parser.error("--min-score must be between 0 and 100")
    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be at least 1")

    _reject_clear_pairs(parser, arguments)

    clears: set[str] = set()
    if arguments.clear_source:
        clears.add("source")
    if arguments.clear_location:
        clears.add("location")
    if arguments.clear_remote:
        clears.add("remote")
    if arguments.clear_min_score:
        clears.add("min_score")
    if arguments.clear_profile:
        clears.add("profile_path")
    if arguments.clear_status_filters:
        clears.add("status_filters")

    return (
        SavedSearchPatch(
            query=arguments.query,
            profile_path=arguments.profile,
            grouped=grouped,
            source=arguments.source,
            remote=True if arguments.remote else None,
            location=arguments.location,
            untracked=untracked,
            include_statuses=tuple(include_statuses) if include_statuses else None,
            exclude_statuses=tuple(exclude_statuses) if exclude_statuses else None,
            min_score=arguments.min_score,
            result_limit=arguments.limit,
        ),
        frozenset(clears),
    )


def _reject_clear_pairs(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> None:
    """Reject setting a filter while simultaneously clearing it."""
    pairs = (
        (arguments.clear_source, arguments.source, "--source"),
        (arguments.clear_location, arguments.location, "--location"),
        (arguments.clear_min_score, arguments.min_score, "--min-score"),
        (arguments.clear_profile, arguments.profile, "--profile"),
        (arguments.clear_remote, arguments.remote, "--remote"),
    )
    for cleared, set_value, flag in pairs:
        if cleared and set_value is not None:
            parser.error(f"{flag} and --clear cannot be combined")


def _run_saved_search_list(arguments: argparse.Namespace) -> int:
    try:
        with connect(arguments.database) as connection:
            initialize_schema(connection)
            searches = list_searches(connection)
    except sqlite3.Error as error:
        print(
            f"Failed to list saved searches: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    if not searches:
        print("No saved searches.")
        return 0
    print(f"Saved searches ({len(searches)}):")
    for search in searches:
        print(f"  {search.name}  (mode={search.mode})")
    return 0


def _run_saved_search_show(arguments: argparse.Namespace) -> int:
    try:
        with connect(arguments.database) as connection:
            initialize_schema(connection)
            search = get_search(connection, arguments.name)
    except sqlite3.Error as error:
        print(
            f"Failed to show saved search: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    if search is None:
        print(f"No saved search named {arguments.name!r}.", file=sys.stderr)
        return 1

    print(f"Saved search: {search.name}")
    print(f"  Mode:         {search.mode}")
    print(f"  Query:        {search.query}")
    print(f"  Grouped:      {'yes' if search.grouped else 'no'}")
    print(f"  Source:       {search.source or 'any'}")
    print(f"  Remote:       {'remote only' if search.remote else 'any'}")
    print(f"  Location:     {search.location or 'any'}")
    print(f"  Untracked:    {'yes' if search.untracked else 'no'}")
    if search.include_statuses:
        print(f"  Status:       {', '.join(search.include_statuses)}")
    if search.exclude_statuses:
        print(f"  Excluded:     {', '.join(search.exclude_statuses)}")
    print(f"  Result limit: {search.result_limit}")
    if search.mode == "rank":
        min_score = search.min_score if search.min_score is not None else "none"
        print(f"  Min score:    {min_score}")
        print(f"  Profile:      {search.profile_path or 'none'}")
    print(f"  Created:      {search.created_at}")
    print(f"  Updated:      {search.updated_at}")
    return 0


def _run_saved_search_create(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    fields = _build_creation_fields(parser, arguments)
    try:
        with collection_lock(arguments.database, command="saved-search create"):
            with connect(arguments.database) as connection:
                initialize_schema(connection)
                created = create_search(connection, fields=fields)
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except OSError as error:
        print(
            f"Failed to create saved search: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except (DuplicateNameError, ValidationError, sqlite3.Error) as error:
        print(f"Failed to create saved search: {error}", file=sys.stderr)
        return 1

    print(f"Created saved search {created.name!r} (mode={created.mode}).")
    return 0


def _run_saved_search_update(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    if arguments.mode is not None:
        parser.error("--mode is immutable; it cannot be changed after creation")

    try:
        with collection_lock(arguments.database, command="saved-search update"):
            with connect(arguments.database) as connection:
                initialize_schema(connection)
                stored = get_search(connection, arguments.name)
                if stored is None:
                    print(f"No saved search named {arguments.name!r}.", file=sys.stderr)
                    return 1
                patch, clears = _build_update_patch(parser, arguments, mode=stored.mode)
                updated, changed = update_search(
                    connection,
                    name=arguments.name,
                    patch=patch,
                    clears=clears,
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except OSError as error:
        print(
            f"Failed to update saved search: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except (ValidationError, sqlite3.Error) as error:
        print(f"Failed to update saved search: {error}", file=sys.stderr)
        return 1

    if not changed:
        print(f"No changes to saved search {stored.name!r}.")
        return 0
    print(f"Updated saved search {updated.name!r}.")
    return 0


def _run_saved_search_rename(arguments: argparse.Namespace) -> int:
    try:
        with collection_lock(arguments.database, command="saved-search rename"):
            with connect(arguments.database) as connection:
                initialize_schema(connection)
                renamed = rename_search(
                    connection,
                    name=arguments.name,
                    new_name=arguments.new_name,
                )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except OSError as error:
        print(
            f"Failed to rename saved search: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except (DuplicateNameError, UnknownSearchError, sqlite3.Error) as error:
        print(f"Failed to rename saved search: {error}", file=sys.stderr)
        return 1

    print(f"Renamed saved search {arguments.name!r} to {renamed.name!r}.")
    return 0


def _run_saved_search_delete(arguments: argparse.Namespace) -> int:
    try:
        with collection_lock(arguments.database, command="saved-search delete"):
            with connect(arguments.database) as connection:
                initialize_schema(connection)
                removed = delete_search(connection, name=arguments.name)
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except OSError as error:
        print(
            f"Failed to delete saved search: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    except (UnknownSearchError, sqlite3.Error) as error:
        print(f"Failed to delete saved search: {error}", file=sys.stderr)
        return 1

    print(f"Deleted saved search {removed.name!r}.")
    return 0


def _run_saved_search_run(arguments: argparse.Namespace) -> int:
    try:
        with connect(arguments.database) as connection:
            initialize_schema(connection)
            search = get_search(connection, arguments.name)
            if search is None:
                print(f"No saved search named {arguments.name!r}.", file=sys.stderr)
                return 1
            if search.mode == "rank":
                if not search.profile_path:
                    print(
                        f"Rank saved search {search.name!r} has no profile path.",
                        file=sys.stderr,
                    )
                    return 1
                profile = load_profile(Path(search.profile_path))
                rank_outcome = execute_rank(connection, search, profile=profile)
            else:
                search_outcome = execute_search(connection, search)
    except FileNotFoundError as error:
        print(f"Failed to run saved search: {error}", file=sys.stderr)
        return 1
    except (ValueError, json.JSONDecodeError) as error:
        print(f"Failed to run saved search: {error}", file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        print(
            f"Failed to run saved search: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    if search.mode == "search":
        if search_outcome.grouped:
            _render_search_grouped(
                search_outcome.groups, search_outcome.tracking_statuses
            )
        else:
            _render_search_flat(search_outcome.flat, search_outcome.tracking_statuses)
        return 0

    min_score = search.min_score or 0
    if rank_outcome.grouped:
        _render_rank_grouped(
            rank_outcome.grouped_ranked,
            rank_outcome.tracking_statuses,
            min_score=min_score,
            candidate_count=rank_outcome.grouped_candidate_count,
        )
    else:
        _render_rank_flat(
            rank_outcome.flat_ranked,
            rank_outcome.tracking_statuses,
            min_score=min_score,
            candidate_count=rank_outcome.flat_candidate_count,
        )
    return 0


def _run_saved_search_readish(
    command: str,
    arguments: argparse.Namespace,
) -> int:
    if command == "list":
        return _run_saved_search_list(arguments)
    if command == "show":
        return _run_saved_search_show(arguments)
    return _run_saved_search_run(arguments)


def _run_saved_search_write(
    command: str,
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    if command == "create":
        return _run_saved_search_create(arguments, parser)
    if command == "update":
        return _run_saved_search_update(arguments, parser)
    if command == "rename":
        return _run_saved_search_rename(arguments)
    return _run_saved_search_delete(arguments)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CareerOS command-line interface."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "diagnose":
        return print_diagnostics()

    if arguments.command == "collect":
        collect_command = arguments.collect_command

        if collect_command == "status":
            return run_collect_status(database_path=arguments.database)

        if collect_command == "history":
            if arguments.limit < 1:
                parser.error("--limit must be at least 1")
            return run_collect_history(
                source=arguments.source,
                limit=arguments.limit,
                failed=arguments.failed,
                database_path=arguments.database,
            )

        if arguments.limit < 1:
            parser.error("--limit must be at least 1")

        if collect_command == "due":
            lock_command = "collect due"
        elif collect_command == "all":
            lock_command = "collect all"
        else:
            lock_command = f"collect {collect_command}"

        try:
            with collection_lock(arguments.database, command=lock_command):
                if collect_command == "due":
                    return run_collect_due(
                        limit=arguments.limit,
                        database_path=arguments.database,
                    )

                if collect_command == "all":
                    return run_collect_all(
                        limit=arguments.limit,
                        database_path=arguments.database,
                    )

                return run_collect(
                    source=collect_command,
                    limit=arguments.limit,
                    database_path=arguments.database,
                )
        except CollectionLockConflict as error:
            print(_lock_conflict_message(error), file=sys.stderr)
            return EXIT_TEMP_FAILURE
        except OSError as error:
            message = f"Collection failed: {type(error).__name__}: {error}"
            print(message, file=sys.stderr)
            return 1

    if arguments.command == "application":
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

    if arguments.command == "search":
        if arguments.limit < 1:
            parser.error("--limit must be at least 1")
        _validate_application_filter_flags(parser, arguments)

        return run_search(
            keyword=arguments.keyword,
            database_path=arguments.database,
            remote=arguments.remote,
            location=arguments.location,
            source=arguments.source,
            limit=arguments.limit,
            grouped=arguments.grouped,
            untracked=arguments.untracked,
            include_statuses=arguments.status or (),
            exclude_statuses=arguments.exclude_status or (),
        )

    if arguments.command == "show":
        if arguments.job_id < 1:
            parser.error("job-id must be a positive integer")

        return run_show(
            job_id=arguments.job_id,
            database_path=arguments.database,
        )

    if arguments.command == "rank":
        if arguments.limit < 1:
            parser.error("--limit must be at least 1")
        if arguments.min_score < 0 or arguments.min_score > 100:
            parser.error("--min-score must be between 0 and 100")
        _validate_application_filter_flags(parser, arguments)

        return run_rank(
            keyword=arguments.keyword,
            profile_path=arguments.profile,
            database_path=arguments.database,
            remote=arguments.remote,
            location=arguments.location,
            source=arguments.source,
            limit=arguments.limit,
            grouped=arguments.grouped,
            min_score=arguments.min_score,
            untracked=arguments.untracked,
            include_statuses=arguments.status or (),
            exclude_statuses=arguments.exclude_status or (),
        )

    if arguments.command == "profile":
        if arguments.profile_command == "init":
            return run_profile_init(arguments.path, force=arguments.force)
        if arguments.profile_command == "show":
            return run_profile_show(arguments.path)
        return run_profile_validate(arguments.path)

    if arguments.command == "report":
        return run_report(arguments, parser)

    if arguments.command == "group":
        if arguments.group_command == "regroup":
            try:
                with collection_lock(arguments.database, command="group regroup"):
                    return run_group_regroup(database_path=arguments.database)
            except CollectionLockConflict as error:
                print(_lock_conflict_message(error), file=sys.stderr)
                return EXIT_TEMP_FAILURE
            except OSError as error:
                message = f"Regroup failed: {type(error).__name__}: {error}"
                print(message, file=sys.stderr)
                return 1

        if arguments.limit < 1:
            parser.error("--limit must be at least 1")
        return run_group_explain(
            database_path=arguments.database,
            group_id=arguments.group_id,
            limit=arguments.limit,
        )

    if arguments.command == "saved-search":
        saved_command = arguments.saved_search_command

        if saved_command in ("list", "show", "run"):
            return _run_saved_search_readish(saved_command, arguments)

        return _run_saved_search_write(saved_command, arguments, parser)

    if arguments.command == "reminder":
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

    parser.error("Unsupported command.")
    return 2
