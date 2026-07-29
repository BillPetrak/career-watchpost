"""Command-line interface for CareerOS."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from careeros.collectors.arbeitnow import fetch_jobs
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs, search_jobs
from careeros.database.schema import initialize_schema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "careeros.sqlite3"


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


def collect_arbeitnow(limit: int, database_path: Path) -> int:
    """Collect Arbeitnow jobs and save them to SQLite."""
    print(f"Collecting up to {limit} jobs from Arbeitnow...")

    try:
        jobs = fetch_jobs(limit=limit)

        with connect(database_path) as connection:
            initialize_schema(connection)
            result = save_jobs(connection, jobs)
    except Exception as error:
        print(f"Collection failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print()
    print("Collection complete")
    print("-------------------")
    print(f"Fetched:  {len(jobs)}")
    print(f"Inserted: {result.inserted}")
    print(f"Updated:  {result.updated}")
    print(f"Database: {database_path}")

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
    )
    collect_parser.add_argument(
        "source",
        choices=["arbeitnow"],
        help="Job source to collect.",
    )
    collect_parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maximum jobs to collect (default: 25).",
    )
    collect_parser.add_argument(
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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CareerOS command-line interface."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "diagnose":
        return print_diagnostics()

    if arguments.command == "collect" and arguments.source == "arbeitnow":
        if arguments.limit < 1:
            parser.error("--limit must be at least 1")

        return collect_arbeitnow(
            limit=arguments.limit,
            database_path=arguments.database,
        )

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

    parser.error("Unsupported command.")
    return 2
