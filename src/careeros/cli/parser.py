# ruff: noqa: E501
"""Argument-parser construction for the CareerOS CLI.

All parser definitions, defaults, choices, and help text live here. This module
must never import command modules or the main dispatch module.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from careeros.applications.models import APPLICATION_STATUSES
from careeros.cli.common import _SOURCE_LABELS
from careeros.collectors import COLLECTORS
from careeros.reminders.models import REMINDER_KINDS
from careeros.review.models import DECISION_VALUES, QUEUE_STATES
from careeros.saved_searches.models import SEARCH_MODES
from careeros.settings import database_path
from careeros.systemd import add_parser as add_systemd_parser

_PRIVATE_PROFILE_DEFAULT_HELP = (
    "private XDG configuration path "
    "(${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml)"
)


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the CareerOS command-line parser."""
    default_database = database_path()
    # Keep the established parser declarations compact while resolving the
    # environment-sensitive default once for this parser instance.
    DEFAULT_DATABASE_PATH = default_database
    parser = argparse.ArgumentParser(
        prog="careeros",
        description="Local-first career intelligence tools.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    add_systemd_parser(subparsers)

    database_parser = subparsers.add_parser(
        "database", help="Safely administer a CareerOS database."
    )
    database_commands = database_parser.add_subparsers(
        dest="database_command", required=True
    )
    backup_parser = database_commands.add_parser(
        "backup",
        help="Back up the SQLite database only; managed material files are not included.",
    )
    backup_parser.add_argument("destination", type=Path)
    backup_parser.add_argument("--database", type=Path, default=default_database)
    migrate_parser = database_commands.add_parser(
        "migrate",
        help="Migrate the SQLite database only; managed material files are not migrated.",
    )
    migrate_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    migrate_parser.add_argument("--backup", type=Path)
    migrate_parser.add_argument("--no-backup", action="store_true")
    migrate_parser.add_argument("--acknowledge-data-loss-risk", action="store_true")
    restore_parser = database_commands.add_parser(
        "restore",
        help="Restore the SQLite database only; managed material files are not restored.",
    )
    restore_parser.add_argument("backup", type=Path)
    restore_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    restore_parser.add_argument("--confirm-replace", action="store_true")
    serve_parser = subparsers.add_parser(
        "serve", help="Serve the protected local web application."
    )
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--ipv6", action="store_true")
    serve_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    serve_parser.add_argument("--materials-root", type=Path)

    material_parser = subparsers.add_parser(
        "material", help="Manage private reusable materials."
    )
    material_commands = material_parser.add_subparsers(
        dest="material_command", required=True
    )
    ml = material_commands.add_parser("list")
    ml.add_argument("--include-retired", action="store_true")
    ml.add_argument("--limit", type=_positive, default=20)
    ml.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    ms = material_commands.add_parser("show")
    ms.add_argument("material_id")
    ms.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    mi = material_commands.add_parser("import")
    mi.add_argument("source", type=Path)
    mi.add_argument("--name", required=True)
    mi.add_argument("--category", required=True)
    mi.add_argument("--media-type", default="application/octet-stream")
    mi.add_argument("--display-filename")
    mi.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    mi.add_argument("--materials-root", type=Path)
    mv = material_commands.add_parser("add-version")
    mv.add_argument("material_id")
    mv.add_argument("source", type=Path)
    mv.add_argument("--expected-revision", type=_positive, required=True)
    mv.add_argument("--media-type", default="application/octet-stream")
    mv.add_argument("--display-filename")
    mv.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    mv.add_argument("--materials-root", type=Path)
    for command in ("update", "retire", "restore"):
        p = material_commands.add_parser(command)
        p.add_argument("material_id")
        p.add_argument("--expected-revision", type=_positive, required=True)
        if command == "update":
            p.add_argument("--name")
            p.add_argument("--category")
        p.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    me = material_commands.add_parser("export-version")
    me.add_argument("version_id")
    me.add_argument("destination", type=Path)
    me.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    me.add_argument("--materials-root", type=Path)
    ma = material_commands.add_parser("audit")
    ma.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    ma.add_argument("--materials-root", type=Path)

    subparsers.add_parser(
        "diagnose",
        help="Check the CareerOS development environment.",
    )

    collect_parser = subparsers.add_parser(
        "collect",
        help="Collect jobs from an external source.",
        epilog=(
            "For 'all', sources run sequentially in a single invocation, each "
            "called once. For 'due', only sources whose last completed run is "
            "older than their polling interval are collected. Do not run "
            "repeated all-source collections more often than the most "
            "restrictive source permits (Himalayas allows at most one poll "
            "per day)."
        ),
    )
    collect_commands = collect_parser.add_subparsers(
        dest="collect_command",
        required=True,
    )

    for name in ("all", "due", *COLLECTORS):
        if name == "all":
            command_help = "Collect from every registered source."
        elif name == "due":
            command_help = "Collect only sources whose polling interval has expired."
        else:
            command_help = f"Collect jobs from {_SOURCE_LABELS.get(name, name)}."
        command_parser = collect_commands.add_parser(name, help=command_help)
        command_parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum jobs to collect (default: 25).",
        )
        command_parser.add_argument(
            "--database",
            type=Path,
            default=DEFAULT_DATABASE_PATH,
            help="SQLite database path.",
        )

    status_parser = collect_commands.add_parser(
        "status",
        help="Show collection status for every registered source.",
    )
    status_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    history_parser = collect_commands.add_parser(
        "history",
        help="Show recent collection run history.",
    )
    history_parser.add_argument(
        "--source",
        choices=[*COLLECTORS],
        help="Only show runs for this source.",
    )
    history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum runs to show (default: 20).",
    )
    history_parser.add_argument(
        "--failed",
        action="store_true",
        help="Only show completed failed runs.",
    )
    history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    application_parser = subparsers.add_parser(
        "application",
        help="Track application workflow state for stored jobs.",
    )
    application_commands = application_parser.add_subparsers(
        dest="application_command",
        required=True,
    )
    redact_parser = application_commands.add_parser(
        "redact-note", help="Destroy a private application event note."
    )
    redact_parser.add_argument("event_id", type=int)
    redact_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)

    set_parser = application_commands.add_parser(
        "set",
        help="Set or update the application status for a stored job.",
    )
    set_parser.add_argument(
        "job_id",
        type=int,
        help="Database job ID.",
    )
    set_parser.add_argument(
        "status",
        choices=APPLICATION_STATUSES,
        help="Application status (one of: %(choices)s).",
    )
    set_parser.add_argument(
        "--note",
        default=None,
        help="Optional note attached to this status change.",
    )
    set_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = application_commands.add_parser(
        "show",
        help="Show the current application state for one job.",
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

    list_parser = application_commands.add_parser(
        "list",
        help="List tracked applications.",
    )
    list_parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Only show applications with this status (repeatable).",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum applications to show (default: 20).",
    )
    list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    history_parser = application_commands.add_parser(
        "history",
        help="Show the event history for one tracked job.",
    )
    history_parser.add_argument(
        "job_id",
        type=int,
        help="Database job ID.",
    )
    history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum events to show (default: 20).",
    )
    history_parser.add_argument(
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
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates into one result per group.",
    )
    search_parser.add_argument(
        "--untracked",
        action="store_true",
        help="Only show jobs with no application record.",
    )
    search_parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Only show jobs with this application status (repeatable).",
    )
    search_parser.add_argument(
        "--exclude-status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Hide jobs with this application status (repeatable).",
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
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates into one result per group.",
    )
    rank_parser.add_argument(
        "--untracked",
        action="store_true",
        help="Only show jobs with no application record.",
    )
    rank_parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Only show jobs with this application status (repeatable).",
    )
    rank_parser.add_argument(
        "--exclude-status",
        action="append",
        choices=APPLICATION_STATUSES,
        help="Hide jobs with this application status (repeatable).",
    )
    rank_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum results (default: 20).",
    )
    rank_parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Only show results with a match score of at least N (0-100).",
    )
    rank_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    rank_parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="Disable review-feedback adjustments to match scores.",
    )
    rank_parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Disable persistent learned-preference adjustments.",
    )
    rank_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    profile_parser = subparsers.add_parser(
        "profile",
        help="Inspect, validate, and initialize candidate profile files.",
    )
    profile_commands = profile_parser.add_subparsers(
        dest="profile_command",
        required=True,
    )

    init_parser = profile_commands.add_parser(
        "init",
        help="Create a starter candidate profile at the private path (or PATH).",
    )
    init_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Destination path (.json/.yaml/.yml); defaults to the "
            f"{_PRIVATE_PROFILE_DEFAULT_HELP}."
        ),
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the file if it already exists.",
    )

    show_parser = profile_commands.add_parser(
        "show",
        help="Show a candidate profile from a JSON or YAML file.",
    )
    show_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Path to a candidate profile JSON or YAML file; defaults to the "
            f"{_PRIVATE_PROFILE_DEFAULT_HELP}."
        ),
    )

    validate_parser = profile_commands.add_parser(
        "validate",
        help="Validate a candidate profile file.",
    )
    validate_parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=None,
        help=(
            "Path to a candidate profile JSON or YAML file; defaults to the "
            f"{_PRIVATE_PROFILE_DEFAULT_HELP}."
        ),
    )

    preference_parser = subparsers.add_parser(
        "preference",
        help="Inspect and control persistent matching preferences.",
    )
    preference_commands = preference_parser.add_subparsers(
        dest="preference_command",
        required=True,
    )
    preference_show = preference_commands.add_parser(
        "show", help="Show stored explicit and learned preference state."
    )
    preference_show.add_argument("--format", choices=("text", "json"), default="text")
    preference_show.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)

    preference_explicit = preference_commands.add_parser(
        "explicit", help="Replace or clear stored explicit preferences."
    )
    preference_explicit_commands = preference_explicit.add_subparsers(
        dest="preference_explicit_command",
        required=True,
    )
    preference_replace = preference_explicit_commands.add_parser(
        "replace", help="Replace the complete stored explicit profile."
    )
    preference_replace.add_argument("profile", type=Path)
    preference_replace.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH
    )
    preference_clear = preference_explicit_commands.add_parser(
        "clear", help="Remove stored explicit preference authority."
    )
    preference_clear.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH
    )

    for command, help_text in (
        ("suppress", "Suppress one learned preference."),
        ("restore", "Restore one learned preference."),
    ):
        preference_learned = preference_commands.add_parser(command, help=help_text)
        preference_learned.add_argument("key")
        preference_learned.add_argument(
            "--database", type=Path, default=DEFAULT_DATABASE_PATH
        )

    preference_reset = preference_commands.add_parser(
        "reset-learned", help="Delete all learned evidence and suppressions."
    )
    preference_reset.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="Confirm destructive learned-state reset.",
    )
    preference_reset.add_argument(
        "--database", type=Path, default=DEFAULT_DATABASE_PATH
    )

    report_parser = subparsers.add_parser(
        "report",
        help=(
            "Show a read-only Career Snapshot of jobs, applications, and "
            "collection state."
        ),
    )
    report_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )
    report_parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help=(
            "Path to a candidate profile JSON file "
            "(overrides a saved search's profile)."
        ),
    )
    report_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum recommendations and action items to show (default: 10).",
    )
    report_parser.add_argument(
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates in recommendations (flat default).",
    )
    report_parser.add_argument(
        "--saved-search",
        default=None,
        help="Name of a rank-mode saved search whose filters drive recommendations.",
    )
    report_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    report_parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="Disable review-feedback adjustments to recommended scores.",
    )
    report_parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Disable persistent learned-preference adjustments.",
    )

    source_parser = subparsers.add_parser(
        "source",
        help="Show unified per-source statistics and source-quality diagnostics.",
    )
    source_commands = source_parser.add_subparsers(
        dest="source_command",
        required=True,
    )

    stats_parser = source_commands.add_parser(
        "stats",
        help="Show per-source statistics (read-only).",
    )
    stats_parser.add_argument(
        "--source",
        choices=[*COLLECTORS],
        help="Only show statistics for this source.",
    )
    stats_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    stats_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    group_parser = subparsers.add_parser(
        "group",
        help="Manage canonical cross-source duplicate groups.",
    )
    group_commands = group_parser.add_subparsers(
        dest="group_command",
        required=True,
    )

    regroup_parser = group_commands.add_parser(
        "regroup",
        help="Recompute canonical groups from stored jobs.",
    )
    regroup_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    explain_parser = group_commands.add_parser(
        "explain",
        help="Show canonical groups without mutating state.",
    )
    explain_parser.add_argument(
        "--group-id",
        help="Only show this group.",
    )
    explain_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum groups to show (default: 20).",
    )
    explain_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    review_parser = subparsers.add_parser(
        "review",
        help="Manage personalized daily review decisions for stored jobs.",
    )
    review_commands = review_parser.add_subparsers(
        dest="review_command",
        required=True,
    )

    set_parser = review_commands.add_parser(
        "set",
        help="Set or update the review decision for a stored job.",
    )
    set_parser.add_argument("job_id", type=int, help="Database job ID.")
    set_parser.add_argument(
        "decision",
        choices=DECISION_VALUES,
        help="Review decision (one of: %(choices)s).",
    )
    set_parser.add_argument(
        "--note", default=None, help="Optional note attached to this decision change."
    )
    set_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    clear_parser = review_commands.add_parser(
        "clear",
        help="Clear the review decision for a stored job.",
    )
    clear_parser.add_argument("job_id", type=int, help="Database job ID.")
    clear_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = review_commands.add_parser(
        "show",
        help="Show the current review decision for one job.",
    )
    show_parser.add_argument("job_id", type=int, help="Database job ID.")
    show_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    list_parser = review_commands.add_parser(
        "list",
        help="List review decisions.",
    )
    list_parser.add_argument(
        "--decision",
        action="append",
        choices=DECISION_VALUES,
        help="Only show decisions with this value (repeatable).",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum decisions to show (default: 20).",
    )
    list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    history_parser = review_commands.add_parser(
        "history",
        help="Show the decision history for one job.",
    )
    history_parser.add_argument("job_id", type=int, help="Database job ID.")
    history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum events to show (default: 20).",
    )
    history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    next_parser = review_commands.add_parser(
        "next",
        help="Rank today's personalized review queue.",
    )
    next_parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Path to a candidate profile JSON/YAML file.",
    )
    next_parser.add_argument(
        "--state",
        choices=QUEUE_STATES,
        default="unreviewed",
        help="Which queue state to rank (default: unreviewed).",
    )
    next_parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Only show results with a match score of at least N (0-100).",
    )
    next_parser.add_argument(
        "--include-tracked",
        action="store_true",
        help="Include opportunities that already have an application record.",
    )
    next_parser.add_argument(
        "--source",
        help="Filter by source (e.g. arbeitnow).",
    )
    next_parser.add_argument(
        "--location",
        help="Filter by location (partial match).",
    )
    next_parser.add_argument(
        "--remote",
        action="store_true",
        help="Only show remote jobs.",
    )
    next_grouped = next_parser.add_mutually_exclusive_group()
    next_grouped.add_argument(
        "--grouped",
        dest="grouped",
        action="store_true",
        default=True,
        help="Collapse cross-source duplicates into one result per group (default).",
    )
    next_grouped.add_argument(
        "--no-grouped",
        dest="grouped",
        action="store_false",
        help="Show every job individually without collapsing duplicates.",
    )
    next_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum results (default: 20).",
    )
    next_parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    next_parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="Disable review-feedback adjustments to queue scores.",
    )
    next_parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Disable persistent learned-preference adjustments.",
    )
    next_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    reminder_parser = subparsers.add_parser(
        "reminder",
        help="Manage local follow-up reminders for stored jobs.",
    )
    reminder_commands = reminder_parser.add_subparsers(
        dest="reminder_command",
        required=True,
    )

    add_parser = reminder_commands.add_parser(
        "add",
        help="Add a reminder for a stored job.",
    )
    add_parser.add_argument("job_id", type=int, help="Database job ID.")
    add_parser.add_argument(
        "kind",
        choices=REMINDER_KINDS,
        help="Reminder kind (one of: %(choices)s).",
    )
    add_parser.add_argument(
        "due_at",
        help=(
            "Due timestamp as ISO-8601 with an explicit UTC offset, e.g. "
            "2026-08-10T14:00:00Z."
        ),
    )
    add_parser.add_argument(
        "--message", default=None, help="Optional reminder message."
    )
    add_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    list_parser = reminder_commands.add_parser(
        "list",
        help="List reminders for stored jobs.",
    )
    state_group = list_parser.add_mutually_exclusive_group()
    state_group.add_argument(
        "--open",
        action="store_const",
        const="open",
        dest="state",
        help="Only show open (not completed) reminders (default).",
    )
    state_group.add_argument(
        "--overdue",
        action="store_const",
        const="overdue",
        dest="state",
        help="Only show open reminders whose due time has passed.",
    )
    state_group.add_argument(
        "--due",
        action="store_const",
        const="due",
        dest="state",
        help="Only show open reminders due now or earlier.",
    )
    state_group.add_argument(
        "--upcoming",
        action="store_const",
        const="upcoming",
        dest="state",
        help="Only show open reminders due in the future.",
    )
    state_group.add_argument(
        "--completed",
        action="store_const",
        const="completed",
        dest="state",
        help="Only show completed reminders.",
    )
    list_parser.add_argument(
        "--job",
        type=int,
        dest="job_id",
        default=None,
        help="Only show reminders for this job.",
    )
    list_parser.add_argument(
        "--kind",
        choices=REMINDER_KINDS,
        default=None,
        help="Only show reminders of this kind.",
    )
    list_parser.add_argument(
        "--after",
        default=None,
        help="Only show reminders due at or after this ISO-8601 timestamp.",
    )
    list_parser.add_argument(
        "--before",
        default=None,
        help="Only show reminders due at or before this ISO-8601 timestamp.",
    )
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum reminders to show (default: 20).",
    )
    list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = reminder_commands.add_parser(
        "show",
        help="Show one reminder by ID.",
    )
    show_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    show_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    update_parser = reminder_commands.add_parser(
        "update",
        help="Change a reminder's due time, kind, or message.",
    )
    update_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    update_parser.add_argument(
        "--due-at",
        dest="due_at",
        default=None,
        help="New due timestamp (ISO-8601 with an explicit UTC offset).",
    )
    update_parser.add_argument(
        "--kind",
        choices=REMINDER_KINDS,
        default=None,
        help="New reminder kind.",
    )
    message_group = update_parser.add_mutually_exclusive_group()
    message_group.add_argument(
        "--message",
        dest="message",
        default=None,
        help="New reminder message.",
    )
    message_group.add_argument(
        "--clear-message",
        action="store_true",
        help="Clear the reminder message.",
    )
    update_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    complete_parser = reminder_commands.add_parser(
        "complete",
        help="Mark an open reminder completed.",
    )
    complete_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    complete_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    reopen_parser = reminder_commands.add_parser(
        "reopen",
        help="Reopen a completed reminder.",
    )
    reopen_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    reopen_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    delete_parser = reminder_commands.add_parser(
        "delete",
        help="Delete a reminder by ID.",
    )
    delete_parser.add_argument("reminder_id", type=int, help="Database reminder ID.")
    delete_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    notify_due_parser = reminder_commands.add_parser(
        "notify-due",
        help="Deliver local desktop notifications for due reminders.",
    )
    notify_due_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum reminders to notify per run (default: 20).",
    )
    notify_due_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview which reminders would be notified without delivering or writing.",
    )
    notify_due_parser.add_argument(
        "--backend",
        choices=("auto", "desktop", "stdout"),
        default="auto",
        help="Notification backend (default: auto).",
    )
    notify_due_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    notify_history_parser = reminder_commands.add_parser(
        "notify-history",
        help="Show notification delivery history.",
    )
    notify_history_parser.add_argument(
        "--reminder",
        type=int,
        dest="notify_reminder_id",
        default=None,
        help="Only show attempts for this reminder.",
    )
    notify_history_parser.add_argument(
        "--failed",
        action="store_true",
        help="Only show failed attempts.",
    )
    notify_history_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum attempts to show (default: 20).",
    )
    notify_history_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    saved_search_parser = subparsers.add_parser(
        "saved-search",
        help="Manage reusable search/rank workflow definitions.",
    )
    saved_search_commands = saved_search_parser.add_subparsers(
        dest="saved_search_command",
        required=True,
    )

    create_parser = saved_search_commands.add_parser(
        "create",
        help="Create a saved search or ranking workflow.",
    )
    create_parser.add_argument("name", help="Unique workflow name.")
    create_parser.add_argument("query", help="Search keyword or query.")
    create_parser.add_argument(
        "--mode",
        choices=SEARCH_MODES,
        required=True,
        help="Workflow mode (search or rank).",
    )
    _add_saved_search_filter_flags(create_parser, default_database)

    list_parser = saved_search_commands.add_parser(
        "list",
        help="List saved workflows.",
    )
    list_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    show_parser = saved_search_commands.add_parser(
        "show",
        help="Show a saved workflow definition.",
    )
    show_parser.add_argument("name", help="Workflow name.")
    show_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    run_parser = saved_search_commands.add_parser(
        "run",
        help="Execute a saved workflow against stored jobs.",
    )
    run_parser.add_argument("name", help="Workflow name.")
    run_parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Disable persistent learned-preference adjustments for rank mode.",
    )
    run_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    update_parser = saved_search_commands.add_parser(
        "update",
        help="Change a saved workflow definition.",
    )
    update_parser.add_argument("name", help="Workflow name.")
    update_parser.add_argument(
        "--query",
        help="Replace the search keyword or query.",
    )
    update_parser.add_argument(
        "--mode",
        choices=SEARCH_MODES,
        help="Workflow mode (immutable after creation; changing is rejected).",
    )
    _add_saved_search_filter_flags(update_parser, default_database)
    update_parser.add_argument(
        "--clear-source",
        action="store_true",
        help="Clear the source filter.",
    )
    update_parser.add_argument(
        "--clear-location",
        action="store_true",
        help="Clear the location filter.",
    )
    update_parser.add_argument(
        "--clear-remote",
        action="store_true",
        help="Clear the remote filter.",
    )
    update_parser.add_argument(
        "--clear-min-score",
        action="store_true",
        help="Clear the minimum-score filter.",
    )
    update_parser.add_argument(
        "--clear-profile",
        action="store_true",
        help="Clear the rank profile path.",
    )
    update_parser.add_argument(
        "--clear-status-filters",
        action="store_true",
        help="Clear both --status and --exclude-status filters.",
    )

    rename_parser = saved_search_commands.add_parser(
        "rename",
        help="Rename a saved workflow.",
    )
    rename_parser.add_argument("name", help="Current workflow name.")
    rename_parser.add_argument("new_name", help="New workflow name.")
    rename_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    delete_parser = saved_search_commands.add_parser(
        "delete",
        help="Delete a saved workflow.",
    )
    delete_parser.add_argument("name", help="Workflow name.")
    delete_parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="SQLite database path.",
    )

    export_parser = subparsers.add_parser(
        "export",
        help=(
            "Write deterministic JSON/JSONL/CSV exports of stored jobs, "
            "rankings, the review queue, source statistics, or collection "
            "history (read-only)."
        ),
    )
    export_commands = export_parser.add_subparsers(
        dest="export_command",
        required=True,
    )

    jobs_parser = export_commands.add_parser(
        "jobs",
        help="Export stored jobs (flat or grouped).",
    )
    jobs_parser.add_argument("--query", help="Only export jobs matching this keyword.")
    _add_export_filter_flags(jobs_parser)
    jobs_parser.add_argument(
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates into one record per group.",
    )
    _add_export_common_flags(jobs_parser, default_database)

    ranked_parser = export_commands.add_parser(
        "ranked",
        help="Export profile-ranked jobs with scores and reasons.",
    )
    ranked_parser.add_argument(
        "--query",
        required=True,
        help="Search keyword for the jobs to rank.",
    )
    ranked_parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Path to a candidate profile JSON/YAML file.",
    )
    _add_export_filter_flags(ranked_parser)
    ranked_parser.add_argument(
        "--grouped",
        action="store_true",
        help="Collapse cross-source duplicates into one record per group.",
    )
    ranked_parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Only export results with a match score of at least N (0-100).",
    )
    ranked_parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="Disable review-feedback adjustments to exported scores.",
    )
    ranked_parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Disable persistent learned-preference adjustments.",
    )
    _add_export_common_flags(ranked_parser, default_database)

    review_parser = export_commands.add_parser(
        "review",
        help="Export the personalized daily review queue (ranked).",
    )
    review_parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Path to a candidate profile JSON/YAML file.",
    )
    review_parser.add_argument(
        "--state",
        choices=QUEUE_STATES,
        default="unreviewed",
        help="Which queue state to export (default: unreviewed).",
    )
    review_parser.add_argument(
        "--min-score",
        type=int,
        default=0,
        help="Only export results with a match score of at least N (0-100).",
    )
    review_parser.add_argument(
        "--include-tracked",
        action="store_true",
        help="Include opportunities that already have an application record.",
    )
    review_parser.add_argument(
        "--no-feedback",
        action="store_true",
        help="Disable review-feedback adjustments to exported scores.",
    )
    review_parser.add_argument(
        "--no-adaptive",
        action="store_true",
        help="Disable persistent learned-preference adjustments.",
    )
    _add_export_filter_flags(review_parser)
    review_grouped = review_parser.add_mutually_exclusive_group()
    review_grouped.add_argument(
        "--grouped",
        dest="grouped",
        action="store_true",
        default=True,
        help="Collapse cross-source duplicates into one record per group (default).",
    )
    review_grouped.add_argument(
        "--no-grouped",
        dest="grouped",
        action="store_false",
        help="Export every job individually without collapsing duplicates.",
    )
    _add_export_common_flags(review_parser, default_database)

    source_stats_parser = export_commands.add_parser(
        "source-stats",
        help="Export unified per-source statistics.",
    )
    source_stats_parser.add_argument(
        "--source",
        choices=[*COLLECTORS],
        help="Only export statistics for this source.",
    )
    _add_export_common_flags(source_stats_parser, default_database)

    history_parser = export_commands.add_parser(
        "collection-history",
        help="Export collection run history.",
    )
    history_parser.add_argument(
        "--source",
        choices=[*COLLECTORS],
        help="Only export runs for this source.",
    )
    history_parser.add_argument(
        "--failed",
        action="store_true",
        help="Only export completed failed runs.",
    )
    _add_export_common_flags(history_parser, default_database)

    return parser


def _add_export_filter_flags(parser: argparse.ArgumentParser) -> None:
    """Add the job filters shared by the job-bearing export subcommands."""
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Only export remote jobs.",
    )
    parser.add_argument(
        "--location",
        help="Only export jobs matching this location (partial match).",
    )
    parser.add_argument(
        "--source",
        choices=[*COLLECTORS],
        help="Only export jobs from this source.",
    )
    parser.add_argument(
        "--employment-type",
        help="Only export jobs with this employment type (partial match).",
    )
    parser.add_argument(
        "--clean-descriptions",
        action="store_true",
        help="Normalize descriptions to readable text in the export.",
    )


def _add_export_common_flags(
    parser: argparse.ArgumentParser, default_database: Path
) -> None:
    """Add the flags shared by every export subcommand."""
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum records (default: 20; 0 means no cap).",
    )
    parser.add_argument(
        "--format",
        choices=("json", "jsonl", "csv"),
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write to this file instead of stdout.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting an existing --output file.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=default_database,
        help="SQLite database path.",
    )


def _add_saved_search_filter_flags(
    parser: argparse.ArgumentParser, default_database: Path
) -> None:
    """Add the shared filter flags used by saved-search create and update."""
    parser.add_argument(
        "--grouped",
        action="store_true",
        dest="grouped_flag",
        help="Collapse cross-source duplicates into one result per group.",
    )
    parser.add_argument(
        "--no-grouped",
        action="store_true",
        dest="no_grouped_flag",
        help="Do not collapse cross-source duplicates.",
    )
    parser.add_argument(
        "--profile",
        help="Candidate profile JSON for rank mode (required in rank mode).",
    )
    parser.add_argument(
        "--source",
        help="Only include results from this source.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Only include remote jobs.",
    )
    parser.add_argument(
        "--location",
        help="Only include results matching this location.",
    )
    parser.add_argument(
        "--untracked",
        action="store_true",
        dest="untracked_flag",
        help="Only include jobs with no application record.",
    )
    parser.add_argument(
        "--no-untracked",
        action="store_true",
        dest="no_untracked_flag",
        help="Do not restrict to untracked jobs.",
    )
    parser.add_argument(
        "--status",
        action="append",
        choices=APPLICATION_STATUSES,
        dest="status_list",
        help="Only show jobs with this application status (repeatable).",
    )
    parser.add_argument(
        "--exclude-status",
        action="append",
        choices=APPLICATION_STATUSES,
        dest="exclude_status_list",
        help="Hide jobs with this application status (repeatable).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        help="Only show results with a match score of at least N (0-100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum results.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=default_database,
        help="SQLite database path.",
    )
