"""FastAPI application for the CareerOS API slice.

The slice exposes twelve read-only GET endpoints backed by the shared database
and presentation layers (``GET /health``, ``GET /jobs``, ``GET /jobs/{job_id}``,
``GET /sources/stats``, ``GET /collection-runs``, ``GET /applications``,
``GET /applications/{job_id}``, ``GET /reminders``,
``GET /reminders/{reminder_id}``, ``GET /groups``, ``GET /groups/summary``, and
``GET /groups/{group_id}``) plus one computation-only ``POST /ranked-jobs``
endpoint. Every endpoint is strictly read-only with respect to the database and
filesystem: nothing creates a database file, directory, or schema, nothing
writes, and nothing makes network calls. ``POST /ranked-jobs`` computes a
ranking from a candidate profile submitted inline in the request body and
creates no resource; it never reads profile files, never persists or logs the
submitted profile, and never applies review feedback. Application notes, event
history, review decisions, reminder messages, group matcher diagnostics, and
ranking reasons are never exposed over HTTP.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from careeros.api.deps import read_only_connection
from careeros.api.schemas import (
    ApplicationItem,
    ApplicationListResponse,
    CandidateProfileRequest,
    CollectionRun,
    CollectionRunListResponse,
    Group,
    GroupListResponse,
    GroupSummaryResponse,
    HealthResponse,
    Job,
    JobListResponse,
    RankedJobItem,
    RankedJobsResponse,
    RankRequest,
    ReminderItem,
    ReminderListResponse,
    SourceStats,
    SourceStatsListResponse,
)
from careeros.applications.models import validate_status
from careeros.database.applications import (
    ApplicationListItem,
    get_application,
    list_applications,
)
from careeros.database.groups import (
    GroupedJob,
    grouping_summary,
    list_all_groups,
    list_group_infos,
)
from careeros.database.jobs import get_job_by_id, list_jobs
from careeros.database.reminders import ReminderListEntry, get_reminder
from careeros.database.runs import list_runs
from careeros.presenters import (
    application_item_dict,
    group_dict,
    job_dict,
    ranked_job_item_dict,
    reminder_item_dict,
    source_stats_dict,
)
from careeros.ranking.scorer import CandidateProfile
from careeros.reminders.models import (
    ValidationError as ReminderValidationError,
)
from careeros.reminders.models import (
    parse_due_at,
)
from careeros.reminders.service import list_reminders
from careeros.saved_searches.execution import rank_candidates
from careeros.source_stats.service import compute_source_stats, empty_source_stats

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Safety valve when loading all matching applications before Python-side
# slicing (the same load-then-slice pattern as the other list endpoints).
# Tracked-application counts are bounded by the stored jobs they attach to;
# this mirrors the export package's record cap without importing it.
MAX_FETCH_APPLICATIONS = 1_000_000

# Safety valve when loading all matching runs before Python-side slicing (the
# same load-then-slice pattern as the other list endpoints). Run history is
# bounded in practice by collection cadence; this mirrors the export package's
# record cap without importing it.
MAX_FETCH_RUNS = 1_000_000

# Safety valve when loading all matching reminders before Python-side slicing
# (the same load-then-slice pattern as the other list endpoints). Reminder
# counts are bounded by the stored jobs they attach to; this mirrors the
# export package's record cap without importing it.
MAX_FETCH_REMINDERS = 1_000_000

DATABASE_UNAVAILABLE_MESSAGE = (
    "The database schema is unavailable (run 'careeros collect <source>' to "
    "initialize it)."
)


def _no_job_found_message(job_id: int) -> str:
    """Return the canonical "no such job" message for a job id."""
    return f"No job found with ID {job_id}."


def _no_reminder_found_message(reminder_id: int) -> str:
    """Return the canonical "no such reminder" message for a reminder id."""
    return f"No reminder found with ID {reminder_id}."


def _no_group_found_message(group_id: str) -> str:
    """Return the canonical "no such group" message for a group id."""
    return f"No group found with ID {group_id}."


def _to_profile(request_profile: CandidateProfileRequest) -> CandidateProfile:
    """Build a scorer profile from validated inline request fields.

    Maps the API request model onto the shared ``CandidateProfile`` dataclass.
    Whitespace-stripping and empty-after-strip rejection are enforced by the
    request schema; a whitespace-only ``remote_preference`` is stored stripped
    and collapses to ``None`` here, mirroring the profile loader.
    """
    return CandidateProfile(
        desired_titles=tuple(request_profile.desired_titles),
        skills=tuple(request_profile.skills),
        preferred_locations=tuple(request_profile.preferred_locations),
        remote_preference=request_profile.remote_preference or None,
        excluded_terms=tuple(request_profile.excluded_terms),
        required_skills=tuple(request_profile.required_skills),
        hard_excluded_terms=tuple(request_profile.hard_excluded_terms),
        preferred_employment_types=tuple(request_profile.preferred_employment_types),
        preferred_seniority=tuple(request_profile.preferred_seniority),
    )


def _read_schema_version(connection: sqlite3.Connection) -> int | None:
    """Read the stored schema version without running any DDL."""
    try:
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def create_app() -> FastAPI:
    """Build the CareerOS FastAPI application."""
    app = FastAPI(
        title="CareerOS API",
        description=(
            "Read-only REST access to stored CareerOS state. Never writes, "
            "never fetches jobs, and never contacts external services."
        ),
        version="0.1.0",
    )

    @app.exception_handler(sqlite3.Error)
    async def _database_error_handler(
        request: Request, exc: sqlite3.Error
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"detail": DATABASE_UNAVAILABLE_MESSAGE}
        )

    @app.get("/health", response_model=HealthResponse)
    def health(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
    ) -> HealthResponse:
        if connection is None:
            return HealthResponse(status="ok", database="absent", schema_version=None)
        return HealthResponse(
            status="ok",
            database="present",
            schema_version=_read_schema_version(connection),
        )

    @app.get("/jobs", response_model=JobListResponse)
    def list_jobs_endpoint(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        source: str | None = None,
        remote: bool | None = None,
        location: str | None = None,
        employment_type: str | None = None,
    ) -> JobListResponse:
        if connection is None:
            return JobListResponse(items=[], total=0, offset=offset, limit=limit)
        matches = list_jobs(
            connection,
            remote=remote,
            location=location,
            source=source,
            employment_type=employment_type,
        )
        return JobListResponse(
            items=[
                Job.model_validate(job_dict(job))
                for job in matches[offset : offset + limit]
            ],
            total=len(matches),
            offset=offset,
            limit=limit,
        )

    @app.get("/jobs/{job_id}", response_model=Job)
    def show_job(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        job_id: int,
    ) -> Job:
        if connection is not None:
            stored = get_job_by_id(connection, job_id)
            if stored is not None:
                return Job.model_validate(job_dict(stored))
        raise HTTPException(status_code=404, detail=_no_job_found_message(job_id))

    @app.get("/sources/stats", response_model=SourceStatsListResponse)
    def source_stats(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        source: str | None = None,
    ) -> SourceStatsListResponse:
        if connection is None:
            stats = empty_source_stats()
        else:
            stats = compute_source_stats(connection)
        if source is not None:
            stats = tuple(entry for entry in stats if entry.source == source)
        return SourceStatsListResponse(
            items=[
                SourceStats.model_validate(source_stats_dict(entry))
                for entry in stats[offset : offset + limit]
            ],
            total=len(stats),
            offset=offset,
            limit=limit,
        )

    @app.get("/collection-runs", response_model=CollectionRunListResponse)
    def collection_runs(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        source: str | None = None,
        failed: bool = False,
    ) -> CollectionRunListResponse:
        if connection is None:
            return CollectionRunListResponse(
                items=[], total=0, offset=offset, limit=limit
            )
        runs = list_runs(
            connection,
            source=source,
            failed_only=failed,
            limit=MAX_FETCH_RUNS,
        )
        return CollectionRunListResponse(
            items=[
                CollectionRun(
                    run_id=run.run_id,
                    source=run.source,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    succeeded=run.succeeded,
                    fetched=run.fetched,
                    inserted=run.inserted,
                    updated=run.updated,
                )
                for run in runs[offset : offset + limit]
            ],
            total=len(runs),
            offset=offset,
            limit=limit,
        )

    @app.get("/applications", response_model=ApplicationListResponse)
    def list_applications_endpoint(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        status: Annotated[list[str] | None, Query()] = None,
    ) -> ApplicationListResponse:
        if connection is None:
            return ApplicationListResponse(
                items=[], total=0, offset=offset, limit=limit
            )
        if status:
            for value in status:
                try:
                    validate_status(value)
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from None
        matches = list_applications(
            connection,
            statuses=status,
            limit=MAX_FETCH_APPLICATIONS,
        )
        return ApplicationListResponse(
            items=[
                ApplicationItem.model_validate(application_item_dict(item))
                for item in matches[offset : offset + limit]
            ],
            total=len(matches),
            offset=offset,
            limit=limit,
        )

    @app.get("/applications/{job_id}", response_model=ApplicationItem)
    def show_application(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        job_id: int,
    ) -> ApplicationItem:
        if connection is not None:
            stored = get_job_by_id(connection, job_id)
            if stored is not None:
                application = get_application(connection, job_id)
                if application is not None:
                    return ApplicationItem.model_validate(
                        application_item_dict(
                            ApplicationListItem(
                                application=application,
                                job=stored,
                                latest_note=None,
                            )
                        )
                    )
                raise HTTPException(
                    status_code=404, detail=f"No application found for job {job_id}."
                )
        raise HTTPException(status_code=404, detail=_no_job_found_message(job_id))

    @app.get("/reminders", response_model=ReminderListResponse)
    def list_reminders_endpoint(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        job_id: Annotated[int | None, Query(ge=1)] = None,
        kind: str | None = None,
        state: str | None = None,
        after: str | None = None,
        before: str | None = None,
        as_of: str | None = None,
    ) -> ReminderListResponse:
        if connection is None:
            return ReminderListResponse(items=[], total=0, offset=offset, limit=limit)
        reference: datetime | None = None
        if as_of is not None:
            try:
                reference = parse_due_at(as_of)
            except ReminderValidationError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
        try:
            entries = list_reminders(
                connection,
                job_id=job_id,
                kind=kind,
                state=state,
                after=after,
                before=before,
                limit=MAX_FETCH_REMINDERS,
                now=reference,
            )
        except ReminderValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return ReminderListResponse(
            items=[
                ReminderItem.model_validate(reminder_item_dict(entry))
                for entry in entries[offset : offset + limit]
            ],
            total=len(entries),
            offset=offset,
            limit=limit,
        )

    @app.get("/reminders/{reminder_id}", response_model=ReminderItem)
    def show_reminder(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        reminder_id: int,
    ) -> ReminderItem:
        if connection is not None:
            stored = get_reminder(connection, reminder_id)
            if stored is not None:
                job = get_job_by_id(connection, stored.job_id)
                if job is not None:
                    return ReminderItem.model_validate(
                        reminder_item_dict(ReminderListEntry(reminder=stored, job=job))
                    )
                raise HTTPException(
                    status_code=404, detail=_no_job_found_message(stored.job_id)
                )
        raise HTTPException(
            status_code=404, detail=_no_reminder_found_message(reminder_id)
        )

    @app.get("/groups", response_model=GroupListResponse)
    def list_groups_endpoint(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        source: str | None = None,
        remote: bool | None = None,
        location: str | None = None,
        employment_type: str | None = None,
    ) -> GroupListResponse:
        if connection is None:
            return GroupListResponse(items=[], total=0, offset=offset, limit=limit)
        groups = list_all_groups(
            connection,
            remote=remote,
            location=location,
            source=source,
            employment_type=employment_type,
        )
        return GroupListResponse(
            items=[
                Group.model_validate(group_dict(group))
                for group in groups[offset : offset + limit]
            ],
            total=len(groups),
            offset=offset,
            limit=limit,
        )

    @app.get("/groups/summary", response_model=GroupSummaryResponse)
    def group_summary(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
    ) -> GroupSummaryResponse:
        if connection is None:
            return GroupSummaryResponse(
                total_jobs=0, groups=0, grouped_jobs=0, distinct_opportunities=0
            )
        summary = grouping_summary(connection)
        return GroupSummaryResponse(
            total_jobs=summary.total_jobs,
            groups=summary.groups,
            grouped_jobs=summary.grouped_jobs,
            distinct_opportunities=(
                summary.total_jobs - summary.grouped_jobs + summary.groups
            ),
        )

    @app.get("/groups/{group_id}", response_model=Group)
    def show_group(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        group_id: str,
    ) -> Group:
        if connection is not None:
            infos = list_group_infos(connection, group_id=group_id, limit=1)
            if infos:
                info = infos[0]
                grouped = GroupedJob(
                    group_id=info.group_id,
                    representative=info.members[0],
                    members=info.members,
                    sources=info.sources,
                    urls=info.urls,
                )
                return Group.model_validate(group_dict(grouped))
        raise HTTPException(status_code=404, detail=_no_group_found_message(group_id))

    @app.post("/ranked-jobs", response_model=RankedJobsResponse)
    def rank_jobs(
        payload: RankRequest,
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        query: str = "",
        source: str | None = None,
        remote: bool | None = None,
        location: str | None = None,
        employment_type: str | None = None,
        minimum_score: Annotated[int, Query(ge=0, le=100)] = 0,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> RankedJobsResponse:
        """Rank stored jobs against an inline candidate profile (computation-only).

        Review feedback is always excluded (``feedback=None``), so stored
        review decisions never influence API scores. Results are flat raw jobs
        only, deterministically ordered by ``match_score DESC, published_at DESC
        NULLS LAST, id ASC``. ``total`` reflects the ranked count after
        ``minimum_score`` filtering and before pagination. A missing database
        yields an empty envelope without creating a file, directory, or schema.
        """
        if connection is None:
            return RankedJobsResponse(items=[], total=0, offset=offset, limit=limit)
        profile = _to_profile(payload.profile)
        outcome = rank_candidates(
            connection,
            query=query,
            profile=profile,
            remote=remote,
            location=location,
            source=source,
            employment_type=employment_type,
            limit=None,
            grouped=False,
            min_score=minimum_score,
            feedback=None,
        )
        ranked = outcome.flat_ranked
        return RankedJobsResponse(
            items=[
                RankedJobItem.model_validate(ranked_job_item_dict(item))
                for item in ranked[offset : offset + limit]
            ],
            total=len(ranked),
            offset=offset,
            limit=limit,
        )

    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")
