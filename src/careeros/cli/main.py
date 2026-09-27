# ruff: noqa: E501
"""CareerOS CLI entry point and command dispatch."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sequence

from careeros.cli.commands import (
    run_application_command,
    run_application_redact_note,
    run_career_profile_command,
    run_collect_command,
    run_export_command,
    run_group_command,
    run_preference_command,
    run_profile_command,
    run_rank_command,
    run_reminder_command,
    run_report_command,
    run_review_command,
    run_saved_search_command,
    run_search_command,
    run_show_command,
    run_source_command,
)
from careeros.cli.commands.diagnose import print_diagnostics
from careeros.cli.commands.materials import run_material_command
from careeros.cli.parser import build_parser
from careeros.database.admin import (
    backup as backup_database,
)
from careeros.database.admin import (
    migrate_database,
)
from careeros.database.admin import (
    restore as restore_database,
)
from careeros.server import serve
from careeros.systemd import run_command as run_systemd_command


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CareerOS command-line interface."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "diagnose":
        return print_diagnostics()

    if arguments.command == "systemd":
        return run_systemd_command(arguments)

    if arguments.command == "database":
        try:
            if arguments.database_command == "backup":
                backup_database(arguments.database, arguments.destination)
                print(f"SQLite backup created: {arguments.destination}")
                print(
                    "Managed material files were not included; preserve the matching materials root separately."
                )
            elif arguments.database_command == "migrate":
                migrate_database(
                    arguments.database,
                    backup_path=arguments.backup,
                    no_backup=arguments.no_backup,
                    acknowledge=arguments.acknowledge_data_loss_risk,
                )
                print(
                    "Database migrated to schema v13. Any generated pre-migration backup contains SQLite only; preserve the matching materials root separately."
                )
            elif arguments.database_command == "restore":
                if not arguments.confirm_replace:
                    parser.error("--confirm-replace is required")
                restore_database(arguments.backup, arguments.database)
                print("SQLite database restored.")
                print(
                    "Managed material files were not restored; use the matching materials root."
                )
            return 0
        except (OSError, sqlite3.Error, ValueError) as error:
            print(
                f"Database operation failed: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1

    if arguments.command == "serve":
        try:
            serve(
                port=arguments.port,
                ipv6=arguments.ipv6,
                database=arguments.database,
                materials_root=arguments.materials_root,
            )
            return 0
        except (OSError, sqlite3.Error, ValueError) as error:
            print(f"Server failed: {type(error).__name__}: {error}", file=sys.stderr)
            return 1

    if arguments.command == "collect":
        return run_collect_command(arguments, parser)

    if arguments.command == "material":
        return run_material_command(arguments)

    if arguments.command == "application":
        if arguments.application_command == "redact-note":
            return run_application_redact_note(arguments.event_id, arguments.database)
        return run_application_command(arguments, parser)

    if arguments.command == "search":
        return run_search_command(arguments, parser)

    if arguments.command == "show":
        return run_show_command(arguments, parser)

    if arguments.command == "rank":
        return run_rank_command(arguments, parser)

    if arguments.command == "profile":
        return run_profile_command(arguments)

    if arguments.command == "career-profile":
        return run_career_profile_command(arguments)

    if arguments.command == "preference":
        return run_preference_command(arguments)

    if arguments.command == "report":
        return run_report_command(arguments, parser)

    if arguments.command == "source":
        return run_source_command(arguments)

    if arguments.command == "group":
        return run_group_command(arguments, parser)

    if arguments.command == "review":
        return run_review_command(arguments, parser)

    if arguments.command == "saved-search":
        return run_saved_search_command(arguments, parser)

    if arguments.command == "reminder":
        return run_reminder_command(arguments, parser)

    if arguments.command == "export":
        return run_export_command(arguments, parser)

    parser.error("Unsupported command.")
    return 2
