"""FastAPI application for the CareerOS API slice.

The slice exposes nine read-only endpoints backed by the shared database and
presentation layers: ``GET /health``, ``GET /jobs``, ``GET /jobs/{job_id}``,
``GET /sources/stats``, ``GET /collection-runs``, ``GET /applications``,
``GET /applications/{job_id}``, ``GET /reminders``, and
``GET /reminders/{reminder_id}``. Every endpoint is strictly read-only: it
never creates a database file, directory, or schema, never writes, and never
makes network calls. Application notes, event history, review decisions, and
reminder messages are never exposed over HTTP.
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
    CollectionRun,
    CollectionRunListResponse,
    HealthResponse,
    Job,
    JobListResponse,
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
from careeros.database.jobs import get_job_by_id, list_jobs
from careeros.database.reminders import ReminderListEntry, get_reminder
from careeros.database.runs import list_runs
from careeros.presenters import (
    application_item_dict,
    job_dict,
    reminder_item_dict,
    source_stats_dict,
)
from careeros.reminders.models import (
    ValidationError as ReminderValidationError,
)
from careeros.reminders.models import (
    parse_due_at,
)
from careeros.reminders.service import list_reminders
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

    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")
