"""Immutable data model for the Career Snapshot report.

The report is a read-only snapshot over stored jobs, application-tracking
state, saved searches, and collection history. Dataclasses here are
presentation-agnostic: they hold values only, never connections or printers.
The service layer (:mod:`careeros.reporting.service`) builds a :class:`Report`
from the database, and :mod:`careeros.reporting.render` turns it into text or
JSON. Nothing here ever writes to the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from careeros.database.applications import StoredApplication
from careeros.database.jobs import StoredJob
from careeros.database.reminders import StoredReminder
from careeros.notifications.models import NotificationSummary
from careeros.ranking.scorer import RankedGroup, RankedJob
from careeros.status import SourceStatus

# User-facing headings for the active application-pipeline categories, shared
# by the service layer (which labels each ActionItem) and the renderer (which
# prints a heading even when a category has no items).
ACTION_ITEM_LABELS: dict[str, str] = {
    "interview": "Interview attention",
    "offer": "Offer attention",
    "interested": "Interested, not applied",
    "applied": "Applied, awaiting progress",
}


@dataclass(frozen=True)
class Overview:
    """Top-level counters and collection snapshot for the report."""

    total_jobs: int
    canonical_groups: int
    grouped_members: int
    distinct_opportunities: int
    tracked_applications: int
    saved_searches: int
    source_snapshots: tuple[SourceStatus, ...]
    due_sources: tuple[str, ...]


@dataclass(frozen=True)
class ApplicationSnapshot:
    """One tracked job with its current state and latest note."""

    job: StoredJob
    application: StoredApplication
    latest_note: str | None


@dataclass(frozen=True)
class ApplicationPipeline:
    """Status counts and the most recently changed applications."""

    status_counts: tuple[tuple[str, int], ...]
    recently_changed: tuple[ApplicationSnapshot, ...]


@dataclass(frozen=True)
class ActionItem:
    """A single active action surfaced by the report."""

    kind: str
    label: str
    job: StoredJob
    status: str
    updated_at: str
    latest_note: str | None


@dataclass(frozen=True)
class ActionItems:
    """Action items grouped by category, in prominence order."""

    interview: tuple[ActionItem, ...]
    offer: tuple[ActionItem, ...]
    interested: tuple[ActionItem, ...]
    awaiting_progress: tuple[ActionItem, ...]


@dataclass(frozen=True)
class Recommendations:
    """Profile-ranked opportunities for the report.

    ``source`` is ``"profile"`` (command-line ``--profile``), ``"saved-search"``
    (a rank-mode saved search), or ``"none"``. ``note`` carries a human message
    when no recommendations could be computed.
    """

    grouped: bool
    source: str
    source_name: str | None
    ranked_jobs: tuple[RankedJob, ...]
    ranked_groups: tuple[RankedGroup, ...]
    candidate_count: int
    min_score: int
    note: str | None


@dataclass(frozen=True)
class ReminderSnapshot:
    """One open reminder joined with its stored job for the report."""

    reminder: StoredReminder
    job: StoredJob


@dataclass(frozen=True)
class Reminders:
    """Reminder counts and upcoming action items for the report.

    ``overdue_count`` counts open reminders whose due time has passed.
    ``due_today_utc_count`` counts open reminders whose due time falls within
    the current UTC calendar day; CareerOS does not know the user's local
    timezone, so "today" is defined and reported in UTC only. ``upcoming`` is
    the open reminders due at or after the report's ``now``, ordered by
    urgency (``due_at ASC`` then ``id ASC``), capped by the report limit.
    ``notifications`` carries the durable delivery summary.
    """

    overdue_count: int
    due_today_utc_count: int
    open_total: int
    upcoming: tuple[ReminderSnapshot, ...]
    notifications: NotificationSummary


@dataclass(frozen=True)
class CollectionHealth:
    """Per-source collection health, including incomplete runs."""

    per_source: tuple[SourceStatus, ...]


@dataclass(frozen=True)
class Report:
    """The complete Career Snapshot, immutable and deterministic."""

    now: datetime
    overview: Overview
    pipeline: ApplicationPipeline
    actions: ActionItems
    reminders: Reminders
    recommendations: Recommendations
    health: CollectionHealth
