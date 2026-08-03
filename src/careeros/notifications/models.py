"""Notification-domain model, status vocabulary, and occurrence identity.

Notification delivery is a separate fact from reminder completion. Completion
is workflow state on ``reminders.completed_at``; delivery is an append-only row
in ``reminder_notifications`` keyed by *occurrence* ``(reminder_id, due_at)``.
A reminder becomes eligible again as a new occurrence only when its due time
changes (reschedule); reopening alone does not re-notify an already-delivered
occurrence.
"""

from __future__ import annotations

from dataclasses import dataclass

# Durable status of one notification attempt row.
NOTIFICATION_STATUSES: tuple[str, ...] = ("attempted", "delivered", "failed")

# Notification backends actually used to dispatch (``auto`` is a CLI-level
# preference resolved to one of these before any dispatch).
NOTIFICATION_BACKENDS: tuple[str, ...] = ("desktop", "stdout")


class NotificationError(Exception):
    """Base class for notification-domain errors."""


class BackendUnavailable(NotificationError):
    """Raised when an explicitly-selected backend cannot be used on this host."""


class ValidationError(NotificationError):
    """Raised when notification parameters (e.g. limit) are invalid."""


@dataclass(frozen=True)
class DueOccurrence:
    """One reminder occurrence that currently needs a notification."""

    reminder_id: int
    due_at: str
    kind: str
    message: str | None
    job_id: int
    job_title: str
    job_company: str | None
    job_source: str


@dataclass(frozen=True)
class ClaimedReminder(DueOccurrence):
    """A DueOccurrence that has been claimed for delivery this run."""

    attempt_id: int


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of dispatching one notification to a backend."""

    success: bool
    error: str | None = None


@dataclass(frozen=True)
class NotificationSummary:
    """Delivery totals used by the read-only report."""

    pending_due: int
    delivered_total: int
    failed_total: int
    last_delivered_at: str | None
    last_failed_at: str | None


__all__: tuple[str, ...] = (
    "NOTIFICATION_BACKENDS",
    "NOTIFICATION_STATUSES",
    "BackendUnavailable",
    "ClaimedReminder",
    "DeliveryResult",
    "DueOccurrence",
    "NotificationError",
    "NotificationSummary",
    "ValidationError",
)
