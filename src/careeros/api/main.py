"""FastAPI application for the first CareerOS API slice.

The slice exposes three read-only endpoints backed by the shared database and
presentation layers: ``GET /health``, ``GET /jobs``, and ``GET /jobs/{job_id}``.
Every endpoint is strictly read-only: it never creates a database file,
directory, or schema, never writes, and never makes network calls.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from careeros.api.deps import read_only_connection
from careeros.api.schemas import HealthResponse, Job, JobListResponse
from careeros.database.jobs import get_job_by_id, list_jobs
from careeros.presenters import job_dict

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

DATABASE_UNAVAILABLE_MESSAGE = (
    "The database schema is unavailable (run 'careeros collect <source>' to "
    "initialize it)."
)


def _no_job_found_message(job_id: int) -> str:
    """Return the canonical "no such job" message for a job id."""
    return f"No job found with ID {job_id}."


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

    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")
