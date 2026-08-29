"""The ``source`` command (per-source statistics)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from careeros.database.connection import connect_read_only
from careeros.source_stats.render import (
    render_source_stats_json,
    render_source_stats_text,
)
from careeros.source_stats.service import compute_source_stats, empty_source_stats


def run_source_stats(
    *,
    source: str | None,
    database_path: Path,
    output_format: str,
) -> int:
    """Show unified per-source statistics (read-only, no lock, no writes).

    A missing database file is reported as an empty database without being
    created; an existing database is only ever read.
    """
    try:
        if database_path.exists():
            with connect_read_only(database_path) as connection:
                stats = compute_source_stats(connection)
        else:
            stats = empty_source_stats()
    except (OSError, sqlite3.Error) as error:
        print(f"Source stats failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if source is not None:
        stats = tuple(entry for entry in stats if entry.source == source)

    if output_format == "json":
        print(render_source_stats_json(stats), end="")
    else:
        print(render_source_stats_text(stats), end="")
    return 0


def run_source_command(arguments: argparse.Namespace) -> int:
    """Dispatch the ``source`` command."""
    return run_source_stats(
        source=arguments.source,
        database_path=arguments.database,
        output_format=arguments.format,
    )
