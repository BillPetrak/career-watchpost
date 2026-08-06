"""The ``saved-search`` command family (reusable workflows)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from careeros.cli.common import EXIT_TEMP_FAILURE, _lock_conflict_message
from careeros.cli.render import (
    _render_rank_flat,
    _render_rank_grouped,
    _render_search_flat,
    _render_search_grouped,
)
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.ranking.profile import load_profile
from careeros.review.feedback import build_feedback_set
from careeros.saved_searches.execution import (
    execute_rank,
    execute_search,
)
from careeros.saved_searches.models import (
    DuplicateNameError,
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
                rank_outcome = execute_rank(
                    connection,
                    search,
                    profile=profile,
                    feedback=build_feedback_set(connection),
                )
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


def run_saved_search_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch a ``saved-search`` subcommand."""
    saved_command = arguments.saved_search_command

    if saved_command in ("list", "show", "run"):
        return _run_saved_search_readish(saved_command, arguments)

    return _run_saved_search_write(saved_command, arguments, parser)
