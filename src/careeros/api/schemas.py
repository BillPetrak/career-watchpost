"""Pydantic response schemas for the CareerOS API."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Readiness probe for the API service."""

    status: str
    database: str
    schema_version: int | None


class Job(BaseModel):
    """A stored job, mirroring the shared ``job_dict`` projection."""

    id: int
    source: str
    source_job_id: str | None
    title: str
    company: str | None
    location: str | None
    remote_status: str | None
    employment_type: str | None
    published_at: str | None
    url: str | None


class JobListResponse(BaseModel):
    """A paginated page of stored jobs."""

    items: list[Job]
    total: int
    offset: int
    limit: int


class FieldCoverageStats(BaseModel):
    """Coverage of one stored job field for one source.

    Mirrors the shared ``field_coverage_dict`` projection: ``coverage_pct`` is
    ``None`` when the source has no stored jobs.
    """

    field: str
    missing_count: int
    provided_count: int
    coverage_pct: int | None


class SourceStats(BaseModel):
    """Per-source statistics, mirroring the shared ``source_stats_dict``.

    ``fields`` holds one :class:`FieldCoverageStats` per tracked field in the
    canonical ``MISSING_FIELD_COLUMNS`` order.
    """

    source: str
    total_jobs: int
    never_groupable_jobs: int
    fields: list[FieldCoverageStats]


class SourceStatsListResponse(BaseModel):
    """A paginated page of per-source statistics in collector registry order."""

    items: list[SourceStats]
    total: int
    offset: int
    limit: int


class CollectionRun(BaseModel):
    """One collection attempt, mirroring ``database.runs.CollectionRun``.

    ``started_at``/``finished_at`` are raw stored ISO-8601 strings so malformed
    values survive to the response without crashing. ``succeeded`` is ``None``
    for incomplete (crashed) runs. The provider ``error`` text is intentionally
    omitted: it can embed URLs, hostnames, or local paths, so the API exposes
    no diagnostic content beyond the run's own fields.
    """

    run_id: int
    source: str
    started_at: str | None
    finished_at: str | None
    succeeded: int | None
    fetched: int
    inserted: int
    updated: int


class CollectionRunListResponse(BaseModel):
    """A paginated page of collection-run history, newest first."""

    items: list[CollectionRun]
    total: int
    offset: int
    limit: int


class ApplicationItem(BaseModel):
    """One tracked application joined with its job.

    Mirrors the shared ``application_item_dict`` projection. The application
    note, its event history, reminders, and review decisions are deliberately
    omitted: they are local workflow state the HTTP API never exposes.
    """

    application_id: int
    job_id: int
    current_status: str
    created_at: str
    updated_at: str
    job: Job


class ApplicationListResponse(BaseModel):
    """A paginated page of tracked applications, newest first."""

    items: list[ApplicationItem]
    total: int
    offset: int
    limit: int


class ReminderItem(BaseModel):
    """One stored reminder joined with its job.

    Mirrors the shared ``reminder_item_dict`` projection. The reminder
    message, its notification delivery history, and other local workflow state
    (application notes, review decisions) are deliberately omitted: they are
    user-written or diagnostic content the HTTP API never exposes.
    """

    reminder_id: int
    job_id: int
    kind: str
    due_at: str
    completed_at: str | None
    created_at: str
    updated_at: str
    job: Job


class ReminderListResponse(BaseModel):
    """A paginated page of reminders, soonest first."""

    items: list[ReminderItem]
    total: int
    offset: int
    limit: int


__all__: tuple[str, ...] = (
    "ApplicationItem",
    "ApplicationListResponse",
    "CollectionRun",
    "CollectionRunListResponse",
    "FieldCoverageStats",
    "HealthResponse",
    "Job",
    "JobListResponse",
    "ReminderItem",
    "ReminderListResponse",
    "SourceStats",
    "SourceStatsListResponse",
)
