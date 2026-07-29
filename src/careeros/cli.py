"""Command-line interface for CareerOS."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from careeros.collectors.arbeitnow import fetch_jobs
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
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

    parser.error("Unsupported command.")
    return 2
