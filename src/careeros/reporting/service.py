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
from careeros.database.jobs import _SELECT_COLUMNS, _stored_job_from_row
from careeros.ranking.scorer import CandidateProfile
from careeros.reporting.models import (
    ACTION_ITEM_LABELS,
    ActionItem,
    ActionItems,
    ApplicationPipeline,
    ApplicationSnapshot,
    CollectionHealth,
    Overview,
    Recommendations,
    Report,
)
from careeros.saved_searches.execution import execute_rank, rank_candidates
from careeros.saved_searches.models import SavedSearch
from careeros.saved_searches.service import list_searches
from careeros.status import build_statuses

_JOB_COLUMNS: tuple[str, ...] = tuple(_SELECT_COLUMNS.split(", "))


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
                job=_stored_job_from_row(row),
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
) -> Recommendations:
    """Score opportunities for the report using the shared ranking engine.

    A saved rank search governs its own query, filters, min score, and result
    limit (the report's ``limit`` only ever trims afterwards). Without a saved
    search, ``--profile`` ranks every untracked job. Without either, the
    section is honestly skipped with a note.
    """
    if saved_search is not None:
        assert profile is not None
        outcome = execute_rank(connection, saved_search, profile=profile)
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


def build_report(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    profile: CandidateProfile | None = None,
    saved_search: SavedSearch | None = None,
    limit: int = 10,
    grouped: bool = False,
) -> Report:
    """Build a complete Career Snapshot from stored state (read-only).

    ``now`` is captured once by the caller and used for every collection
    status and due calculation in the report.
    """
    source_snapshots = tuple(build_statuses(connection, now=now))
    due_sources = tuple(status.source for status in source_snapshots if status.due)

    summary = grouping_summary(connection)
    distinct_opportunities = summary.total_jobs - summary.grouped_jobs + summary.groups

    tracked = _list_tracked(connection)
    counts = _counts_by_status(connection)
    searches = list_searches(connection)

    overview = Overview(
        total_jobs=summary.total_jobs,
        canonical_groups=summary.groups,
        grouped_members=summary.grouped_jobs,
        distinct_opportunities=distinct_opportunities,
        tracked_applications=len(tracked),
        saved_searches=len(searches),
        source_snapshots=source_snapshots,
        due_sources=due_sources,
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

    recommendations = _build_recommendations(
        connection,
        profile=profile,
        saved_search=saved_search,
        limit=limit,
        grouped=grouped,
    )

    return Report(
        now=now,
        overview=overview,
        pipeline=pipeline,
        actions=actions,
        recommendations=recommendations,
        health=CollectionHealth(per_source=source_snapshots),
    )
