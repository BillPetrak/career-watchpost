"""The ``collect`` command family and its collection-lock wrapper."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

from careeros.cli import common
from careeros.cli.common import (
    _SOURCE_LABELS,
    EXIT_TEMP_FAILURE,
    _lock_conflict_message,
)
from careeros.collecting import (
    CollectionResult,
    collect_all,
    collect_due,
    collect_one,
)
from careeros.collection_policies import SOURCE_POLICIES
from careeros.collectors import COLLECTORS, configured_collectors
from careeros.database.connection import connect, connect_read_only
from careeros.database.runs import CollectionRun, list_runs, parse_utc
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.source_config import (
    LeverConfig,
    SourceConfig,
    SourceConfigError,
    load_source_config,
)
from careeros.sources import CONFIGURABLE_SOURCES, resolve_source_catalog
from careeros.status import build_statuses


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


def run_collect(
    source: str,
    limit: int,
    database_path: Path,
    *,
    config: SourceConfig | None = None,
) -> int:
    """Collect jobs from a registered source and save them to SQLite."""
    label = _SOURCE_LABELS.get(source, source)
    print(f"Collecting up to {limit} jobs from {label}...")

    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            result = collect_one(
                connection,
                source=source,
                collectors=(
                    configured_collectors(config) if config is not None else COLLECTORS
                ),
                limit=limit,
            )
    except (OSError, sqlite3.Error, ValueError) as error:
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


def run_collect_all(limit: int, database_path: Path, *, config: SourceConfig) -> int:
    """Collect from every registered source sequentially and print summaries."""
    results: list[CollectionResult] = []

    try:
        catalog = resolve_source_catalog(config=config)
        collectors = configured_collectors(config)
        for result in collect_all(
            limit=limit,
            database_path=database_path,
            collectors=collectors,
            order=catalog.active,
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
    except (OSError, sqlite3.Error, ValueError) as error:
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


def run_collect_due(
    limit: int,
    database_path: Path,
    *,
    config: SourceConfig | None = None,
) -> int:
    """Collect only sources whose polling interval has expired."""
    results: list[CollectionResult] = []

    try:
        if config is None:
            catalog = resolve_source_catalog(
                config=SourceConfig(
                    version=1,
                    lever=LeverConfig(present=False, enabled=False, boards=()),
                )
            )
            collectors = COLLECTORS
            order = tuple(COLLECTORS)
        else:
            catalog = resolve_source_catalog(config=config)
            collectors = configured_collectors(config)
            order = catalog.active
        for result in collect_due(
            limit=limit,
            database_path=database_path,
            collectors=collectors,
            policies=SOURCE_POLICIES,
            order=order,
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
    except (OSError, sqlite3.Error, ValueError) as error:
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


def run_collect_status(database_path: Path, sources_config: Path | None = None) -> int:
    """Show polling status for every registered source without mutating state."""
    now = common._utc_now()
    try:
        with connect_read_only(database_path) as connection:
            statuses = build_statuses(
                connection,
                now=now,
                config_path=str(sources_config) if sources_config else None,
            )
    except (OSError, sqlite3.Error, ValueError) as error:
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
        if status.source in CONFIGURABLE_SOURCES:
            print(f"  Configuration:  {status.configuration}")
        print(
            "  Next due:       "
            + (
                status.next_due.isoformat()
                if status.next_due is not None
                else ("-" if status.state != "active" else "now")
            )
        )
        display_state = (
            status.state
            if status.source in CONFIGURABLE_SOURCES and status.state != "active"
            else ("due" if status.due else "not due")
        )
        print(f"  State:          {display_state}")
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
        with connect_read_only(database_path) as connection:
            runs = list_runs(
                connection,
                source=source,
                limit=limit,
                failed_only=failed,
            )
    except (OSError, sqlite3.Error, ValueError) as error:
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


def run_collect_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch a ``collect`` subcommand, acquiring the collection lock."""
    collect_command = arguments.collect_command

    if collect_command == "status":
        return run_collect_status(
            database_path=arguments.database,
            sources_config=getattr(arguments, "sources_config", None),
        )

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

    config = None
    if collect_command in {"all", "due", *CONFIGURABLE_SOURCES}:
        try:
            config = load_source_config(
                str(arguments.sources_config)
                if getattr(arguments, "sources_config", None) is not None
                else None
            )
        except SourceConfigError as error:
            print(f"Collection failed: {error}", file=sys.stderr)
            return 1
        if (
            collect_command in CONFIGURABLE_SOURCES
            and not getattr(config, collect_command).enabled
        ):
            print(
                f"Collection failed: {collect_command.capitalize()} is not enabled "
                "in source configuration",
                file=sys.stderr,
            )
            return 1

    if collect_command == "due":
        lock_command = "collect due"
    elif collect_command == "all":
        lock_command = "collect all"
    else:
        lock_command = f"collect {collect_command}"

    try:
        with collection_lock(arguments.database, command=lock_command):
            if collect_command == "due":
                assert config is not None
                return run_collect_due(
                    limit=arguments.limit,
                    database_path=arguments.database,
                    config=config,
                )

            if collect_command == "all":
                assert config is not None
                return run_collect_all(
                    limit=arguments.limit,
                    database_path=arguments.database,
                    config=config,
                )

            return run_collect(
                source=collect_command,
                limit=arguments.limit,
                database_path=arguments.database,
                config=config,
            )
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except OSError as error:
        message = f"Collection failed: {type(error).__name__}: {error}"
        print(message, file=sys.stderr)
        return 1
