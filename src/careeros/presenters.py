"""Shared presentation and serialization helpers.

Every helper here is a pure projection of stored CareerOS domain objects into
display or JSON structures, with the exact key order and values the existing
CLI and renderers rely on. They are shared by the command-line interface
(:mod:`careeros.cli`), the Career Snapshot report renderers
(:mod:`careeros.reporting.render`), the review-queue renderer
(:mod:`careeros.review.render`), and the source-statistics renderer
(:mod:`careeros.source_stats.render`) so that identical shapes are produced in
exactly one place.

The richer export record schemas in :mod:`careeros.export.service` intentionally
do NOT use these helpers: export records carry the full stored-job columns plus
export-specific metadata (``group_id``, ``member_count``, ``rank``, ...) and
are defined by their own deterministic contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from careeros.database.applications import ApplicationListItem
from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.database.reminders import ReminderListEntry
from careeros.database.review import DecisionListItem, ReviewEvent
from careeros.ranking.scorer import RankedGroup, RankedJob, component_to_dict
from careeros.review.service import QueueItem

if TYPE_CHECKING:
    from careeros.source_stats.models import FieldCoverage, SourceStats


def job_dict(job: StoredJob) -> dict[str, Any]:
    """Serialize a stored job with the stable CareerOS job keys."""
    return {
        "id": job.id,
        "source": job.source,
        "source_job_id": job.source_job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "remote_status": job.remote_status,
        "employment_type": job.employment_type,
        "published_at": job.published_at,
        "url": job.url,
    }


def application_item_dict(item: ApplicationListItem) -> dict[str, Any]:
    """Serialize one tracked application joined with its job.

    The projection deliberately excludes the application note, the event
    history, and any other local workflow state (reminders, review decisions):
    those stay private to the CLI and are never exposed over the HTTP API.
    """
    return {
        "application_id": item.application.id,
        "job_id": item.application.job_id,
        "current_status": item.application.current_status,
        "created_at": item.application.created_at,
        "updated_at": item.application.updated_at,
        "job": job_dict(item.job),
    }


def reminder_item_dict(entry: ReminderListEntry) -> dict[str, Any]:
    """Serialize one stored reminder joined with its job.

    The projection deliberately excludes the reminder message (user-written
    free text), notification delivery history, and any other local workflow
    state: those stay private to the CLI and are never exposed over the HTTP
    API.
    """
    return {
        "reminder_id": entry.reminder.id,
        "job_id": entry.reminder.job_id,
        "kind": entry.reminder.kind,
        "due_at": entry.reminder.due_at,
        "completed_at": entry.reminder.completed_at,
        "created_at": entry.reminder.created_at,
        "updated_at": entry.reminder.updated_at,
        "job": job_dict(entry.job),
    }


def group_dict(group: GroupedJob) -> dict[str, Any]:
    """Serialize a canonical group with the stable CareerOS group keys."""
    return {
        "group_id": group.group_id,
        "representative": job_dict(group.representative),
        "members": [job_dict(member) for member in group.members],
        "sources": list(group.sources),
        "urls": list(group.urls),
    }


def ranked_job_dict(ranked: RankedJob) -> dict[str, Any]:
    """Serialize one scored flat result with its component breakdown."""
    return {
        "job": job_dict(ranked.job),
        "match_score": ranked.match_score,
        "score": ranked.score,
        "reasons": list(ranked.reasons),
        "score_components": [
            component_to_dict(component) for component in ranked.result.components
        ],
    }


def ranked_group_dict(ranked: RankedGroup) -> dict[str, Any]:
    """Serialize one scored grouped result with its component breakdown."""
    return {
        "group": group_dict(ranked.group),
        "match_score": ranked.match_score,
        "score": ranked.score,
        "reasons": list(ranked.reasons),
        "score_components": [
            component_to_dict(component) for component in ranked.result.components
        ],
    }


def ranked_job_item_dict(ranked: RankedJob) -> dict[str, Any]:
    """Serialize one scored flat result for the HTTP API.

    A narrowly scoped subset of ``ranked_job_dict``: the HTTP API exposes only
    the job and its two score values. ``reasons`` and ``score_components`` are
    deliberately omitted because they can echo submitted profile terms or embed
    private review-feedback text, which the HTTP API never exposes.
    """
    return {
        "job": job_dict(ranked.job),
        "match_score": ranked.match_score,
        "score": ranked.score,
    }


def ranked_group_item_dict(ranked: RankedGroup) -> dict[str, Any]:
    """Serialize one scored canonical group for the HTTP API.

    A narrowly scoped subset of ``ranked_group_dict``: the HTTP API exposes
    only the canonical group and its two score values. ``reasons`` and
    ``score_components`` are deliberately omitted because they can echo
    submitted profile terms or embed private review-feedback text, which the
    HTTP API never exposes. Review decisions, application status, notes, and
    reminder messages are likewise never exposed.
    """
    return {
        "group": group_dict(ranked.group),
        "match_score": ranked.match_score,
        "score": ranked.score,
    }


def review_queue_item_dict(item: QueueItem) -> dict[str, Any]:
    """Serialize one ranked review-queue entry for the HTTP API.

    A narrowly scoped subset of the review renderer's queue-item shape: the
    canonical group, the two score values, and the resolved group decision and
    application status. ``reasons`` and ``score_components`` are deliberately
    omitted because they can echo submitted profile terms or embed private
    review-feedback text, which the HTTP API never exposes. Decision notes,
    application notes, conflict/owner detail, and reminder messages are
    likewise never exposed.
    """
    return {
        "group": group_dict(item.group),
        "match_score": item.ranked.match_score,
        "score": item.ranked.score,
        "review_decision": item.group_decision.decision,
        "application_status": item.application.status,
    }


def review_decision_item_dict(item: DecisionListItem) -> dict[str, Any]:
    """Serialize one current review decision joined with its job.

    The projection deliberately excludes the latest review note (user-written
    free text) and the decision event history: those are local workflow state
    the HTTP API never exposes.
    """
    return {
        "decision_id": item.decision.id,
        "job_id": item.decision.job_id,
        "decision": item.decision.decision,
        "created_at": item.decision.created_at,
        "updated_at": item.decision.updated_at,
        "job": job_dict(item.job),
    }


def review_decision_event_dict(event: ReviewEvent) -> dict[str, Any]:
    """Serialize one append-only review-decision transition.

    The projection deliberately excludes the event note (user-written free
    text): it is local workflow state the HTTP API never exposes.
    """
    return {
        "event_id": event.id,
        "job_id": event.job_id,
        "old_decision": event.old_decision,
        "new_decision": event.new_decision,
        "created_at": event.created_at,
    }


def location_display(location: str | None, remote_status: str | None) -> str:
    """Render a job location for display, appending ``(remote)`` when remote.

    Matches the long-standing display convention used by search, rank, review,
    and report text output: absent locations render as ``N/A`` and remote jobs
    are annotated inline.
    """
    value = location or "N/A"
    if remote_status == "remote":
        return f"{value} (remote)"
    return value


def field_coverage_dict(field: FieldCoverage) -> dict[str, Any]:
    """Serialize one per-field coverage statistic."""
    return {
        "field": field.field,
        "missing_count": field.missing_count,
        "provided_count": field.provided_count,
        "coverage_pct": field.coverage_pct,
    }


def source_stats_dict(stats: SourceStats) -> dict[str, Any]:
    """Serialize one per-source statistics entry."""
    return {
        "source": stats.source,
        "total_jobs": stats.total_jobs,
        "never_groupable_jobs": stats.never_groupable_jobs,
        "fields": [field_coverage_dict(field) for field in stats.fields],
    }


__all__: tuple[str, ...] = (
    "application_item_dict",
    "field_coverage_dict",
    "group_dict",
    "job_dict",
    "location_display",
    "ranked_group_dict",
    "ranked_group_item_dict",
    "ranked_job_dict",
    "ranked_job_item_dict",
    "reminder_item_dict",
    "review_decision_event_dict",
    "review_decision_item_dict",
    "review_queue_item_dict",
    "source_stats_dict",
)
