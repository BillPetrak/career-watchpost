"""Deterministic renderers for the Career Snapshot report.

Both the default text renderer and the JSON renderer consume the same immutable
:class:`Report` produced by :mod:`careeros.reporting.service`; no business logic
is reimplemented here. Text output is readable in a normal terminal and JSON is
sorted-key and independently deterministic.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.database.reminders import StoredReminder
from careeros.notifications.models import NotificationSummary
from careeros.ranking.feedback import FeedbackAdjustment
from careeros.ranking.scorer import (
    RankedGroup,
    RankedJob,
    component_to_dict,
    feedback_summary,
)
from careeros.reporting.models import (
    ACTION_ITEM_LABELS,
    ActionItem,
    ActionItems,
    ApplicationPipeline,
    ApplicationSnapshot,
    CollectionHealth,
    Overview,
    Recommendations,
    Reminders,
    Report,
    ReviewQueue,
)
from careeros.source_stats.models import (
    MISSING_FIELD_WORDING,
    FieldCoverage,
    SourceStats,
)
from careeros.status import SourceStatus

_SOURCE_OUTCOMES: dict[bool | None, str] = {
    True: "success",
    False: "failed",
    None: "unknown",
}


def render_report(report: Report) -> str:
    """Render the report as deterministic plain text (the default format)."""
    blocks = [
        _render_header(report),
        _render_overview(report.overview),
        _render_pipeline(report.pipeline),
        _render_actions(report.actions),
        _render_reminders(report.reminders),
        _render_review(report.review),
        _render_recommendations(report.recommendations),
        _render_health(report.health),
    ]
    return "\n\n".join(block for block in blocks if block) + "\n"


def render_report_json(report: Report) -> str:
    """Render the report as deterministic JSON carrying the same data."""
    return json.dumps(_report_to_dict(report), indent=2, sort_keys=True) + "\n"


def _render_header(report: Report) -> str:
    return "\n".join(
        [
            "Career Snapshot",
            "===============",
            f"As of: {report.now.isoformat()}",
        ]
    )


def _render_overview(overview: Overview) -> str:
    lines = [
        "Overview",
        "--------",
        f"Stored jobs:            {overview.total_jobs}",
        f"Canonical groups:       {overview.canonical_groups}",
        f"Grouped members:        {overview.grouped_members}",
        f"Distinct opportunities: {overview.distinct_opportunities}",
        f"Tracked applications:   {overview.tracked_applications}",
        f"Saved searches:         {overview.saved_searches}",
        "",
        "Review queue:",
        f"  Reviewed groups:      {overview.reviewed_groups}",
        f"  Unreviewed groups:    {overview.unreviewed_groups}",
        f"  Shortlisted:          {overview.shortlisted_groups}",
        f"  Dismissed:            {overview.dismissed_groups}",
        f"  Review later:         {overview.review_later_groups}",
        "",
        "Sources:",
    ]
    width = max((len(status.source) for status in overview.source_snapshots), default=8)
    for status in overview.source_snapshots:
        lines.append(f"  {_source_summary(status, width)}")
    lines.append("")
    due = ", ".join(overview.due_sources) if overview.due_sources else "none"
    lines.append(f"Sources due: {due}")
    lines.append("")
    lines.append(f"Source quality (fields {MISSING_FIELD_WORDING}):")
    for stats in overview.source_stats:
        lines.extend(_source_quality_lines(stats))
    return "\n".join(lines)


def _source_summary(status: SourceStatus, width: int) -> str:
    name = status.source.ljust(width)
    if status.last_attempt is None:
        return f"{name}  never run"
    outcome = _SOURCE_OUTCOMES.get(status.last_succeeded, "unknown")
    return f"{name}  last run {status.last_attempt.isoformat()}  {outcome}"


def _wrap_items(
    prefix: str,
    items: Sequence[str],
    *,
    width: int = 78,
    continuation: str = "      ",
) -> list[str]:
    """Wrap ``prefix`` plus comma-separated ``items`` across terminal lines.

    The first line starts with ``prefix``; when the running line would exceed
    ``width`` the remainder continues under ``continuation`` so wrapped output
    stays aligned and deterministic.
    """
    lines: list[str] = [prefix]
    for item in items:
        candidate = f"{lines[-1]}, {item}" if lines[-1] != prefix else f"{prefix}{item}"
        if len(candidate) > width and lines[-1] != prefix:
            lines.append(f"{continuation}{item}")
        else:
            lines[-1] = candidate
    return lines


def _source_quality_lines(stats: SourceStats) -> list[str]:
    """Render one source's quality summary (compact when nothing is missing)."""
    header = f"  {stats.source}: {stats.total_jobs} jobs"
    if stats.total_jobs == 0:
        return [header]
    if all(field.missing_count == 0 for field in stats.fields):
        return [
            f"{header}, never groupable: {stats.never_groupable_jobs}, "
            "no fields missing"
        ]

    lines = [f"{header}, never groupable: {stats.never_groupable_jobs}"]
    missing_items = [f"{field.field} {field.missing_count}" for field in stats.fields]
    lines.extend(_wrap_items(f"    {MISSING_FIELD_WORDING}: ", missing_items))
    coverage_items = [
        (
            f"{field.field} {field.coverage_pct}%"
            if field.coverage_pct is not None
            else f"{field.field} -"
        )
        for field in stats.fields
    ]
    lines.extend(_wrap_items("    coverage: ", coverage_items))
    return lines


def _render_pipeline(pipeline: ApplicationPipeline) -> str:
    lines = [
        "Application pipeline",
        "--------------------",
    ]
    for status, count in pipeline.status_counts:
        lines.append(f"  {status + ':':<11} {count}")
    lines.append("")
    lines.append("Recently changed:")
    if not pipeline.recently_changed:
        lines.append("  none")
    else:
        for snapshot in pipeline.recently_changed:
            lines.append(f"  {_snapshot_line(snapshot)}")
            if snapshot.latest_note is not None:
                lines.append(f"      Note: {snapshot.latest_note}")
    return "\n".join(lines)


def _snapshot_line(snapshot: ApplicationSnapshot) -> str:
    job = snapshot.job
    return (
        f"- [{job.id}] {job.title} ({job.company or 'N/A'}, {job.source}): "
        f"{snapshot.application.current_status.capitalize()}, "
        f"updated {snapshot.application.updated_at}"
    )


def _render_actions(actions: ActionItems) -> str:
    lines = [
        "Action items",
        "------------",
    ]
    for items, kind in (
        (actions.interview, "interview"),
        (actions.offer, "offer"),
        (actions.interested, "interested"),
        (actions.awaiting_progress, "applied"),
    ):
        lines.append(f"{ACTION_ITEM_LABELS[kind]}:")
        if not items:
            lines.append("  none")
        else:
            for item in items:
                lines.append(f"  {_action_line(item)}")
                if item.latest_note is not None:
                    lines.append(f"      Note: {item.latest_note}")
        lines.append("")
    lines.append("CareerOS never infers deadlines or follow-up dates from text.")
    return "\n".join(lines)


def _action_line(item: ActionItem) -> str:
    job = item.job
    return f"- [{job.id}] {job.title} ({job.company or 'N/A'}, {job.source})"


def _render_reminders(reminders: Reminders) -> str:
    notifications = reminders.notifications
    lines = [
        "Reminders",
        "---------",
        f"Overdue:         {reminders.overdue_count}",
        f"Due today (UTC): {reminders.due_today_utc_count}",
        f"Open total:      {reminders.open_total}",
        "Upcoming (soonest first):",
    ]
    if not reminders.upcoming:
        lines.append("  none")
    else:
        for snapshot in reminders.upcoming:
            reminder = snapshot.reminder
            job = snapshot.job
            lines.append(
                f"  - [{job.id}] {job.title} ({job.company or 'N/A'}, "
                f"{job.source}): {reminder.kind}, due {reminder.due_at}"
            )
            if reminder.message is not None:
                lines.append(f"      Message: {reminder.message}")
    lines.append("Notifications:")
    lines.append(f"  Pending due:    {notifications.pending_due}")
    lines.append(f"  Delivered total: {notifications.delivered_total}")
    lines.append(f"  Failed total:    {notifications.failed_total}")
    lines.append(f"  Last delivered:  {notifications.last_delivered_at or 'never'}")
    lines.append(f"  Last failed:     {notifications.last_failed_at or 'never'}")
    lines.append(
        "Reminder times are stored and compared in UTC. "
        "Due today (UTC) means within the current UTC calendar day; "
        "CareerOS does not assume your local timezone."
    )
    return "\n".join(lines)


def _render_review(review: ReviewQueue) -> str:
    lines = [
        "Review queue",
        "------------",
    ]
    if review.source == "none":
        lines.append(review.note or "No review queue computed.")
        return "\n".join(lines)

    if review.source == "saved-search":
        lines.append(f"Profile: saved search {review.source_name!r}")
    else:
        lines.append("Profile: command-line --profile")
    lines.append(f"Mode:    {'grouped' if review.grouped else 'flat'}")

    if review.candidate_count == 0:
        lines.append("No unreviewed opportunities.")
        return "\n".join(lines)

    if not review.items:
        lines.append("No unreviewed opportunities found.")
        return "\n".join(lines)

    noun = "group" if review.grouped else "job"
    plural = "s" if len(review.items) != 1 else ""
    lines.append(f"Ranked {len(review.items)} unreviewed {noun}{plural}:")
    for index, item in enumerate(review.items, start=1):
        group = item.group
        rep = group.representative
        lines.append(f"  {index}. [Score {item.match_score}/100] {rep.title}")
        lines.append(f"     Company:  {rep.company or 'N/A'}")
        lines.append(
            f"     Location: {_location_display(rep.location, rep.remote_status)}"
        )
        lines.append(f"     Source:   {', '.join(group.sources)}")
        if len(group.members) > 1:
            lines.append(f"     Group:    {len(group.members)} listings grouped")
        lines.extend(_feedback_lines(item.feedback))
        lines.extend(_reason_lines(item.reasons))
    return "\n".join(lines)


def _render_recommendations(recommendations: Recommendations) -> str:
    lines = [
        "Recommended opportunities",
        "-------------------------",
    ]
    if recommendations.source == "none":
        lines.append(recommendations.note or "No recommendations computed.")
        return "\n".join(lines)

    if recommendations.source == "saved-search":
        lines.append(f"Profile: saved search {recommendations.source_name!r}")
    else:
        lines.append("Profile: command-line --profile")

    if recommendations.candidate_count == 0:
        lines.append("No matching jobs found.")
        return "\n".join(lines)

    if recommendations.grouped:
        groups = recommendations.ranked_groups
        if not groups:
            lines.append(_no_results_line(recommendations))
            return "\n".join(lines)
        count = len(groups)
        lines.append(f"Ranked {count} group{'s' if count != 1 else ''}:")
        for index, entry in enumerate(groups, start=1):
            lines.extend(_ranked_group_lines(index, entry))
        return "\n".join(lines)

    jobs = recommendations.ranked_jobs
    if not jobs:
        lines.append(_no_results_line(recommendations))
        return "\n".join(lines)
    count = len(jobs)
    lines.append(f"Ranked {count} job{'s' if count != 1 else ''}:")
    for index, job in enumerate(jobs, start=1):
        lines.extend(_ranked_job_lines(index, job))
    return "\n".join(lines)


def _no_results_line(recommendations: Recommendations) -> str:
    if recommendations.min_score > 0:
        noun = "groups" if recommendations.grouped else "jobs"
        return f"No {noun} meet the minimum score of {recommendations.min_score}."
    return "No matching jobs found."


def _ranked_job_lines(index: int, ranked: RankedJob) -> list[str]:
    job = ranked.job
    lines = [
        f"  {index}. [Score {ranked.match_score}/100] {job.title}",
        f"     Company:  {job.company or 'N/A'}",
        f"     Location: {_location_display(job.location, job.remote_status)}",
        f"     Source:   {job.source}",
    ]
    lines.extend(_feedback_lines(ranked.result.feedback))
    lines.extend(_reason_lines(ranked.reasons))
    return lines


def _ranked_group_lines(index: int, ranked: RankedGroup) -> list[str]:
    group = ranked.group
    rep = group.representative
    lines = [
        f"  {index}. [Score {ranked.match_score}/100] {rep.title}",
        f"     Company:  {rep.company or 'N/A'}",
        f"     Location: {_location_display(rep.location, rep.remote_status)}",
        f"     Source:   {', '.join(group.sources)}",
    ]
    if len(group.members) > 1:
        lines.append(f"     Group:    {len(group.members)} listings grouped")
    lines.extend(_feedback_lines(ranked.result.feedback))
    lines.extend(_reason_lines(ranked.reasons))
    return lines


def _reason_lines(reasons: tuple[str, ...]) -> list[str]:
    lines = ["     Reasons:"]
    if reasons:
        lines.extend(f"       {reason}" for reason in reasons)
    else:
        lines.append("       none")
    return lines


def _feedback_lines(feedback: FeedbackAdjustment | None) -> list[str]:
    """Return the text ``Feedback:`` line for an adjustment, or nothing."""
    if feedback is None or feedback.points == 0:
        return []
    return [f"Feedback: {feedback_summary(feedback)}"]


def _location_display(location: str | None, remote_status: str | None) -> str:
    value = location or "N/A"
    if remote_status == "remote":
        return f"{value} (remote)"
    return value


def _render_health(health: CollectionHealth) -> str:
    lines = [
        "Collection health",
        "-----------------",
    ]
    for status in health.per_source:
        lines.append(status.source)
        if status.last_attempt is None:
            lines.append("  Last run:    never")
            lines.append("  Status:      never")
            lines.append("  Fetched:     -")
            lines.append("  Inserted:    -")
            lines.append("  Updated:     -")
        else:
            lines.append(f"  Last run:    {status.last_attempt.isoformat()}")
            outcome = _SOURCE_OUTCOMES.get(status.last_succeeded, "unknown")
            lines.append(f"  Status:      {outcome}")
            lines.append(f"  Fetched:     {status.last_fetched}")
            lines.append(f"  Inserted:    {status.last_inserted}")
            lines.append(f"  Updated:     {status.last_updated}")
        if status.last_error is not None:
            lines.append(f"  Error:       {status.last_error}")
        next_due = status.next_due.isoformat() if status.next_due is not None else "now"
        lines.append(f"  Next due:    {next_due}")
        lines.append(f"  Incomplete:  {status.incomplete_runs}")
    return "\n".join(lines)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _job_dict(job: StoredJob) -> dict[str, Any]:
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


def _source_status_dict(status: SourceStatus) -> dict[str, Any]:
    return {
        "source": status.source,
        "min_interval_seconds": int(status.min_interval.total_seconds()),
        "documented": status.documented,
        "last_attempt": _iso(status.last_attempt),
        "last_succeeded": status.last_succeeded,
        "fetched": status.last_fetched,
        "inserted": status.last_inserted,
        "updated": status.last_updated,
        "last_error": status.last_error,
        "next_due": _iso(status.next_due),
        "due": status.due,
        "incomplete_runs": status.incomplete_runs,
    }


def _field_coverage_dict(field: FieldCoverage) -> dict[str, Any]:
    return {
        "field": field.field,
        "missing_count": field.missing_count,
        "provided_count": field.provided_count,
        "coverage_pct": field.coverage_pct,
    }


def _source_stats_dict(stats: SourceStats) -> dict[str, Any]:
    return {
        "source": stats.source,
        "total_jobs": stats.total_jobs,
        "never_groupable_jobs": stats.never_groupable_jobs,
        "fields": [_field_coverage_dict(field) for field in stats.fields],
    }


def _snapshot_dict(snapshot: ApplicationSnapshot) -> dict[str, Any]:
    return {
        "job": _job_dict(snapshot.job),
        "status": snapshot.application.current_status,
        "created_at": snapshot.application.created_at,
        "updated_at": snapshot.application.updated_at,
        "latest_note": snapshot.latest_note,
    }


def _action_dict(item: ActionItem) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "label": item.label,
        "job": _job_dict(item.job),
        "status": item.status,
        "updated_at": item.updated_at,
        "latest_note": item.latest_note,
    }


def _reminder_dict(reminder: StoredReminder) -> dict[str, Any]:
    return {
        "id": reminder.id,
        "job_id": reminder.job_id,
        "kind": reminder.kind,
        "message": reminder.message,
        "due_at": reminder.due_at,
        "completed_at": reminder.completed_at,
        "created_at": reminder.created_at,
        "updated_at": reminder.updated_at,
    }


def _notification_summary_dict(summary: NotificationSummary) -> dict[str, Any]:
    return {
        "pending_due": summary.pending_due,
        "delivered_total": summary.delivered_total,
        "failed_total": summary.failed_total,
        "last_delivered_at": summary.last_delivered_at,
        "last_failed_at": summary.last_failed_at,
    }


def _ranked_job_dict(ranked: RankedJob) -> dict[str, Any]:
    return {
        "job": _job_dict(ranked.job),
        "match_score": ranked.match_score,
        "score": ranked.score,
        "reasons": list(ranked.reasons),
        "score_components": [
            component_to_dict(component) for component in ranked.result.components
        ],
    }


def _group_dict(group: GroupedJob) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "representative": _job_dict(group.representative),
        "members": [_job_dict(member) for member in group.members],
        "sources": list(group.sources),
        "urls": list(group.urls),
    }


def _ranked_group_dict(ranked: RankedGroup) -> dict[str, Any]:
    return {
        "group": _group_dict(ranked.group),
        "match_score": ranked.match_score,
        "score": ranked.score,
        "reasons": list(ranked.reasons),
        "score_components": [
            component_to_dict(component) for component in ranked.result.components
        ],
    }


def _report_to_dict(report: Report) -> dict[str, Any]:
    overview = report.overview
    pipeline = report.pipeline
    actions = report.actions
    review = report.review
    recommendations = report.recommendations
    return {
        "as_of": report.now.isoformat(),
        "overview": {
            "total_jobs": overview.total_jobs,
            "canonical_groups": overview.canonical_groups,
            "grouped_members": overview.grouped_members,
            "distinct_opportunities": overview.distinct_opportunities,
            "tracked_applications": overview.tracked_applications,
            "saved_searches": overview.saved_searches,
            "reviewed_groups": overview.reviewed_groups,
            "unreviewed_groups": overview.unreviewed_groups,
            "shortlisted_groups": overview.shortlisted_groups,
            "dismissed_groups": overview.dismissed_groups,
            "review_later_groups": overview.review_later_groups,
            "sources": [
                _source_status_dict(status) for status in overview.source_snapshots
            ],
            "due_sources": list(overview.due_sources),
            "source_stats": [
                _source_stats_dict(stats) for stats in overview.source_stats
            ],
        },
        "application_pipeline": {
            "status_counts": dict(pipeline.status_counts),
            "recently_changed": [
                _snapshot_dict(snapshot) for snapshot in pipeline.recently_changed
            ],
        },
        "action_items": {
            "interview": [_action_dict(item) for item in actions.interview],
            "offer": [_action_dict(item) for item in actions.offer],
            "interested": [_action_dict(item) for item in actions.interested],
            "awaiting_progress": [
                _action_dict(item) for item in actions.awaiting_progress
            ],
        },
        "reminders": {
            "overdue_count": report.reminders.overdue_count,
            "due_today_utc_count": report.reminders.due_today_utc_count,
            "open_total": report.reminders.open_total,
            "upcoming": [
                {
                    "reminder": _reminder_dict(snapshot.reminder),
                    "job": _job_dict(snapshot.job),
                }
                for snapshot in report.reminders.upcoming
            ],
            "notification_summary": _notification_summary_dict(
                report.reminders.notifications
            ),
        },
        "recommendations": {
            "grouped": recommendations.grouped,
            "source": recommendations.source,
            "source_name": recommendations.source_name,
            "min_score": recommendations.min_score,
            "candidate_count": recommendations.candidate_count,
            "note": recommendations.note,
            "ranked_jobs": [
                _ranked_job_dict(ranked) for ranked in recommendations.ranked_jobs
            ],
            "ranked_groups": [
                _ranked_group_dict(ranked) for ranked in recommendations.ranked_groups
            ],
        },
        "review_queue": {
            "grouped": review.grouped,
            "source": review.source,
            "source_name": review.source_name,
            "candidate_count": review.candidate_count,
            "note": review.note,
            "items": [
                {
                    "group": _group_dict(item.group),
                    "match_score": item.match_score,
                    "reasons": list(item.reasons),
                    "score_components": [
                        component_to_dict(component) for component in item.components
                    ],
                }
                for item in review.items
            ],
        },
        "collection_health": {
            "sources": [
                _source_status_dict(status) for status in report.health.per_source
            ],
        },
    }
