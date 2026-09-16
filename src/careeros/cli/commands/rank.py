"""The ``rank`` command."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from careeros.cli.common import (
    _validate_application_filter_flags,
    load_profile_or_error,
)
from careeros.cli.render import (
    _render_rank_flat,
    _render_rank_grouped,
    render_rank_json,
)
from careeros.database.connection import connect_read_only
from careeros.review.feedback import build_feedback_set
from careeros.saved_searches.execution import rank_candidates


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
    output_format: str = "text",
    use_feedback: bool = True,
    use_adaptive: bool = True,
) -> int:
    """Search jobs and rank them against a candidate profile."""
    profile = load_profile_or_error(profile_path)
    if profile is None:
        return 1

    try:
        with connect_read_only(database_path) as connection:
            feedback = build_feedback_set(connection) if use_feedback else None
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
                feedback=feedback,
                use_adaptive=use_adaptive,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Search failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if output_format == "json":
        print(render_rank_json(outcome), end="")
        return 0

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


def run_rank_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch the ``rank`` command."""
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
        output_format=arguments.format,
        use_feedback=not arguments.no_feedback,
        use_adaptive=not arguments.no_adaptive,
    )
