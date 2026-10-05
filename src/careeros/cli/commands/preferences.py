"""The persistent ``preference`` inspection and reversal command family."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from careeros.cli.common import (
    EXIT_TEMP_FAILURE,
    _lock_conflict_message,
    load_profile_or_error,
)
from careeros.database.connection import connect_current_rw, connect_read_only
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.preferences.learning import LearnedPreference, OpportunityReference
from careeros.preferences.models import ExplicitPreferenceProfile
from careeros.preferences.service import (
    PreferenceInspection,
    clear_explicit_profile,
    inspect_preferences,
    replace_explicit_profile,
    reset_learned_preferences,
    restore_learned_preference,
    suppress_learned_preference,
)


def _explicit_dict(profile: ExplicitPreferenceProfile) -> dict[str, Any]:
    return {
        "desired_titles": list(profile.desired_titles),
        "skills": list(profile.skills),
        "required_skills": list(profile.required_skills),
        "preferred_locations": list(profile.preferred_locations),
        "remote_preference": profile.remote_preference,
        "excluded_terms": list(profile.excluded_terms),
        "hard_excluded_terms": list(profile.hard_excluded_terms),
        "preferred_employment_types": list(profile.preferred_employment_types),
        "preferred_seniority": list(profile.preferred_seniority),
    }


def _example_dict(example: OpportunityReference) -> dict[str, Any]:
    return {
        "job_id": example.job_id,
        "title": example.title,
        "company": example.company,
    }


def _learned_dict(preference: LearnedPreference) -> dict[str, Any]:
    return {
        "key": preference.preference_key,
        "dimension": preference.dimension,
        "display_value": preference.display_value,
        "normalized_value": preference.normalized_value,
        "polarity": preference.polarity,
        "positive_count": preference.positive_count,
        "negative_count": preference.negative_count,
        "net_evidence": preference.net_evidence,
        "strength_level": preference.strength_level,
        "active": preference.active,
        "activation_reason": preference.activation_reason,
        "suppressed": preference.suppressed,
        "positive_examples": [
            _example_dict(example) for example in preference.positive_examples
        ],
        "negative_examples": [
            _example_dict(example) for example in preference.negative_examples
        ],
    }


def inspection_dict(inspection: PreferenceInspection) -> dict[str, Any]:
    """Return the explicit allow-listed JSON projection used by the CLI."""
    return {
        "explicit": {
            "configured": inspection.explicit.configured,
            "updated_at": inspection.explicit.updated_at,
            "profile": _explicit_dict(inspection.explicit.profile),
            "locked_dimensions": sorted(inspection.explicit.locked_dimensions),
        },
        "learned": [_learned_dict(item) for item in inspection.learned],
        "learning_reset_at": inspection.state.learning_reset_at,
        "learning_reset_review_event_id": (
            inspection.state.learning_reset_review_event_id
        ),
    }


def _ranking_status(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return bounded ranking readiness metadata without private authority values."""
    try:
        from careeros.database.review_ranking import fully_prepared, get_authority

        authority = get_authority(connection)
    except (ImportError, sqlite3.Error, ValueError):
        return {"state": "unavailable", "ready": False}
    return {
        "state": authority.state,
        "ready": fully_prepared(connection),
        "pending_modes": authority.required_mode_mask,
        "target_revision": authority.target_revision,
        "retry_not_before": authority.retry_not_before,
    }


def _render_text(inspection: PreferenceInspection) -> None:
    explicit = inspection.explicit
    profile = explicit.profile
    print("Matching preferences")
    print("--------------------")
    print(f"Stored explicit configured: {'yes' if explicit.configured else 'no'}")
    print(f"Stored explicit updated: {explicit.updated_at or '-'}")
    fields = (
        ("Desired titles", profile.desired_titles),
        ("Skills", profile.skills),
        ("Required skills", profile.required_skills),
        ("Preferred locations", profile.preferred_locations),
        (
            "Remote preference",
            (profile.remote_preference,) if profile.remote_preference else (),
        ),
        ("Excluded terms", profile.excluded_terms),
        ("Hard excluded terms", profile.hard_excluded_terms),
        ("Employment types", profile.preferred_employment_types),
        ("Seniority", profile.preferred_seniority),
    )
    for label, values in fields:
        print(f"{label + ':':<25}{', '.join(values) or '-'}")
    print(
        f"{'Learned locks:':<25}{', '.join(sorted(explicit.locked_dimensions)) or '-'}"
    )
    print()
    print("Learned preferences")
    print("-------------------")
    if not inspection.learned:
        print("No learned preferences.")
        return
    for preference in inspection.learned:
        state = (
            "suppressed"
            if preference.suppressed
            else ("active" if preference.active else "inactive")
        )
        print()
        print(f"{preference.dimension}: {preference.display_value} [{state}]")
        print(f"  Key: {preference.preference_key}")
        print(
            "  Evidence: "
            f"+{preference.positive_count} / -{preference.negative_count} "
            f"(net {preference.net_evidence:+d})"
        )
        print(
            f"  Polarity: {preference.polarity or '-'}; "
            f"strength: {preference.strength_level}; "
            f"reason: {preference.activation_reason}"
        )
        for label, examples in (
            ("Positive examples", preference.positive_examples),
            ("Negative examples", preference.negative_examples),
        ):
            rendered = "; ".join(
                f"job {item.job_id}: {item.title} ({item.company or 'N/A'})"
                for item in examples
            )
            print(f"  {label}: {rendered or '-'}")


def _show(arguments: argparse.Namespace) -> int:
    try:
        with connect_read_only(arguments.database) as connection:
            inspection = inspect_preferences(connection)
            ranking_status = _ranking_status(connection)
    except (OSError, sqlite3.Error, ValueError) as error:
        print(
            f"Preference inspection failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    if arguments.format == "json":
        # Preserve the established machine-readable preference contract. The
        # human-facing text form carries bounded ranking readiness below.
        print(
            json.dumps(inspection_dict(inspection), ensure_ascii=False, sort_keys=True)
        )
    else:
        _render_text(inspection)
        print()
        print("Review ranking")
        print("--------------")
        print(f"State:                 {ranking_status['state']}")
        print(
            f"Ready:                 {'yes' if ranking_status['ready'] else 'pending'}"
        )
    return 0


def _write(arguments: argparse.Namespace) -> int:
    command = arguments.preference_command
    try:
        profile = None
        if command == "explicit" and arguments.preference_explicit_command == "replace":
            profile = load_profile_or_error(arguments.profile)
            if profile is None:
                return 1
        with collection_lock(arguments.database, command=f"preference {command}"):
            with connect_current_rw(arguments.database) as connection:
                if command == "explicit":
                    if arguments.preference_explicit_command == "replace":
                        assert profile is not None
                        replace_explicit_profile(connection, profile)
                        print("Stored explicit preferences replaced.")
                    else:
                        clear_explicit_profile(connection)
                        print("Stored explicit preferences cleared.")
                elif command == "suppress":
                    suppress_learned_preference(connection, arguments.key)
                    print(f"Suppressed learned preference {arguments.key}.")
                elif command == "restore":
                    restore_learned_preference(connection, arguments.key)
                    print(f"Restored learned preference {arguments.key}.")
                else:
                    reset_learned_preferences(connection)
                    print("Learned preferences reset.")
                if not _ranking_status(connection)["ready"]:
                    print("Review ranking rebuild pending.")
    except CollectionLockConflict as error:
        print(_lock_conflict_message(error), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    except ValueError as error:
        print(f"Preference validation failed: {error}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as error:
        print(
            f"Preference operation failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


def run_preference_command(arguments: argparse.Namespace) -> int:
    """Dispatch one persistent preference command."""
    if arguments.preference_command == "show":
        return _show(arguments)
    return _write(arguments)
