"""Read-only service that builds a Career Snapshot report.

All queries here are SELECTs against existing tables; nothing writes, commits
new rows, or mutates stored state. The service reuses the existing status,
grouping, ranking, application, and saved-search layers rather than
reimplementing any of them. A single injected ``now`` drives every time-based
calculation so the output is deterministic and testable.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from careeros.applications.models import APPLICATION_STATUSES
from careeros.database.applications import StoredApplication
from careeros.database.groups import grouping_summary
from careeros.database.jobs import JOB_COLUMNS, job_from_row
from careeros.notifications.service import notification_summary
from careeros.ranking.feedback import FeedbackSet
from careeros.ranking.scorer import CandidateProfile
from careeros.reminders.service import (
    count_due_today_reminders,
    count_open_reminders,
    count_overdue_reminders,
    list_upcoming_reminders,
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
    ReminderSnapshot,
    Report,
    ReviewQueue,
    ReviewQueueItem,
)
from careeros.review.service import build_queue, review_group_summary
from careeros.saved_searches.execution import execute_rank, rank_candidates
from careeros.saved_searches.models import SavedSearch
from careeros.saved_searches.service import list_searches
from careeros.source_stats.service import compute_source_stats
from careeros.status import build_statuses

_JOB_COLUMNS: tuple[str, ...] = tuple(JOB_COLUMNS.split(", "))


def _counts_by_status(connection: sqlite3.Connection) -> dict[str, int]:
    """Return one count per application status, zero-filled for absent ones."""
    rows = connection.execute(
        """
        SELECT current_status, COUNT(*) AS n
        FROM applications
        GROUP BY current_status
        """
    ).fetchall()
    counts = {str(row["current_status"]): int(row["n"]) for row in rows}
    return {status: counts.get(status, 0) for status in APPLICATION_STATUSES}


def _list_tracked(connection: sqlite3.Connection) -> list[ApplicationSnapshot]:
    """Return tracked applications joined with their jobs, newest first.

    Ordering is deterministic: ``updated_at DESC`` then application ``id DESC``.
    Each snapshot carries the latest non-empty event note (same subquery the
    existing application listing uses), so the report never duplicates note
    semantics.
    """
    rows = connection.execute(
        f"""
        SELECT a.id AS app_id,
               a.job_id AS job_id,
               a.current_status AS current_status,
               a.created_at AS app_created_at,
               a.updated_at AS app_updated_at,
               {", ".join(f"j.{column}" for column in _JOB_COLUMNS)},
               (SELECT e.note
                FROM application_events e
                WHERE e.application_id = a.id AND e.note IS NOT NULL
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT 1) AS latest_note
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        ORDER BY a.updated_at DESC, a.id DESC
        """
    ).fetchall()

    snapshots: list[ApplicationSnapshot] = []
    for row in rows:
        snapshots.append(
            ApplicationSnapshot(
                job=job_from_row(row),
                application=StoredApplication(
                    id=row["app_id"],
                    job_id=row["job_id"],
                    current_status=row["current_status"],
                    created_at=row["app_created_at"],
                    updated_at=row["app_updated_at"],
                ),
                latest_note=row["latest_note"],
            )
        )
    return snapshots


def _action_items(
    tracked: list[ApplicationSnapshot],
    *,
    status: str,
    limit: int,
) -> tuple[ActionItem, ...]:
    """Classify tracked applications into one action category, capped."""
    items = [
        ActionItem(
            kind=status,
            label=ACTION_ITEM_LABELS[status],
            job=snapshot.job,
            status=snapshot.application.current_status,
            updated_at=snapshot.application.updated_at,
            latest_note=snapshot.latest_note,
        )
        for snapshot in tracked
        if snapshot.application.current_status == status
    ]
    return tuple(items[:limit])


def _build_recommendations(
    connection: sqlite3.Connection,
    *,
    profile: CandidateProfile | None,
    saved_search: SavedSearch | None,
    limit: int,
    grouped: bool,
    feedback: FeedbackSet | None,
) -> Recommendations:
    """Score opportunities for the report using the shared ranking engine.

    A saved rank search governs its own query, filters, min score, and result
    limit (the report's ``limit`` only ever trims afterwards). Without a saved
    search, ``--profile`` ranks every untracked job. Without either, the
    section is honestly skipped with a note. ``feedback`` is shared with the
    review queue so the database is queried once per report.
    """
    if saved_search is not None:
        assert profile is not None
        outcome = execute_rank(
            connection, saved_search, profile=profile, feedback=feedback
        )
        min_score = saved_search.min_score or 0
        if outcome.grouped:
            return Recommendations(
                grouped=True,
                source="saved-search",
                source_name=saved_search.name,
                ranked_jobs=(),
                ranked_groups=tuple(outcome.grouped_ranked[:limit]),
                candidate_count=outcome.grouped_candidate_count,
                min_score=min_score,
                note=None,
            )
        return Recommendations(
            grouped=False,
            source="saved-search",
            source_name=saved_search.name,
            ranked_jobs=tuple(outcome.flat_ranked[:limit]),
            ranked_groups=(),
            candidate_count=outcome.flat_candidate_count,
            min_score=min_score,
            note=None,
        )

    if profile is not None:
        outcome = rank_candidates(
            connection,
            query="",
            profile=profile,
            grouped=grouped,
            untracked=True,
            limit=limit,
            feedback=feedback,
        )
        if outcome.grouped:
            return Recommendations(
                grouped=True,
                source="profile",
                source_name=None,
                ranked_jobs=(),
                ranked_groups=tuple(outcome.grouped_ranked),
                candidate_count=outcome.grouped_candidate_count,
                min_score=0,
                note=None,
            )
        return Recommendations(
            grouped=False,
            source="profile",
            source_name=None,
            ranked_jobs=tuple(outcome.flat_ranked),
            ranked_groups=(),
            candidate_count=outcome.flat_candidate_count,
            min_score=0,
            note=None,
        )

    return Recommendations(
        grouped=False,
        source="none",
        source_name=None,
        ranked_jobs=(),
        ranked_groups=(),
        candidate_count=0,
        min_score=0,
        note="No profile or saved search provided; no recommendations computed.",
    )


def _build_reminders(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    limit: int,
) -> Reminders:
    """Assemble the reminder section from the read-only reminder service.

    The reminder service owns every query and all time logic; this function
    only shapes the results into the report model.
    """
    entries = list_upcoming_reminders(connection, now=now, limit=limit)
    snapshots = tuple(
        ReminderSnapshot(reminder=entry.reminder, job=entry.job) for entry in entries
    )
    return Reminders(
        overdue_count=count_overdue_reminders(connection, now=now),
        due_today_utc_count=count_due_today_reminders(connection, now=now),
        open_total=count_open_reminders(connection),
        upcoming=snapshots,
        notifications=notification_summary(connection, now=now),
    )


def _build_review_queue(
    connection: sqlite3.Connection,
    *,
    profile: CandidateProfile | None,
    saved_search: SavedSearch | None,
    limit: int,
    grouped: bool,
    feedback: FeedbackSet | None,
) -> ReviewQueue:
    """Rank the daily review queue for the report (read-only).

    The queue reuses the shared review ``build_queue`` service with the same
    defaults as ``careeros review next``: unreviewed opportunities only, tracked
    applications excluded, scored with the report's effective profile. A saved
    search supplies the profile but not its query/filters, keeping the queue a
    pure personalized daily feed rather than a stored-workflow rerun. Without a
    profile the section is honestly skipped with a note. ``feedback`` is shared
    with the recommendations so the database is queried once per report.
    """
    if profile is None:
        return ReviewQueue(
            grouped=grouped,
            source="none",
            source_name=None,
            items=(),
            candidate_count=0,
            note="No profile or saved search provided; no review queue computed.",
        )

    outcome = build_queue(
        connection,
        profile=profile,
        state="unreviewed",
        min_score=0,
        include_tracked=False,
        limit=limit,
        grouped=grouped,
        feedback=feedback,
    )

    if saved_search is not None:
        source, source_name = "saved-search", saved_search.name
    else:
        source, source_name = "profile", None

    return ReviewQueue(
        grouped=grouped,
        source=source,
        source_name=source_name,
        items=tuple(
            ReviewQueueItem(
                group=item.group,
                match_score=item.ranked.match_score,
                reasons=item.ranked.reasons,
                components=item.ranked.result.components,
                feedback=item.ranked.result.feedback,
            )
            for item in outcome.items
        ),
        candidate_count=outcome.candidate_count,
        note=None,
    )


def build_report(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    profile: CandidateProfile | None = None,
    saved_search: SavedSearch | None = None,
    limit: int = 10,
    grouped: bool = False,
    feedback: FeedbackSet | None = None,
) -> Report:
    """Build a complete Career Snapshot from stored state (read-only).

    ``now`` is captured once by the caller and used for every collection
    status and due calculation in the report. ``feedback`` carries the
    review-feedback set shared by the review queue and the recommendations
    (built once by the caller); None disables review feedback.
    """
    source_snapshots = tuple(build_statuses(connection, now=now))
    due_sources = tuple(status.source for status in source_snapshots if status.due)
    source_stats = compute_source_stats(connection)

    summary = grouping_summary(connection)
    distinct_opportunities = summary.total_jobs - summary.grouped_jobs + summary.groups

    tracked = _list_tracked(connection)
    counts = _counts_by_status(connection)
    searches = list_searches(connection)
    review_summary = review_group_summary(connection)

    overview = Overview(
        total_jobs=summary.total_jobs,
        canonical_groups=summary.groups,
        grouped_members=summary.grouped_jobs,
        distinct_opportunities=distinct_opportunities,
        tracked_applications=len(tracked),
        saved_searches=len(searches),
        reviewed_groups=review_summary.reviewed_groups,
        unreviewed_groups=review_summary.unreviewed_groups,
        shortlisted_groups=review_summary.shortlisted_groups,
        dismissed_groups=review_summary.dismissed_groups,
        review_later_groups=review_summary.review_later_groups,
        source_snapshots=source_snapshots,
        due_sources=due_sources,
        source_stats=source_stats,
    )

    pipeline = ApplicationPipeline(
        status_counts=tuple(
            (status, counts[status]) for status in APPLICATION_STATUSES
        ),
        recently_changed=tuple(tracked[:limit]),
    )

    actions = ActionItems(
        interview=_action_items(tracked, status="interview", limit=limit),
        offer=_action_items(tracked, status="offer", limit=limit),
        interested=_action_items(tracked, status="interested", limit=limit),
        awaiting_progress=_action_items(tracked, status="applied", limit=limit),
    )

    reminders = _build_reminders(connection, now=now, limit=limit)

    review = _build_review_queue(
        connection,
        profile=profile,
        saved_search=saved_search,
        limit=limit,
        grouped=grouped,
        feedback=feedback,
    )

    recommendations = _build_recommendations(
        connection,
        profile=profile,
        saved_search=saved_search,
        limit=limit,
        grouped=grouped,
        feedback=feedback,
    )

    return Report(
        now=now,
        overview=overview,
        pipeline=pipeline,
        actions=actions,
        reminders=reminders,
        review=review,
        recommendations=recommendations,
        health=CollectionHealth(per_source=source_snapshots),
    )
