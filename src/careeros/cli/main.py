"""CareerOS CLI entry point and command dispatch."""

from __future__ import annotations

from collections.abc import Sequence

from careeros.cli.commands import (
    run_application_command,
    run_collect_command,
    run_export_command,
    run_group_command,
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
from careeros.cli.parser import build_parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CareerOS command-line interface."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    if arguments.command == "diagnose":
        return print_diagnostics()

    if arguments.command == "collect":
        return run_collect_command(arguments, parser)

    if arguments.command == "application":
        return run_application_command(arguments, parser)

    if arguments.command == "search":
        return run_search_command(arguments, parser)

    if arguments.command == "show":
        return run_show_command(arguments, parser)

    if arguments.command == "rank":
        return run_rank_command(arguments, parser)

    if arguments.command == "profile":
        return run_profile_command(arguments)

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
