"""Shared constants, paths, and small helpers for the CareerOS CLI.

This module holds the cross-cutting pieces used by the parser, the renderers,
and the command handlers: project paths, exit codes, the source display
labels, the shared profile-loading and lock-conflict paths, and small
validation helpers. It must never import command modules, the parser, or the
main dispatch module.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from careeros import settings
from careeros.locking import LockConflict
from careeros.ranking.profile import load_profile
from careeros.ranking.scorer import CandidateProfile

DEFAULT_DATABASE_PATH = settings.DEFAULT_DATABASE_PATH
DEFAULT_PROFILE_PATH = settings.DEFAULT_PROFILE_PATH


# EX_TEMPFAIL from sysexits.h: a conventional "temporary failure, retry later"
# code, distinct from 1 (collection/setup failure) and 2 (argparse errors).
EXIT_TEMP_FAILURE = 75


def load_profile_or_error(profile_path: Path) -> CandidateProfile | None:
    """Load a candidate profile, printing a message and returning None on failure.

    Centralizes the load-profile failure path used by ``review next``, ``rank``,
    ``report``, and the ``profile`` commands: every call site keeps the exact
    message ``Failed to load profile: {error}`` and exit code 1.
    """
    try:
        return load_profile(profile_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"Failed to load profile: {error}", file=sys.stderr)
        return None


def no_job_found_message(job_id: int) -> str:
    """Return the canonical "no such job" message for a job id."""
    return f"No job found with ID {job_id}."


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


_SOURCE_LABELS = {
    "arbeitnow": "Arbeitnow",
    "himalayas": "Himalayas",
    "jobicy": "Jobicy",
    "remoteok": "Remote OK",
    "remotive": "Remotive",
    "weworkremotely": "We Work Remotely",
    "workingnomads": "Working Nomads",
}


def _lock_conflict_message(error: LockConflict) -> str:
    """Format a concise lock-contention message for stderr."""
    lines = [
        f"{error.lock_name.capitalize()} already running for database:",
        str(error.database_path),
        f"Lock file: {error.lock_path}",
    ]
    owner: list[str] = []
    if error.metadata.get("pid"):
        owner.append(f"pid={error.metadata['pid']}")
    if error.metadata.get("command"):
        owner.append(f"command={error.metadata['command']!r}")
    if error.metadata.get("acquired_at"):
        owner.append(f"acquired_at={error.metadata['acquired_at']}")
    if owner:
        lines.append("Owner: " + ", ".join(owner))
    return "\n".join(lines)


def _validate_application_filter_flags(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
) -> None:
    """Reject contradictory combinations of tracking filter flags."""
    include_statuses = arguments.status or ()
    exclude_statuses = arguments.exclude_status or ()
    if arguments.untracked and (include_statuses or exclude_statuses):
        parser.error("--untracked cannot be combined with --status or --exclude-status")
    if include_statuses and exclude_statuses:
        parser.error("--status and --exclude-status cannot be combined")
