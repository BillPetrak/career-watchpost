"""Read-only export record builders.

Every builder is a pure projection of stored CareerOS state: it only SELECTs
against the shared read-only pipelines (search, grouping, ranking, review
queue, source statistics, collection history) and never writes, never acquires
the collection lock, and never contacts an external service. When the caller
passes ``connection=None`` (database file does not exist yet), builders return
a deterministically empty result without ever creating the file.

Records are ordered dictionaries whose key order is the canonical export
column order; the serializers (``serializers.py``) render that order verbatim
into JSON/JSONL/CSV so every format carries the same data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from careeros.database.groups import (
    GroupedJob,
    group_ids_for_jobs,
    list_all_groups,
    search_grouped,
)
from careeros.database.jobs import StoredJob, list_jobs, search_jobs
from careeros.database.runs import CollectionRun, list_runs
from careeros.export.models import (
    MAX_EXPORT_RECORDS,
    ExportRequest,
    ExportResult,
)
from careeros.ranking.profile import load_profile
from careeros.ranking.scorer import component_to_dict
from careeros.review.feedback import build_feedback_set
from careeros.review.service import QueueItem, build_queue
from careeros.saved_searches.execution import rank_candidates
from careeros.source_stats.models import SourceStats
from careeros.source_stats.service import compute_source_stats, empty_source_stats
from careeros.text import clean_description

JOBS_SCHEMA = "careeros.jobs.v1"
RANKED_SCHEMA = "careeros.ranked.v1"
REVIEW_SCHEMA = "careeros.review.v1"
SOURCE_STATS_SCHEMA = "careeros.source-stats.v1"
COLLECTION_HISTORY_SCHEMA = "careeros.collection-history.v1"


def effective_limit(limit: int) -> int:
    """Turn a raw ``--limit`` value into the effective record cap.

    0 means "no cap" and maps to :data:`MAX_EXPORT_RECORDS`; any positive value
    is bounded by :data:`MAX_EXPORT_RECORDS`.
    """
    if limit <= 0:
        return MAX_EXPORT_RECORDS
    return min(limit, MAX_EXPORT_RECORDS)


def _finish(
    schema: str,
    records: list[dict[str, Any]],
    cap: int,
) -> ExportResult:
    """Build an :class:`ExportResult`, applying the record cap."""
    total = len(records)
    if len(records) > cap:
        records = records[:cap]
    return ExportResult(schema=schema, records=tuple(records), total=total)


def _clean_description(value: str | None, *, clean: bool) -> str | None:
    """Optionally normalize a stored description to readable text."""
    if value is None or not clean:
        return value
    return clean_description(value)


def _flat_job_record(
    job: StoredJob,
    group_id: str | None,
    *,
    clean_descriptions: bool,
) -> dict[str, Any]:
    """Serialize one stored job with its canonical group ID."""
    return {
        "id": job.id,
        "source": job.source,
        "source_job_id": job.source_job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "description": _clean_description(job.description, clean=clean_descriptions),
        "url": job.url,
        "employment_type": job.employment_type,
        "remote_status": job.remote_status,
        "published_at": job.published_at,
        "collected_at": job.collected_at,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "group_id": group_id,
    }


def _grouped_job_record(
    group: GroupedJob,
    *,
    clean_descriptions: bool,
) -> dict[str, Any]:
    """Serialize one canonical group (representative plus member summary)."""
    representative = group.representative
    return {
        "group_id": group.group_id,
        "representative_id": representative.id,
        "source": representative.source,
        "title": representative.title,
        "company": representative.company,
        "location": representative.location,
        "description": _clean_description(
            representative.description, clean=clean_descriptions
        ),
        "url": representative.url,
        "employment_type": representative.employment_type,
        "remote_status": representative.remote_status,
        "published_at": representative.published_at,
        "collected_at": representative.collected_at,
        "created_at": representative.created_at,
        "updated_at": representative.updated_at,
        "member_count": len(group.members),
        "members": [member.id for member in group.members],
        "sources": list(group.sources),
        "urls": list(group.urls),
    }


def _score_fields(
    record: dict[str, Any],
    *,
    rank: int,
    score: int,
    match_score: int,
    reasons: Sequence[str],
    components: Sequence[Any],
) -> dict[str, Any]:
    """Prepend the rank and append ranking fields in canonical order."""
    return {
        "rank": rank,
        **record,
        "score": score,
        "match_score": match_score,
        "reasons": list(reasons),
        "score_components": [component_to_dict(component) for component in components],
    }


def build_jobs_records(
    connection: sqlite3.Connection | None,
    request: ExportRequest,
) -> ExportResult:
    """Build job records (flat or grouped) with optional filters."""
    cap = effective_limit(request.limit)
    if connection is None:
        return _finish(JOBS_SCHEMA, [], cap)

    if request.grouped:
        if request.query is not None:
            groups = search_grouped(
                connection,
                request.query,
                remote=request.remote,
                location=request.location,
                source=request.source,
                employment_type=request.employment_type,
                limit=None,
            )
        else:
            groups = list_all_groups(
                connection,
                remote=request.remote,
                location=request.location,
                source=request.source,
                employment_type=request.employment_type,
            )
        groups = sorted(groups, key=lambda group: group.representative.id)
        records = [
            _grouped_job_record(group, clean_descriptions=request.clean_descriptions)
            for group in groups
        ]
        return _finish(JOBS_SCHEMA, records, cap)

    if request.query is not None:
        jobs = search_jobs(
            connection,
            request.query,
            remote=request.remote,
            location=request.location,
            source=request.source,
            employment_type=request.employment_type,
            limit=None,
        )
    else:
        jobs = list_jobs(
            connection,
            remote=request.remote,
            location=request.location,
            source=request.source,
            employment_type=request.employment_type,
        )
    group_ids = group_ids_for_jobs(connection, (job.id for job in jobs))
    records = [
        _flat_job_record(
            job, group_ids[job.id], clean_descriptions=request.clean_descriptions
        )
        for job in jobs
    ]
    return _finish(JOBS_SCHEMA, records, cap)


def build_ranked_records(
    connection: sqlite3.Connection | None,
    request: ExportRequest,
) -> ExportResult:
    """Build ranked records using the shared search + score pipeline."""
    cap = effective_limit(request.limit)
    if connection is None or request.profile is None:
        return _finish(RANKED_SCHEMA, [], cap)

    profile = load_profile(request.profile)
    feedback = build_feedback_set(connection) if request.use_feedback else None
    outcome = rank_candidates(
        connection,
        query=request.query or "",
        profile=profile,
        remote=request.remote,
        location=request.location,
        source=request.source,
        employment_type=request.employment_type,
        limit=None,
        grouped=request.grouped,
        min_score=request.min_score,
        untracked=request.untracked,
        include_statuses=request.include_statuses,
        exclude_statuses=request.exclude_statuses,
        feedback=feedback,
    )

    if request.grouped:
        records = [
            _score_fields(
                _grouped_job_record(
                    ranked.group, clean_descriptions=request.clean_descriptions
                ),
                rank=rank,
                score=ranked.score,
                match_score=ranked.match_score,
                reasons=ranked.reasons,
                components=ranked.result.components,
            )
            for rank, ranked in enumerate(outcome.grouped_ranked, start=1)
        ]
        return _finish(RANKED_SCHEMA, records, cap)

    group_ids = group_ids_for_jobs(
        connection, (ranked.job.id for ranked in outcome.flat_ranked)
    )
    records = [
        _score_fields(
            _flat_job_record(
                ranked.job,
                group_ids[ranked.job.id],
                clean_descriptions=request.clean_descriptions,
            ),
            rank=rank,
            score=ranked.score,
            match_score=ranked.match_score,
            reasons=ranked.reasons,
            components=ranked.result.components,
        )
        for rank, ranked in enumerate(outcome.flat_ranked, start=1)
    ]
    return _finish(RANKED_SCHEMA, records, cap)


def _review_record(
    rank: int,
    item: QueueItem,
    *,
    grouped: bool,
    group_id: str | None,
    clean_descriptions: bool,
) -> dict[str, Any]:
    """Serialize one review-queue item with its resolved decision."""
    record = _score_fields(
        (
            _grouped_job_record(item.group, clean_descriptions=clean_descriptions)
            if grouped
            else _flat_job_record(
                item.group.representative,
                group_id,
                clean_descriptions=clean_descriptions,
            )
        ),
        rank=rank,
        score=item.ranked.score,
        match_score=item.ranked.match_score,
        reasons=item.ranked.reasons,
        components=item.ranked.result.components,
    )
    record["review_decision"] = item.group_decision.decision
    return record


def build_review_records(
    connection: sqlite3.Connection | None,
    request: ExportRequest,
) -> ExportResult:
    """Build review-queue records with the same semantics as ``review next``."""
    cap = effective_limit(request.limit)
    if connection is None or request.profile is None:
        return _finish(REVIEW_SCHEMA, [], cap)

    profile = load_profile(request.profile)
    feedback = build_feedback_set(connection) if request.use_feedback else None
    queue = build_queue(
        connection,
        profile=profile,
        state=request.state,
        min_score=request.min_score,
        include_tracked=request.include_tracked,
        source=request.source,
        location=request.location,
        remote=request.remote,
        employment_type=request.employment_type,
        limit=MAX_EXPORT_RECORDS,
        grouped=request.grouped,
        feedback=feedback,
    )

    if request.grouped:
        records = [
            _review_record(
                rank,
                item,
                grouped=True,
                group_id=None,
                clean_descriptions=request.clean_descriptions,
            )
            for rank, item in enumerate(queue.items, start=1)
        ]
        return _finish(REVIEW_SCHEMA, records, cap)

    group_ids = group_ids_for_jobs(
        connection, (item.group.representative.id for item in queue.items)
    )
    records = [
        _review_record(
            rank,
            item,
            grouped=False,
            group_id=group_ids[item.group.representative.id],
            clean_descriptions=request.clean_descriptions,
        )
        for rank, item in enumerate(queue.items, start=1)
    ]
    return _finish(REVIEW_SCHEMA, records, cap)


def _source_stats_record(stats: SourceStats) -> dict[str, Any]:
    """Serialize one per-source statistics row in canonical field order."""
    record: dict[str, Any] = {
        "source": stats.source,
        "total_jobs": stats.total_jobs,
        "never_groupable_jobs": stats.never_groupable_jobs,
    }
    for coverage in stats.fields:
        record[f"{coverage.field}_missing"] = coverage.missing_count
        record[f"{coverage.field}_coverage_pct"] = coverage.coverage_pct
    return record


def build_source_stats_records(
    connection: sqlite3.Connection | None,
    request: ExportRequest,
) -> ExportResult:
    """Build per-source statistics records for every registered source."""
    cap = effective_limit(request.limit)
    if connection is None:
        stats = empty_source_stats()
    else:
        stats = compute_source_stats(connection)
    records = [_source_stats_record(entry) for entry in stats]
    if request.source is not None:
        records = [record for record in records if record["source"] == request.source]
    return _finish(SOURCE_STATS_SCHEMA, records, cap)


def _history_record(run: CollectionRun) -> dict[str, Any]:
    """Serialize one collection run in canonical field order."""
    return {
        "run_id": run.run_id,
        "source": run.source,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "succeeded": run.succeeded,
        "fetched": run.fetched,
        "inserted": run.inserted,
        "updated": run.updated,
        "error": run.error,
    }


def build_history_records(
    connection: sqlite3.Connection | None,
    request: ExportRequest,
) -> ExportResult:
    """Build collection-history records, newest first."""
    cap = effective_limit(request.limit)
    if connection is None:
        return _finish(COLLECTION_HISTORY_SCHEMA, [], cap)
    runs = list_runs(
        connection,
        source=request.source,
        failed_only=request.failed_only,
        limit=MAX_EXPORT_RECORDS,
    )
    records = [_history_record(run) for run in runs]
    return _finish(COLLECTION_HISTORY_SCHEMA, records, cap)


def build_records(
    connection: sqlite3.Connection | None,
    command: str,
    request: ExportRequest,
) -> ExportResult:
    """Dispatch to the builder for one export subcommand."""
    if command == "jobs":
        return build_jobs_records(connection, request)
    if command == "ranked":
        return build_ranked_records(connection, request)
    if command == "review":
        return build_review_records(connection, request)
    if command == "source-stats":
        return build_source_stats_records(connection, request)
    if command == "collection-history":
        return build_history_records(connection, request)
    raise ValueError(f"Unknown export command: {command!r}")


__all__: tuple[str, ...] = (
    "COLLECTION_HISTORY_SCHEMA",
    "JOBS_SCHEMA",
    "RANKED_SCHEMA",
    "REVIEW_SCHEMA",
    "SOURCE_STATS_SCHEMA",
    "build_history_records",
    "build_jobs_records",
    "build_ranked_records",
    "build_records",
    "build_review_records",
    "build_source_stats_records",
    "effective_limit",
)
