"""The ``search`` command."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from careeros.cli.common import _validate_application_filter_flags
from careeros.cli.render import _render_search_flat, _render_search_grouped
from careeros.database.connection import connect_read_only
from careeros.saved_searches.execution import search_candidates


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
        with connect_read_only(database_path) as connection:
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
    except FileNotFoundError:
        outcome = None
    except (OSError, sqlite3.Error) as error:
        print(f"Search failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if outcome is None:
        return 0
    if outcome.grouped:
        _render_search_grouped(outcome.groups, outcome.tracking_statuses)
    else:
        _render_search_flat(outcome.flat, outcome.tracking_statuses)

    return 0


def run_search_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch the ``search`` command."""
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
