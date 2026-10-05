"""The ``group`` command family (canonical duplicate groups)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from careeros.cli.common import EXIT_TEMP_FAILURE, _lock_conflict_message
from careeros.collecting import refresh_opportunity_authority
from careeros.database.connection import connect, connect_read_only
from careeros.database.groups import (
    ensure_grouped,
    grouping_summary,
    list_group_infos,
)
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.preferences.service import (
    effective_learned_authority_fingerprint,
    refresh_learned_authority,
)


def run_group_regroup(database_path: Path) -> int:
    """Recompute canonical duplicate groups and persist them."""
    try:
        with connect(database_path) as connection:
            initialize_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous_learned_fingerprint = effective_learned_authority_fingerprint(
                    connection
                )
                ensure_grouped(connection, commit=False)
                refresh_opportunity_authority(connection)
                refresh_learned_authority(
                    connection,
                    previous_fingerprint=previous_learned_fingerprint,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            summary = grouping_summary(connection)
    except (OSError, sqlite3.Error) as error:
        print(f"Regroup failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Regrouping complete")
    print("-------------------")
    print(f"Total jobs:      {summary.total_jobs}")
    print(f"Groups:          {summary.groups}")
    print(f"Grouped members: {summary.grouped_jobs}")

    return 0


def run_group_explain(
    database_path: Path,
    *,
    group_id: str | None,
    limit: int,
) -> int:
    """Show canonical groups (with members) without mutating state."""
    try:
        with connect_read_only(database_path) as connection:
            infos = list_group_infos(
                connection,
                group_id=group_id,
                limit=limit,
            )
    except (OSError, sqlite3.Error) as error:
        print(f"Explain failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print("Canonical groups")
    print("----------------")

    if not infos:
        print()
        print("No groups found.")
        return 0

    for info in infos:
        print()
        print(f"Group {info.group_id}")
        print(f"  Reason:    {info.reason}")
        print(f"  Sources:   {', '.join(str(s) for s in info.sources)}")
        print(f"  Created:   {info.created_at}")
        print("  Members:")
        for member in info.members:
            print(f"    - [{member.id}] {member.source}: {member.title}")

    return 0


def run_group_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Dispatch a ``group`` subcommand, acquiring the collection lock."""
    if arguments.group_command == "regroup":
        try:
            with collection_lock(arguments.database, command="group regroup"):
                return run_group_regroup(database_path=arguments.database)
        except CollectionLockConflict as error:
            print(_lock_conflict_message(error), file=sys.stderr)
            return EXIT_TEMP_FAILURE
        except OSError as error:
            message = f"Regroup failed: {type(error).__name__}: {error}"
            print(message, file=sys.stderr)
            return 1

    if arguments.limit < 1:
        parser.error("--limit must be at least 1")
    return run_group_explain(
        database_path=arguments.database,
        group_id=arguments.group_id,
        limit=arguments.limit,
    )
