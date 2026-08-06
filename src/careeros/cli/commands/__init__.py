"""Command handlers for the CareerOS CLI.

Each module here owns one top-level command (or family of subcommands) and its
per-command dispatch function, which the main dispatch module imports. Command
modules must never import the parser or the main dispatch module; shared
renderers and helpers live in ``careeros.cli.render`` and
``careeros.cli.common``.
"""

from careeros.cli.commands.applications import (
    run_application_command,
    run_application_history,
    run_application_list,
    run_application_set,
    run_application_show,
)
from careeros.cli.commands.collect import (
    run_collect,
    run_collect_all,
    run_collect_command,
    run_collect_due,
    run_collect_history,
    run_collect_status,
)
from careeros.cli.commands.diagnose import print_diagnostics
from careeros.cli.commands.export import run_export, run_export_command
from careeros.cli.commands.group import (
    run_group_command,
    run_group_explain,
    run_group_regroup,
)
from careeros.cli.commands.profile import (
    run_profile_command,
    run_profile_init,
    run_profile_show,
    run_profile_validate,
)
from careeros.cli.commands.rank import run_rank, run_rank_command
from careeros.cli.commands.reminders import (
    run_reminder_add,
    run_reminder_command,
    run_reminder_complete,
    run_reminder_delete,
    run_reminder_list,
    run_reminder_notify_dry_run,
    run_reminder_notify_due,
    run_reminder_notify_history,
    run_reminder_reopen,
    run_reminder_show,
    run_reminder_update,
)
from careeros.cli.commands.report import run_report, run_report_command
from careeros.cli.commands.review import (
    run_review_clear,
    run_review_command,
    run_review_history,
    run_review_list,
    run_review_next,
    run_review_set,
    run_review_show,
)
from careeros.cli.commands.saved_searches import run_saved_search_command
from careeros.cli.commands.search import run_search, run_search_command
from careeros.cli.commands.show import run_show, run_show_command
from careeros.cli.commands.source import run_source_command, run_source_stats

__all__ = [
    "print_diagnostics",
    "run_application_command",
    "run_application_history",
    "run_application_list",
    "run_application_set",
    "run_application_show",
    "run_collect",
    "run_collect_all",
    "run_collect_command",
    "run_collect_due",
    "run_collect_history",
    "run_collect_status",
    "run_export",
    "run_export_command",
    "run_group_command",
    "run_group_explain",
    "run_group_regroup",
    "run_profile_command",
    "run_profile_init",
    "run_profile_show",
    "run_profile_validate",
    "run_rank",
    "run_rank_command",
    "run_reminder_add",
    "run_reminder_command",
    "run_reminder_complete",
    "run_reminder_delete",
    "run_reminder_list",
    "run_reminder_notify_dry_run",
    "run_reminder_notify_due",
    "run_reminder_notify_history",
    "run_reminder_reopen",
    "run_reminder_show",
    "run_reminder_update",
    "run_report",
    "run_report_command",
    "run_review_clear",
    "run_review_command",
    "run_review_history",
    "run_review_list",
    "run_review_next",
    "run_review_set",
    "run_review_show",
    "run_saved_search_command",
    "run_search",
    "run_search_command",
    "run_show",
    "run_show_command",
    "run_source_command",
    "run_source_stats",
]
