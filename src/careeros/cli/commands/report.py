"""The ``report`` command (read-only Career Snapshot)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from careeros.cli import common
from careeros.cli.common import load_profile_or_error
from careeros.database.connection import connect_read_only
from careeros.ranking.scorer import CandidateProfile
from careeros.reporting.models import Report
from careeros.reporting.render import render_report, render_report_json
from careeros.reporting.service import build_report
from careeros.review.feedback import build_feedback_set
from careeros.saved_searches.models import SavedSearch
from careeros.saved_searches.service import get_search


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
        with connect_read_only(arguments.database) as connection:
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
                profile = load_profile_or_error(arguments.profile)
                if profile is None:
                    return 1
            elif saved_search is not None:
                if not saved_search.profile_path:
                    print(
                        f"Rank saved search {saved_search.name!r} has no profile path.",
                        file=sys.stderr,
                    )
                    return 1
                profile = load_profile_or_error(Path(saved_search.profile_path))
                if profile is None:
                    return 1

            report = build_report(
                connection,
                now=common._utc_now(),
                profile=profile,
                saved_search=saved_search,
                limit=arguments.limit,
                grouped=arguments.grouped,
                feedback=(
                    build_feedback_set(connection)
                    if not arguments.no_feedback
                    else None
                ),
                use_adaptive=not arguments.no_adaptive,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Report failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    assert report is not None
    if arguments.format == "json":
        print(render_report_json(report), end="")
    else:
        print(render_report(report), end="")
    return 0


def run_report_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch the ``report`` command."""
    return run_report(arguments, parser)
