"""FastAPI application for the CareerOS API slice.

The slice exposes eighteen read-only GET endpoints backed by the shared database
and presentation layers (``GET /health``, ``GET /jobs``, ``GET /jobs/{job_id}``,
``GET /sources/stats``, ``GET /collection-runs``, ``GET /applications``,
``GET /applications/{job_id}``, ``GET /applications/{job_id}/history``,
``GET /reminders``,
``GET /reminders/{reminder_id}``, ``GET /groups``, ``GET /groups/summary``,
``GET /groups/{group_id}``, ``GET /review-decisions``,
``GET /review-decisions/{job_id}``,
``GET /review-decisions/{job_id}/history``, ``GET /saved-searches``, and
``GET /saved-searches/{name}``) plus three computation-only ``POST``
endpoints: ``POST /ranked-jobs``, ``POST /review-queue``, and
``POST /saved-searches/{name}/execute``. Every read and computation endpoint is
strictly read-only with respect to the database and filesystem: nothing creates
a database file, directory, or schema, nothing writes, and nothing makes network
calls. Both ``POST /ranked-jobs`` and ``POST /review-queue`` compute a ranking
from a candidate profile submitted inline in the request body and create no
resource; they never read profile files, never persist or log the submitted
profile, and never apply review feedback. ``POST /saved-searches/{name}/execute``
executes a stored saved-search workflow (search or rank mode) through the shared
execution pipeline against the current database, honoring the stored definition
including its ``result_limit`` cap; it never reads the stored ``profile_path``
and rank mode requires the candidate profile inline (search mode rejects one).
``PUT /review-decisions/{job_id}``, ``DELETE /review-decisions/{job_id}``,
``PUT /applications/{job_id}``,
``POST /reminders``, and
``PUT /reminders/{reminder_id}/complete`` are the slice's mutation endpoints:
``PUT /review-decisions/{job_id}`` sets or replaces
one job's current review decision through the shared ``review.service.set_decision``
domain service, ``DELETE /review-decisions/{job_id}`` clears one job's current
review decision through the shared ``review.service.clear_decision`` domain
service, ``PUT /applications/{job_id}`` starts tracking a job or moves
it through the application lifecycle through the shared
``applications.service.set_application_status`` domain service,
``POST /reminders`` adds a local follow-up reminder for a stored job through
the shared ``reminders.service.add_reminder`` domain service (intentionally
non-idempotent, never accepts a message, and never triggers notification
delivery), and
``PUT /reminders/{reminder_id}/complete`` marks an open reminder completed
through the shared ``reminders.service.complete_reminder`` domain service
(idempotent, never accepts a message, and never triggers notification
delivery). Each domain
service owns the transaction and audit event. The mutations run on a writable
connection under the per-database
``collection_lock`` (contention maps to a privacy-safe ``503``) and require an
already-initialized database (missing/uninitialized/malformed databases are the
canonical privacy-safe ``500``); they never initialize the database and never
accept or expose review notes, application notes, or reminder messages.
``POST /ranked-jobs`` ranks stored jobs against
the inline profile as flat raw jobs by default, or one result per canonical
duplicate group when ``grouped=true``. ``POST /review-queue`` ranks the
personalized daily review queue (opportunities needing review) against the
inline profile, collapsing cross-source duplicates per canonical group by
default. The review-decisions endpoints expose current review decisions and
their append-only transition history joined with the canonical job projection,
the applications endpoints expose tracked applications and their append-only
status-transition history, and the saved-searches endpoints expose stored
saved-search definitions for viewing only (list and detail; execution is a
separate computation endpoint). Application notes, event notes (beyond status
transition values and times), review decision notes, reminder messages, group
matcher diagnostics, ranking reasons, saved-search ``profile_path`` values
(local filesystem paths), and saved-search ``description`` values are never
exposed over HTTP.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from careeros.api.deps import (
    DATABASE_UNAVAILABLE_MESSAGE,
    read_only_connection,
    writable_connection,
)
from careeros.api.schemas import (
    ApplicationHistoryEvent,
    ApplicationHistoryResponse,
    ApplicationItem,
    ApplicationListResponse,
    ApplicationStatusRequest,
    CandidateProfileRequest,
    ClearReviewDecisionResponse,
    CollectionRun,
    CollectionRunListResponse,
    Group,
    GroupListResponse,
    GroupSummaryResponse,
    HealthResponse,
    Job,
    JobListResponse,
    RankedGroupItem,
    RankedJobItem,
    RankedJobsResponse,
    RankRequest,
    ReminderCreateRequest,
    ReminderItem,
    ReminderListResponse,
    ReviewDecisionEvent,
    ReviewDecisionHistoryResponse,
    ReviewDecisionItem,
    ReviewDecisionListResponse,
    ReviewDecisionRequest,
    ReviewQueueItem,
    ReviewQueueRequest,
    ReviewQueueResponse,
    SavedSearchExecuteRequest,
    SavedSearchExecutionResponse,
    SavedSearchItem,
    SavedSearchListResponse,
    SourceStats,
    SourceStatsListResponse,
)
from careeros.applications.models import validate_status
from careeros.applications.service import (
    UnknownJobError as ApplicationUnknownJobError,
)
from careeros.applications.service import set_application_status
from careeros.database.applications import (
    ApplicationListItem,
    get_application,
    list_application_events,
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
from careeros.database.review import DecisionListItem
from careeros.database.runs import list_runs
from careeros.presenters import (
    application_event_dict,
    application_item_dict,
    group_dict,
    job_dict,
    ranked_group_item_dict,
    ranked_job_item_dict,
    reminder_item_dict,
    review_decision_event_dict,
    review_decision_item_dict,
    review_queue_item_dict,
    saved_search_item_dict,
    source_stats_dict,
)
from careeros.ranking.scorer import CandidateProfile
from careeros.reminders.models import (
    UnknownJobError as ReminderUnknownJobError,
)
from careeros.reminders.models import (
    UnknownReminderError as ReminderUnknownReminderError,
)
from careeros.reminders.models import (
    ValidationError as ReminderValidationError,
)
from careeros.reminders.models import (
    parse_due_at,
)
from careeros.reminders.service import (
    add_reminder,
    complete_reminder,
    list_reminders,
)
from careeros.review.models import (
    UnknownJobError,
    validate_decision,
)
from careeros.review.models import (
    ValidationError as ReviewValidationError,
)
from careeros.review.service import (
    build_queue,
    clear_decision,
    get_decision,
    list_decision_events,
    list_decisions,
    set_decision,
)
from careeros.saved_searches.execution import (
    execute_rank,
    execute_search,
    rank_candidates,
)
from careeros.saved_searches.service import get_search, list_searches
from careeros.source_stats.service import compute_source_stats, empty_source_stats

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Safety valve when loading all matching applications/events before Python-side
# slicing (the same load-then-slice pattern as the other list endpoints).
# Tracked-application and event counts are bounded by the stored jobs they
# attach to; this mirrors the export package's record cap without importing it.
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

# Safety valve when loading all matching review-queue entries before Python-side
# slicing (the same load-then-slice pattern as the other list endpoints). The
# queue is bounded by the stored jobs it ranks; this mirrors the export
# package's record cap without importing it.
MAX_FETCH_QUEUE = 1_000_000

# Safety valve when loading all matching review decisions/events before
# Python-side slicing (the same load-then-slice pattern as the other list
# endpoints). Decision and event counts are bounded by the stored jobs they
# attach to; this mirrors the export package's record cap without importing it.
MAX_FETCH_DECISIONS = 1_000_000


def _no_job_found_message(job_id: int) -> str:
    """Return the canonical "no such job" message for a job id."""
    return f"No job found with ID {job_id}."


def _no_reminder_found_message(reminder_id: int) -> str:
    """Return the canonical "no such reminder" message for a reminder id."""
    return f"No reminder found with ID {reminder_id}."


def _no_group_found_message(group_id: str) -> str:
    """Return the canonical "no such group" message for a group id."""
    return f"No group found with ID {group_id}."


def _no_saved_search_found_message(name: str) -> str:
    """Return the canonical "no such saved search" message for a name."""
    return f"No saved search named {name!r}."


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
            "REST access to stored CareerOS state. Reads and computations are "
            "read-only; the mutation endpoints "
            "PUT /review-decisions/{job_id}, DELETE /review-decisions/{job_id}, "
            "PUT /applications/{job_id}, POST /reminders, and "
            "PUT /reminders/{reminder_id}/complete write "
            "through the shared review-decision, application-tracking, and "
            "reminder domain services "
            "under the collection lock. Never fetches "
            "jobs, never initializes the database, and never contacts external "
            "services."
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

    @app.get(
        "/applications/{job_id}/history",
        response_model=ApplicationHistoryResponse,
    )
    def application_history(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        job_id: int,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> ApplicationHistoryResponse:
        """Show one job's append-only application history, newest first."""
        if connection is not None:
            stored = get_job_by_id(connection, job_id)
            if stored is not None:
                application = get_application(connection, job_id)
                if application is not None:
                    events = list_application_events(
                        connection,
                        application_id=application.id,
                        limit=MAX_FETCH_APPLICATIONS,
                    )
                    return ApplicationHistoryResponse(
                        items=[
                            ApplicationHistoryEvent.model_validate(
                                application_event_dict(event, job_id=job_id)
                            )
                            for event in events[offset : offset + limit]
                        ],
                        total=len(events),
                        offset=offset,
                        limit=limit,
                    )
                raise HTTPException(
                    status_code=404,
                    detail=f"No application found for job {job_id}.",
                )
        raise HTTPException(status_code=404, detail=_no_job_found_message(job_id))

    @app.put("/applications/{job_id}", response_model=ApplicationItem)
    def set_application_status_endpoint(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        job_id: int,
        payload: ApplicationStatusRequest,
    ) -> ApplicationItem:
        """Start tracking a job or move it through the application lifecycle.

        All decision logic lives in the shared
        ``applications.service.set_application_status`` domain service: this
        route only validates the request body, translates known domain errors,
        and returns the canonical ``ApplicationItem`` projection (exactly the
        shape ``GET /applications/{job_id}`` returns). The writable connection
        runs under the per-database collection lock, so this endpoint
        coordinates with every CLI/collector writer; lock contention maps to a
        privacy-safe ``503`` and a missing or uninitialized database to the
        canonical privacy-safe ``500``. Repeating the same status is idempotent
        (no new event, no timestamp mutation) and every real transition is
        recorded in the append-only application history. ``note`` is never
        accepted or exposed.
        """
        try:
            set_application_status(
                connection,
                job_id=job_id,
                status=payload.status,
            )
        except ApplicationUnknownJobError:
            raise HTTPException(
                status_code=404, detail=_no_job_found_message(job_id)
            ) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        stored = get_job_by_id(connection, job_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=_no_job_found_message(job_id))
        application = get_application(connection, job_id)
        if application is None:
            raise HTTPException(
                status_code=404,
                detail=f"No application found for job {job_id}.",
            )
        return ApplicationItem.model_validate(
            application_item_dict(
                ApplicationListItem(
                    application=application,
                    job=stored,
                    latest_note=None,
                )
            )
        )

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

    @app.post("/reminders", response_model=ReminderItem)
    def create_reminder(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        payload: ReminderCreateRequest,
    ) -> ReminderItem:
        """Add a reminder for an existing stored job (write).

        All logic lives in the shared ``reminders.service.add_reminder`` domain
        service: this route only validates the request body, translates known
        domain errors, and returns the canonical ``ReminderItem`` projection
        (exactly the shape ``GET /reminders/{reminder_id}`` returns). The
        writable connection runs under the per-database collection lock, so
        this endpoint coordinates with every CLI/collector writer; lock
        contention maps to a privacy-safe ``503`` and a missing or uninitialized
        database to the canonical privacy-safe ``500``. Creation is
        intentionally non-idempotent: two identical POSTs create two distinct
        reminders. ``message`` is never accepted, and creation never creates
        notification attempts or triggers notification delivery.
        """
        try:
            stored = add_reminder(
                connection,
                job_id=payload.job_id,
                kind=payload.kind,
                due_at=payload.due_at,
                message=None,
            )
        except ReminderValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except ReminderUnknownJobError:
            raise HTTPException(
                status_code=404, detail=_no_job_found_message(payload.job_id)
            ) from None
        job = get_job_by_id(connection, payload.job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail=_no_job_found_message(payload.job_id)
            )
        return ReminderItem.model_validate(
            reminder_item_dict(ReminderListEntry(reminder=stored, job=job))
        )

    @app.put("/reminders/{reminder_id}/complete", response_model=ReminderItem)
    def complete_reminder_endpoint(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        reminder_id: int,
    ) -> ReminderItem:
        """Mark an open reminder completed (write).

        All logic lives in the shared ``reminders.service.complete_reminder``
        domain service: this route only translates the canonical unknown-reminder
        error and returns the canonical ``ReminderItem`` projection (exactly the
        shape ``GET /reminders/{reminder_id}`` returns). The writable connection
        runs under the per-database collection lock, so this endpoint
        coordinates with every CLI/collector writer; lock contention maps to a
        privacy-safe ``503`` and a missing or uninitialized database to the
        canonical privacy-safe ``500``. Completing an already-completed reminder
        is an idempotent no-op (no row mutation, original completion time
        preserved). The route declares no request body, never accepts a message,
        and never creates notification attempts or triggers notification
        delivery.
        """
        try:
            stored, _changed = complete_reminder(connection, reminder_id=reminder_id)
        except ReminderUnknownReminderError:
            raise HTTPException(
                status_code=404, detail=_no_reminder_found_message(reminder_id)
            ) from None
        job = get_job_by_id(connection, stored.job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail=_no_job_found_message(stored.job_id)
            )
        return ReminderItem.model_validate(
            reminder_item_dict(ReminderListEntry(reminder=stored, job=job))
        )

    @app.get("/review-decisions", response_model=ReviewDecisionListResponse)
    def list_review_decisions(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        decision: Annotated[list[str] | None, Query()] = None,
    ) -> ReviewDecisionListResponse:
        """List current review decisions joined with their jobs, newest first."""
        if connection is None:
            return ReviewDecisionListResponse(
                items=[], total=0, offset=offset, limit=limit
            )
        if decision:
            for value in decision:
                try:
                    validate_decision(value)
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from None
        matches = list_decisions(
            connection,
            decisions=decision,
            limit=MAX_FETCH_DECISIONS,
        )
        return ReviewDecisionListResponse(
            items=[
                ReviewDecisionItem.model_validate(review_decision_item_dict(item))
                for item in matches[offset : offset + limit]
            ],
            total=len(matches),
            offset=offset,
            limit=limit,
        )

    @app.get("/review-decisions/{job_id}", response_model=ReviewDecisionItem)
    def show_review_decision(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        job_id: int,
    ) -> ReviewDecisionItem:
        """Show the current review decision for one job."""
        if connection is not None:
            stored = get_job_by_id(connection, job_id)
            if stored is not None:
                decision = get_decision(connection, job_id=job_id)
                if decision is not None:
                    return ReviewDecisionItem.model_validate(
                        review_decision_item_dict(
                            DecisionListItem(
                                decision=decision,
                                job=stored,
                                latest_note=None,
                            )
                        )
                    )
                raise HTTPException(
                    status_code=404,
                    detail=f"No review decision found for job {job_id}.",
                )
        raise HTTPException(status_code=404, detail=_no_job_found_message(job_id))

    @app.put("/review-decisions/{job_id}", response_model=ReviewDecisionItem)
    def set_review_decision(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        job_id: int,
        payload: ReviewDecisionRequest,
    ) -> ReviewDecisionItem:
        """Set or replace the current review decision for one job (write).

        All decision logic
        lives in the shared ``review.service.set_decision`` domain service:
        this route only validates the request body, translates known domain
        errors, and returns the canonical ``ReviewDecisionItem`` projection
        (exactly the shape ``GET /review-decisions/{job_id}`` returns). The
        writable connection runs under the per-database collection lock, so
        this endpoint coordinates with every CLI/collector writer; lock
        contention maps to a privacy-safe ``503`` and a missing or uninitialized
        database to the canonical privacy-safe ``500``. Repeating the same
        decision is idempotent (no new event, no timestamp mutation). ``note``
        is never accepted or exposed.
        """
        try:
            set_decision(connection, job_id=job_id, decision=payload.decision)
        except ReviewValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except UnknownJobError:
            raise HTTPException(
                status_code=404, detail=_no_job_found_message(job_id)
            ) from None
        stored = get_job_by_id(connection, job_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=_no_job_found_message(job_id))
        decision = get_decision(connection, job_id=job_id)
        if decision is None:
            raise HTTPException(
                status_code=404,
                detail=f"No review decision found for job {job_id}.",
            )
        return ReviewDecisionItem.model_validate(
            review_decision_item_dict(
                DecisionListItem(
                    decision=decision,
                    job=stored,
                    latest_note=None,
                )
            )
        )

    @app.delete(
        "/review-decisions/{job_id}", response_model=ClearReviewDecisionResponse
    )
    def clear_review_decision(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        job_id: int,
    ) -> ClearReviewDecisionResponse:
        """Clear one job's review decision, returning it to unreviewed (write).

        All decision logic
        lives in the shared ``review.service.clear_decision`` domain service:
        this route only translates the canonical unknown-job error and returns
        the two-field ``ClearReviewDecisionResponse``. The writable connection
        runs under the per-database collection lock, so this endpoint
        coordinates with every CLI/collector writer; lock contention maps to a
        privacy-safe ``503`` and a missing or uninitialized database to the
        canonical privacy-safe ``500``. A real clear removes the current
        decision row and appends one history event whose ``new_decision`` is
        ``None``, committing atomically; clearing an already-unreviewed job is
        an idempotent no-op (``changed=false``, no new event, no row mutation).
        The route declares no request body, never accepts a note, and never
        exposes decision notes.
        """
        try:
            result = clear_decision(connection, job_id=job_id)
        except UnknownJobError:
            raise HTTPException(
                status_code=404, detail=_no_job_found_message(job_id)
            ) from None
        return ClearReviewDecisionResponse(job_id=job_id, changed=result.changed)

    @app.get(
        "/review-decisions/{job_id}/history",
        response_model=ReviewDecisionHistoryResponse,
    )
    def review_decision_history(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        job_id: int,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> ReviewDecisionHistoryResponse:
        """Show one job's append-only review-decision history, newest first."""
        if connection is not None:
            stored = get_job_by_id(connection, job_id)
            if stored is not None:
                events = list_decision_events(
                    connection,
                    job_id=job_id,
                    limit=MAX_FETCH_DECISIONS,
                )
                return ReviewDecisionHistoryResponse(
                    items=[
                        ReviewDecisionEvent.model_validate(
                            review_decision_event_dict(event)
                        )
                        for event in events[offset : offset + limit]
                    ],
                    total=len(events),
                    offset=offset,
                    limit=limit,
                )
        raise HTTPException(status_code=404, detail=_no_job_found_message(job_id))

    @app.get("/saved-searches", response_model=SavedSearchListResponse)
    def list_saved_searches(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> SavedSearchListResponse:
        """List stored saved-search definitions, case-insensitively by name.

        Viewing only: definitions are listed exactly as stored through the
        shared ``list_searches`` service (ordered case-insensitively by name
        then id); execution is a separate computation endpoint.
        ``profile_path`` and
        ``description`` are never exposed. A missing database yields an empty
        envelope without creating a file, directory, or schema.
        """
        if connection is None:
            return SavedSearchListResponse(
                items=[], total=0, offset=offset, limit=limit
            )
        searches = list_searches(connection)
        return SavedSearchListResponse(
            items=[
                SavedSearchItem.model_validate(saved_search_item_dict(search))
                for search in searches[offset : offset + limit]
            ],
            total=len(searches),
            offset=offset,
            limit=limit,
        )

    @app.get("/saved-searches/{name}", response_model=SavedSearchItem)
    def show_saved_search(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        name: str,
    ) -> SavedSearchItem:
        """Show one stored saved-search definition by case-insensitive name.

        Viewing only: the definition is fetched through the shared
        ``get_search`` service. No filesystem access, profile loading, or
        execution ever occurs as a consequence of this request, and
        ``profile_path`` / ``description`` are never exposed.
        """
        if connection is not None:
            stored = get_search(connection, name)
            if stored is not None:
                return SavedSearchItem.model_validate(saved_search_item_dict(stored))
        raise HTTPException(
            status_code=404, detail=_no_saved_search_found_message(name)
        )

    @app.post(
        "/saved-searches/{name}/execute",
        response_model=SavedSearchExecutionResponse,
    )
    def execute_saved_search(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        name: str,
        payload: SavedSearchExecuteRequest | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> SavedSearchExecutionResponse:
        """Execute a stored saved search (computation-only).

        Runs the shared execution pipeline (``execute_search`` for search mode,
        ``execute_rank`` for rank mode) against the current database, honoring
        every stored filter, ``grouped`` flag, rank ``min_score``, and the
        stored ``result_limit`` cap exactly as ``saved-search run`` does. The
        stored definition is authoritative: only ``offset``/``limit`` pagination
        is accepted, and no query override of the workflow is possible. Rank
        mode requires an inline candidate profile (``422`` when absent) and
        never reads the stored ``profile_path``; search mode rejects a profile
        (``422``). Review feedback is always disabled (``feedback=None``), so
        stored review decisions never affect scores. ``total`` is the pipeline
        output length after the stored caps and before slicing. A missing
        database yields the canonical saved-search ``404`` without creating a
        file, directory, or schema; an uninitialized or malformed database
        yields the canonical privacy-safe ``500``.
        """
        if connection is None:
            raise HTTPException(
                status_code=404, detail=_no_saved_search_found_message(name)
            )
        stored = get_search(connection, name)
        if stored is None:
            raise HTTPException(
                status_code=404, detail=_no_saved_search_found_message(name)
            )
        if stored.mode == "rank":
            request = payload or SavedSearchExecuteRequest()
            if request.profile is None:
                raise HTTPException(
                    status_code=422,
                    detail="profile is required to execute a rank-mode saved search.",
                )
            outcome = execute_rank(
                connection,
                stored,
                profile=_to_profile(request.profile),
                feedback=None,
            )
            if outcome.grouped:
                ranked = outcome.grouped_ranked
                return SavedSearchExecutionResponse(
                    mode="rank",
                    grouped=True,
                    items=[
                        RankedGroupItem.model_validate(ranked_group_item_dict(item))
                        for item in ranked[offset : offset + limit]
                    ],
                    total=len(ranked),
                    offset=offset,
                    limit=limit,
                )
            ranked_jobs = outcome.flat_ranked
            return SavedSearchExecutionResponse(
                mode="rank",
                grouped=False,
                items=[
                    RankedJobItem.model_validate(ranked_job_item_dict(item))
                    for item in ranked_jobs[offset : offset + limit]
                ],
                total=len(ranked_jobs),
                offset=offset,
                limit=limit,
            )
        if payload is not None and payload.profile is not None:
            raise HTTPException(
                status_code=422,
                detail="profile is only valid for rank-mode saved searches.",
            )
        search_outcome = execute_search(connection, stored)
        if search_outcome.grouped:
            groups = search_outcome.groups
            return SavedSearchExecutionResponse(
                mode="search",
                grouped=True,
                items=[
                    Group.model_validate(group_dict(group))
                    for group in groups[offset : offset + limit]
                ],
                total=len(groups),
                offset=offset,
                limit=limit,
            )
        flat = search_outcome.flat
        return SavedSearchExecutionResponse(
            mode="search",
            grouped=False,
            items=[
                Job.model_validate(job_dict(job))
                for job in flat[offset : offset + limit]
            ],
            total=len(flat),
            offset=offset,
            limit=limit,
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
        grouped: bool = False,
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
        review decisions never influence API scores. ``grouped`` is false by
        default, yielding flat raw jobs deterministically ordered by
        ``match_score DESC, published_at DESC NULLS LAST, id ASC``. With
        ``grouped=true`` results are one entry per canonical duplicate group
        through the shared grouped ranking pipeline, deterministically ordered
        by ``match_score DESC``, then newest member publication time, then
        representative job ID. ``total`` reflects the ranked count after
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
            grouped=grouped,
            min_score=minimum_score,
            feedback=None,
        )
        if grouped:
            grouped_ranked = outcome.grouped_ranked
            return RankedJobsResponse(
                items=[
                    RankedGroupItem.model_validate(ranked_group_item_dict(item))
                    for item in grouped_ranked[offset : offset + limit]
                ],
                total=len(grouped_ranked),
                offset=offset,
                limit=limit,
            )
        flat_ranked = outcome.flat_ranked
        return RankedJobsResponse(
            items=[
                RankedJobItem.model_validate(ranked_job_item_dict(item))
                for item in flat_ranked[offset : offset + limit]
            ],
            total=len(flat_ranked),
            offset=offset,
            limit=limit,
        )

    @app.post("/review-queue", response_model=ReviewQueueResponse)
    def review_queue(
        payload: ReviewQueueRequest,
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        state: str = "unreviewed",
        minimum_score: Annotated[int, Query(ge=0, le=100)] = 0,
        include_tracked: bool = False,
        grouped: bool = True,
        source: str | None = None,
        remote: bool | None = None,
        location: str | None = None,
        employment_type: str | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    ) -> ReviewQueueResponse:
        """Rank the personalized daily review queue (computation-only).

        Review feedback is always excluded (``feedback=None``), so stored
        review decisions never influence queue scores. Results are grouped per
        canonical duplicate group by default (``grouped=false`` expands to
        singleton groups), deterministically ordered by ``match_score DESC``,
        then newest member publication time, then representative job ID.
        ``total`` reflects the queue count after ``state``/application/
        ``minimum_score`` filtering and before pagination. A missing database
        yields an empty envelope without creating a file, directory, or schema.
        """
        if connection is None:
            return ReviewQueueResponse(items=[], total=0, offset=offset, limit=limit)
        profile = _to_profile(payload.profile)
        try:
            outcome = build_queue(
                connection,
                profile=profile,
                state=state,
                min_score=minimum_score,
                include_tracked=include_tracked,
                source=source,
                location=location,
                remote=remote,
                employment_type=employment_type,
                limit=MAX_FETCH_QUEUE,
                grouped=grouped,
                feedback=None,
            )
        except ReviewValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        items = outcome.items
        return ReviewQueueResponse(
            items=[
                ReviewQueueItem.model_validate(review_queue_item_dict(item))
                for item in items[offset : offset + limit]
            ],
            total=len(items),
            offset=offset,
            limit=limit,
        )

    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")
