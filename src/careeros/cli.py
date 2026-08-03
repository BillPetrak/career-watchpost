"""Command-line interface for CareerOS."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from careeros.collecting import (
    CollectionResult,
    collect_all,
    collect_due,
    collect_one,
)
from careeros.collectors import COLLECTORS
from careeros.database.connection import connect
from careeros.database.jobs import get_job_by_id, search_jobs
from careeros.database.runs import CollectionRun, list_runs, parse_utc
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.ranking.profile import load_profile
from careeros.ranking.scorer import score_jobs
from careeros.status import build_statuses

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "careeros.sqlite3"

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


def run_search(
    keyword: str,
    database_path: Path,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    limit: int = 20,
) -> int:
    """Search jobs in the SQLite database."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            results = search_jobs(
                connection,
                keyword,
                remote=remote,
                location=location,
                source=source,
                limit=limit,
            )
    except Exception as error:
        print(f"Search failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

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
        print()

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


class _DescriptionCleaner(HTMLParser):
    """Strips HTML tags, preserves paragraph/list boundaries, and unescapes entities."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._parts: list[str] = []
        self._pending_newline = False

    def _emit_newline(self) -> None:
        self._pending_newline = True

    def _flush_newline(self) -> None:
        if self._pending_newline:
            if not self._parts or self._parts[-1] != "\n":
                self._parts.append("\n")
            self._pending_newline = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._emit_newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in {
            "p",
            "div",
            "li",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "tr",
            "td",
            "th",
            "ul",
            "ol",
        }:
            self._emit_newline()

    def handle_data(self, data: str) -> None:
        self._flush_newline()
        cleaned = " ".join(data.split())
        if cleaned:
            if self._parts and self._parts[-1] != "\n":
                self._parts.append(" ")
            self._parts.append(cleaned)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n+", "\n", text)
        return text.strip()


def clean_description(html_text: str) -> str:
    """Strip HTML tags, preserve paragraph breaks, and unescape entities."""
    if not html_text:
        return ""
    text = html.unescape(html_text)
    cleaner = _DescriptionCleaner()
    cleaner.feed(text)
    return cleaner.get_text()


def run_rank(
    keyword: str,
    profile_path: Path,
    database_path: Path,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    limit: int = 20,
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
            matches = search_jobs(
                connection,
                keyword,
                remote=remote,
                location=location,
                source=source,
                limit=limit,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Search failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if not matches:
        print("No matching jobs found.")
        return 0

    ranked = score_jobs(matches, profile)

    print(f"Ranked {len(ranked)} job{'s' if len(ranked) != 1 else ''}:")
    print()

    for i, rj in enumerate(ranked, start=1):
        location_part = rj.job.location or "N/A"
        if rj.job.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        print(f"{i}. [Score {rj.score}] {rj.job.title}")
        print(f"   ID:       {rj.job.id}")
        print(f"   Company:  {rj.job.company or 'N/A'}")
        print(f"   Location: {location_part}")
        print(f"   Remote:   {rj.job.remote_status or 'N/A'}")
        print(f"   Source:   {rj.job.source}")
        print(f"   URL:      {rj.job.url or 'N/A'}")
        print("   Reasons:")
        for reason in rj.reasons:
            print(f"     {reason}")
        print()

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
        "--limit",
        type=int,
        default=20,
        help="Maximum results (default: 20).",
    )
    rank_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    return parser


def _lock_conflict_message(error: CollectionLockConflict) -> str:
    """Format a concise lock-contention message for stderr."""
    lines = [
        "Collection already running for database:",
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

    if arguments.command == "search":
        if arguments.limit < 1:
            parser.error("--limit must be at least 1")

        return run_search(
            keyword=arguments.keyword,
            database_path=arguments.database,
            remote=arguments.remote,
            location=arguments.location,
            source=arguments.source,
            limit=arguments.limit,
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

        return run_rank(
            keyword=arguments.keyword,
            profile_path=arguments.profile,
            database_path=arguments.database,
            remote=arguments.remote,
            location=arguments.location,
            source=arguments.source,
            limit=arguments.limit,
        )

    parser.error("Unsupported command.")
    return 2
