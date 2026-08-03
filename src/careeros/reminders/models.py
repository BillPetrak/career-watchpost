"""Reminder domain model, kind vocabulary, and timestamp parsing.

Reminder kinds are validated centrally here and exposed consistently to the
Python layer (via :func:`validate_kind`) and the CLI (via argparse choices).
Timestamp parsing is deliberately strict and deterministic: CareerOS never
guesses a timezone, so every user-supplied due time must carry an explicit UTC
offset.
"""

from __future__ import annotations

from datetime import datetime

REMINDER_KINDS: tuple[str, ...] = (
    "follow_up",
    "interview",
    "offer",
    "review",
    "custom",
)

# List-state vocabulary used by the ``reminder list`` filters.
REMINDER_STATES: tuple[str, ...] = (
    "open",
    "overdue",
    "due",
    "upcoming",
    "completed",
)


class ReminderError(Exception):
    """Base class for reminder domain errors."""


class UnknownJobError(ReminderError):
    """Raised when a reminder is added for a job id that does not exist."""


class UnknownReminderError(ReminderError):
    """Raised when an operation targets a reminder id that does not exist."""


class ValidationError(ReminderError):
    """Raised when a reminder definition or filter is invalid."""


def validate_kind(kind: str) -> str:
    """Return the kind unchanged when it is a known reminder kind.

    Raises ``ValueError`` for unknown kinds so the service layer can rely on a
    single authoritative vocabulary regardless of which caller invokes it.
    """
    if kind not in REMINDER_KINDS:
        choices = ", ".join(REMINDER_KINDS)
        raise ValueError(f"Invalid reminder kind {kind!r}; expected one of: {choices}.")
    return kind


def parse_due_at(value: str) -> datetime:
    """Parse a user-supplied ISO-8601 due timestamp without guessing.

    Rules (deterministic and explicit):

    - A full timestamp with an explicit UTC offset is required.
    - ``Z`` is accepted and treated as UTC.
    - Naive timestamps (no offset) are rejected.
    - Date-only values (no time component) are rejected.

    Returns a timezone-aware datetime (not yet normalized to UTC; callers
    serialize with :func:`careeros.reminders.service._serialize`).
    """
    if value is None or not value.strip():
        raise ValidationError("Due timestamp must be a non-empty ISO-8601 string.")
    stripped = value.strip()
    if "T" not in stripped and " " not in stripped:
        raise ValidationError(
            "Due timestamp must include a time component; date-only values are "
            "not accepted (CareerOS never picks a midnight or a timezone)."
        )
    try:
        parsed = datetime.fromisoformat(stripped)
    except ValueError as error:
        raise ValidationError(
            f"Invalid due timestamp {value!r}; expected ISO-8601 with an "
            "explicit UTC offset, e.g. 2026-08-10T14:00:00+00:00 or "
            "2026-08-10T14:00:00Z."
        ) from error
    if parsed.tzinfo is None:
        raise ValidationError(
            f"Invalid due timestamp {value!r}: a naive timestamp without an "
            "explicit UTC offset is not accepted (CareerOS never guesses a "
            "timezone)."
        )
    return parsed
