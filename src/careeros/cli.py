"""Command-line interface for CareerOS."""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from html.parser import HTMLParser
from pathlib import Path

from careeros.collectors import COLLECTORS
from careeros.database.connection import connect
from careeros.database.jobs import get_job_by_id, save_jobs, search_jobs
from careeros.database.schema import initialize_schema
from careeros.ranking.profile import load_profile
from careeros.ranking.scorer import score_jobs

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


_SOURCE_LABELS = {
    "arbeitnow": "Arbeitnow",
    "jobicy": "Jobicy",
    "remoteok": "Remote OK",
    "weworkremotely": "We Work Remotely",
}


def run_collect(source: str, limit: int, database_path: Path) -> int:
    """Collect jobs from a registered source and save them to SQLite."""
    fetch_jobs = COLLECTORS[source]
    label = _SOURCE_LABELS.get(source, source)
    print(f"Collecting up to {limit} jobs from {label}...")

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
    )
    collect_parser.add_argument(
        "source",
        choices=list(COLLECTORS),
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CareerOS command-line interface."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "diagnose":
        return print_diagnostics()

    if arguments.command == "collect":
        if arguments.limit < 1:
            parser.error("--limit must be at least 1")

        return run_collect(
            source=arguments.source,
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
