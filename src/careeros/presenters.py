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

from careeros.database.applications import ApplicationEvent, ApplicationListItem
from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.database.reminders import ReminderListEntry
from careeros.database.review import DecisionListItem, ReviewEvent
from careeros.ranking.explanations import explain_result, explanation_to_dict
from careeros.ranking.scorer import RankedGroup, RankedJob, component_to_dict
from careeros.review.service import QueueItem, ReviewedOpportunity
from careeros.saved_searches.models import SavedSearch
from careeros.today.models import TodayReminder, TodayShortlist

if TYPE_CHECKING:
    from careeros.reporting.models import (
        ActionItem,
        ApplicationSnapshot,
        ReviewQueueItem,
    )
    from careeros.source_stats.models import FieldCoverage, SourceStats
    from careeros.status import SourceStatus


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


def application_event_dict(event: ApplicationEvent, *, job_id: int) -> dict[str, Any]:
    """Serialize one append-only application status transition.

    The projection deliberately excludes the event note (user-written free
    text) and the internal application id: they are local workflow state the
    HTTP API never exposes. ``job_id`` is supplied by the caller because an
    :class:`ApplicationEvent` is keyed by ``application_id``, not ``jobs.id``.
    """
    return {
        "event_id": event.id,
        "job_id": job_id,
        "old_status": event.old_status,
        "new_status": event.new_status,
        "created_at": event.created_at,
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


def saved_search_item_dict(search: SavedSearch) -> dict[str, Any]:
    """Serialize one stored saved-search definition for the HTTP API.

    An explicit allow-listed projection of exactly the public definition
    fields: identity, name, mode, query, filter configuration, score/limit
    settings, and timestamps. ``profile_path`` (a local filesystem path) and
    ``description`` (unused user-authored free text) are structurally absent:
    the projection is built field-by-field and never serializes the dataclass
    wholesale, so private/internal fields can never leak into a response.
    """
    return {
        "id": search.id,
        "name": search.name,
        "mode": search.mode,
        "query": search.query,
        "grouped": search.grouped,
        "source": search.source,
        "remote": search.remote,
        "location": search.location,
        "untracked": search.untracked,
        "include_statuses": list(search.include_statuses),
        "exclude_statuses": list(search.exclude_statuses),
        "min_score": search.min_score,
        "result_limit": search.result_limit,
        "created_at": search.created_at,
        "updated_at": search.updated_at,
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

    A narrowly scoped public projection with the job, scores, and structured
    explanation. Raw reasons and score components remain private.
    """
    return {
        "job": job_dict(ranked.job),
        "match_score": ranked.match_score,
        "score": ranked.score,
        "explanation": explanation_to_dict(explain_result(ranked.result)),
    }


def ranked_group_item_dict(ranked: RankedGroup) -> dict[str, Any]:
    """Serialize one scored canonical group for the HTTP API.

    A narrowly scoped public projection with the canonical group, scores, and
    structured opportunity explanation. Workflow state and raw diagnostics
    remain private.
    """
    return {
        "group": group_dict(ranked.group),
        "match_score": ranked.match_score,
        "score": ranked.score,
        "explanation": explanation_to_dict(
            explain_result(
                ranked.result,
                scope="opportunity",
                listing_count=len(ranked.group.members),
            )
        ),
    }


def review_queue_item_dict(item: QueueItem) -> dict[str, Any]:
    """Serialize one ranked review-queue entry for the HTTP API.

    A narrowly scoped public queue projection with scores, workflow state, and
    the structured opportunity explanation. Private notes and raw diagnostics
    remain omitted.
    """
    return {
        "group": group_dict(item.group),
        "match_score": item.ranked.match_score,
        "score": item.ranked.score,
        "review_decision": item.group_decision.decision,
        "application_status": item.application.status,
        "explanation": explanation_to_dict(
            explain_result(
                item.ranked.result,
                scope="opportunity",
                listing_count=len(item.group.members),
            )
        ),
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


def reviewed_opportunity_item_dict(item: ReviewedOpportunity) -> dict[str, Any]:
    """Serialize a reviewed opportunity with its concrete decision owner."""
    return {
        "group": group_dict(item.group),
        "review_decision": item.group_decision.decision,
        "decision_job_id": item.decision_record.job_id,
        "decision_updated_at": item.decision_record.updated_at,
        "conflict": item.group_decision.conflict,
    }


def today_reminder_dict(item: TodayReminder) -> dict[str, Any]:
    """Serialize a public Today reminder, including same-job app context."""
    return {
        "reminder_id": item.reminder.id,
        "job_id": item.reminder.job_id,
        "kind": item.reminder.kind,
        "due_at": item.reminder.due_at,
        "completed_at": item.reminder.completed_at,
        "created_at": item.reminder.created_at,
        "updated_at": item.reminder.updated_at,
        "active_application_status": item.active_application_status,
        "job": job_dict(item.job),
    }


def today_shortlist_dict(item: TodayShortlist) -> dict[str, Any]:
    """Serialize a canonical unstarted shortlist opportunity."""
    return {
        "group": group_dict(item.group),
        "review_decision": "shortlist",
        "decision_job_id": item.decision_job_id,
        "decision_updated_at": item.decision_updated_at,
        "conflict": item.conflict,
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


def source_status_item_dict(status: SourceStatus) -> dict[str, Any]:
    """Serialize one source's collection status for the HTTP API.

    A privacy-pruned subset of the report's source-status projection: the
    provider ``last_error`` text is deliberately omitted because it can embed
    URLs, hostnames, local paths, or tracebacks, and lock/pid/command metadata
    is never carried by the domain status snapshot. Timestamps are ISO-8601
    strings (or ``None``), matching the report renderer.
    """
    return {
        "source": status.source,
        "min_interval_seconds": int(status.min_interval.total_seconds()),
        "documented": status.documented,
        "last_attempt": (
            status.last_attempt.isoformat() if status.last_attempt is not None else None
        ),
        "last_succeeded": status.last_succeeded,
        "fetched": status.last_fetched,
        "inserted": status.last_inserted,
        "updated": status.last_updated,
        "next_due": (
            status.next_due.isoformat() if status.next_due is not None else None
        ),
        "due": status.due,
        "incomplete_runs": status.incomplete_runs,
    }


def application_snapshot_dict(snapshot: ApplicationSnapshot) -> dict[str, Any]:
    """Serialize one recently-changed tracked application for the HTTP API.

    The ``latest_note`` (user-written free text) is deliberately omitted: it is
    local workflow state the HTTP API never exposes.
    """
    return {
        "job": job_dict(snapshot.job),
        "status": snapshot.application.current_status,
        "created_at": snapshot.application.created_at,
        "updated_at": snapshot.application.updated_at,
    }


def action_item_dict(item: ActionItem) -> dict[str, Any]:
    """Serialize one report action item for the HTTP API.

    The ``latest_note`` (user-written free text) is deliberately omitted: it is
    local workflow state the HTTP API never exposes.
    """
    return {
        "kind": item.kind,
        "label": item.label,
        "job": job_dict(item.job),
        "status": item.status,
        "updated_at": item.updated_at,
    }


def report_review_queue_item_dict(item: ReviewQueueItem) -> dict[str, Any]:
    """Serialize one report review-queue entry for the HTTP API.

    A narrowly scoped report projection with the canonical group, match score,
    and structured opportunity explanation. Raw diagnostics and feedback
    provenance remain private.
    """
    return {
        "group": group_dict(item.group),
        "match_score": item.match_score,
        "explanation": explanation_to_dict(
            explain_result(
                item.ranked.result,
                scope="opportunity",
                listing_count=len(item.group.members),
            )
        ),
    }


__all__: tuple[str, ...] = (
    "action_item_dict",
    "application_event_dict",
    "application_item_dict",
    "application_snapshot_dict",
    "field_coverage_dict",
    "group_dict",
    "job_dict",
    "location_display",
    "ranked_group_dict",
    "ranked_group_item_dict",
    "ranked_job_dict",
    "ranked_job_item_dict",
    "reminder_item_dict",
    "report_review_queue_item_dict",
    "review_decision_event_dict",
    "review_decision_item_dict",
    "reviewed_opportunity_item_dict",
    "today_reminder_dict",
    "today_shortlist_dict",
    "review_queue_item_dict",
    "saved_search_item_dict",
    "source_stats_dict",
    "source_status_item_dict",
)
