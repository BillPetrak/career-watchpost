"""FastAPI application for the CareerOS API slice.

The slice exposes nineteen read-only GET endpoints backed by the shared database
and presentation layers (``GET /health``, ``GET /jobs``, ``GET /jobs/{job_id}``,
``GET /sources/stats``, ``GET /collection-runs``, ``GET /applications``,
``GET /applications/{job_id}``, ``GET /applications/{job_id}/history``,
``GET /reminders``,
``GET /reminders/{reminder_id}``, ``GET /groups``, ``GET /groups/summary``,
``GET /groups/{group_id}``, ``GET /review-decisions``,
``GET /review-decisions/{job_id}``,
``GET /review-decisions/{job_id}/history``, ``GET /saved-searches``,
``GET /saved-searches/{name}``, and ``GET /report``) plus three
computation-only ``POST``
endpoints: ``POST /ranked-jobs``, ``POST /review-queue``, and
``POST /saved-searches/{name}/execute``. Every read and computation endpoint is
strictly read-only with respect to the database and filesystem: nothing creates
a database file, directory, or schema, nothing writes, and nothing makes network
calls. ``GET /jobs`` optionally narrows the listing by the same
application-tracking filters as the CLI and ``POST /ranked-jobs``
(``untracked``, ``include_statuses``, ``exclude_statuses``), narrowing the
base keyword/filter set through the shared application filter before
pagination while keeping the job projection and ordering unchanged. ``GET
/groups`` likewise narrows the distinct-opportunities browsing with the same
keyword (``query``) and application-tracking filters through the shared
grouped-search and group-tracking helpers (``search_grouped`` and
``apply_filter_to_groups``), keeping the group projection and ordering
unchanged and never adding an application status to any item. Both
``POST /ranked-jobs`` and ``POST /review-queue`` compute a ranking
from a candidate profile submitted inline in the request body and create no
resource; they never read profile files, never persist or log the submitted
profile, and never apply review feedback. ``POST /saved-searches/{name}/execute``
executes a stored saved-search workflow (search or rank mode) through the shared
execution pipeline against the current database, honoring the stored definition
including its ``result_limit`` cap; it never reads the stored ``profile_path``
and rank mode requires the candidate profile inline (search mode rejects one).
``PUT /review-decisions/{job_id}``, ``DELETE /review-decisions/{job_id}``,
``PUT /applications/{job_id}``,
``POST /reminders``,
``PATCH /reminders/{reminder_id}``,
``PUT /reminders/{reminder_id}/complete``,
``PUT /reminders/{reminder_id}/reopen``,
``DELETE /reminders/{reminder_id}``,
``POST /saved-searches``,
``DELETE /saved-searches/{name}``, and
``PATCH /saved-searches/{name}`` are the slice's mutation endpoints:
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
delivery),
``PUT /reminders/{reminder_id}/complete`` marks an open reminder completed
through the shared ``reminders.service.complete_reminder`` domain service
(idempotent, never accepts a message, and never triggers notification
delivery), and
``PUT /reminders/{reminder_id}/reopen`` reopens a completed reminder
through the shared ``reminders.service.reopen_reminder`` domain service
(idempotent, never accepts a message, and never triggers notification
delivery), and
``DELETE /reminders/{reminder_id}`` deletes one reminder
through the shared ``reminders.service.delete_reminder`` domain service
(returning the deleted ``ReminderItem``; a repeated DELETE is a canonical
``404`` and a reminder referenced by ``reminder_notifications`` is protected
by the existing ``ON DELETE RESTRICT`` constraint), and
``PATCH /reminders/{reminder_id}`` updates a reminder's kind and/or due time
through the shared ``reminders.service.update_reminder`` domain service (a
genuine partial update that requires at least one of ``kind``/``due_at``, is
idempotent for repeated identical values, and never accepts, clears, or
exposes the stored reminder message), and
``DELETE /saved-searches/{name}`` deletes one saved-search definition
through the shared ``saved_searches.service.delete_search`` domain service
(returning the deleted ``SavedSearchItem``; a repeated DELETE is a canonical
``404`` and the route never accepts or exposes ``profile_path`` or
``description``), and
``PATCH /saved-searches/{name}`` either renames one saved-search definition
through the shared ``saved_searches.service.rename_search`` domain service (a
rename-only body carrying exactly the replacement ``name``; source-name
lookup is case-insensitive, a same-name or case-only rename is an idempotent
no-op preserving ``updated_at``, a duplicate target is a domain validation
conflict mapped to ``422``) or updates its definition through the shared
``saved_searches.service.update_search`` domain service (one or more
definition fields and/or ``clears``, with omitted/null fields retaining prior
values and ``[]`` emptying stored status lists; a genuine change bumps
``updated_at`` and an identical repeat is an idempotent no-op). Mixing a
rename with definition updates or non-empty ``clears`` is rejected with
``422``, and the route never accepts or exposes
``profile_path``, ``description``, ``mode``, or any free text. Each domain
service owns the transaction and audit event. The mutations run on a writable
connection under the per-database
``collection_lock`` (contention maps to a privacy-safe ``503``) and require an
already-initialized database (missing/uninitialized/malformed databases are the
canonical privacy-safe ``500``); they never initialize the database and never
accept or expose review notes, application notes, reminder messages, or
saved-search ``profile_path``/``description`` values.
``POST /saved-searches`` creates a new search-mode saved-search definition
through the shared ``saved_searches.service.create_search`` domain service
(search-mode authoring only: the request schema restricts ``mode`` to the
single literal ``"search"``, rank-mode creation is impossible over HTTP
because it would require a local ``profile_path``, and ``profile_path`` /
``description`` / ``min_score`` are never accepted; duplicate names are a
domain validation conflict mapped to ``422`` and creation is intentionally
non-idempotent), and
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
separate computation endpoint). ``GET /report`` returns a profile-less
Career Snapshot dashboard backed by the shared
``reporting.service.build_report`` domain service: it never accepts a profile,
never performs profile scoring, and projects only an explicit HTTP-safe
allow-list of report fields. Application notes, event notes (beyond status
transition values and times), review decision notes, reminder messages, group
matcher diagnostics, ranking reasons, ``notification_summary``, provider
collection-error text, saved-search ``profile_path`` values (local filesystem
paths), and saved-search ``description`` values are never exposed over HTTP.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
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
    ReminderUpdateRequest,
    ReportActionItem,
    ReportActionItems,
    ReportApplicationPipeline,
    ReportCollectionHealth,
    ReportOverview,
    ReportRecentlyChangedItem,
    ReportRecommendations,
    ReportReminders,
    ReportResponse,
    ReportReviewQueue,
    ReportReviewQueueItem,
    ReportSourceStatus,
    ReviewDecisionEvent,
    ReviewDecisionHistoryResponse,
    ReviewDecisionItem,
    ReviewDecisionListResponse,
    ReviewDecisionRequest,
    ReviewQueueItem,
    ReviewQueueRequest,
    ReviewQueueResponse,
    SavedSearchCreateRequest,
    SavedSearchExecuteRequest,
    SavedSearchExecutionResponse,
    SavedSearchItem,
    SavedSearchListResponse,
    SavedSearchUpdateRequest,
    SourceStats,
    SourceStatsListResponse,
)
from careeros.applications.models import APPLICATION_STATUSES, validate_status
from careeros.applications.service import (
    ApplicationFilter,
    application_statuses_for_jobs,
    apply_filter_to_flat,
    apply_filter_to_groups,
    set_application_status,
)
from careeros.applications.service import (
    UnknownJobError as ApplicationUnknownJobError,
)
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
    search_grouped,
)
from careeros.database.jobs import get_job_by_id, list_jobs, search_jobs
from careeros.database.reminders import ReminderListEntry, get_reminder
from careeros.database.review import DecisionListItem
from careeros.database.runs import list_runs
from careeros.presenters import (
    action_item_dict,
    application_event_dict,
    application_item_dict,
    application_snapshot_dict,
    group_dict,
    job_dict,
    ranked_group_item_dict,
    ranked_job_item_dict,
    reminder_item_dict,
    report_review_queue_item_dict,
    review_decision_event_dict,
    review_decision_item_dict,
    review_queue_item_dict,
    saved_search_item_dict,
    source_stats_dict,
    source_status_item_dict,
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
    delete_reminder,
    list_reminders,
    reopen_reminder,
    update_reminder,
)
from careeros.reporting.models import Report
from careeros.reporting.service import build_report
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
from careeros.saved_searches.models import (
    DuplicateNameError,
    SavedSearchFields,
    SavedSearchPatch,
    UnknownSearchError,
)
from careeros.saved_searches.models import (
    ValidationError as SavedSearchValidationError,
)
from careeros.saved_searches.service import (
    create_search,
    delete_search,
    get_search,
    list_searches,
    rename_search,
    update_search,
)
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

# The definition fields a generalized PATCH /saved-searches/{name} can update.
# ``name`` is excluded (it is the rename field, dispatched to rename_search)
# and ``mode``/``profile_path``/``description`` are immutable or private over
# HTTP and can never be supplied.
_SAVED_SEARCH_DEFINITION_FIELDS: tuple[str, ...] = (
    "query",
    "grouped",
    "source",
    "remote",
    "location",
    "untracked",
    "include_statuses",
    "exclude_statuses",
    "min_score",
    "result_limit",
)

# The canonical profile-less report notes, mirroring
# ``reporting.service.build_report`` exactly: they describe why the
# profile-dependent review-queue and recommendations sections are empty. The
# missing-database empty report reuses them so it matches a seeded
# profile-less report structurally.
_NO_PROFILE_REVIEW_QUEUE_NOTE = (
    "No profile or saved search provided; no review queue computed."
)
_NO_PROFILE_RECOMMENDATIONS_NOTE = (
    "No profile or saved search provided; no recommendations computed."
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


def _report_response(report: Report) -> ReportResponse:
    """Project a domain ``Report`` onto the HTTP-safe response model.

    An explicit positive allow-list: every field is built field-by-field from
    approved safe values (the shared HTTP-safe job/group/reminder/source-status
    projections), never by serializing the internal ``Report`` wholesale.
    ``notification_summary``, provider ``last_error`` text, notes, reminder
    messages, scoring reasons/components, and feedback are structurally absent.
    ``GET /report`` is profile-less, so the review-queue items and
    recommendation jobs/groups the domain service reports are empty; the
    sections remain present with ``source="none"``.
    """
    overview = report.overview
    pipeline = report.pipeline
    actions = report.actions
    review = report.review
    recommendations = report.recommendations
    return ReportResponse(
        as_of=report.now.isoformat(),
        overview=ReportOverview(
            total_jobs=overview.total_jobs,
            canonical_groups=overview.canonical_groups,
            grouped_members=overview.grouped_members,
            distinct_opportunities=overview.distinct_opportunities,
            tracked_applications=overview.tracked_applications,
            saved_searches=overview.saved_searches,
            reviewed_groups=overview.reviewed_groups,
            unreviewed_groups=overview.unreviewed_groups,
            shortlisted_groups=overview.shortlisted_groups,
            dismissed_groups=overview.dismissed_groups,
            review_later_groups=overview.review_later_groups,
            sources=[
                ReportSourceStatus.model_validate(source_status_item_dict(status))
                for status in overview.source_snapshots
            ],
            due_sources=list(overview.due_sources),
            source_stats=[
                SourceStats.model_validate(source_stats_dict(stats))
                for stats in overview.source_stats
            ],
        ),
        application_pipeline=ReportApplicationPipeline(
            status_counts=dict(pipeline.status_counts),
            recently_changed=[
                ReportRecentlyChangedItem.model_validate(
                    application_snapshot_dict(snapshot)
                )
                for snapshot in pipeline.recently_changed
            ],
        ),
        action_items=ReportActionItems(
            interview=[
                ReportActionItem.model_validate(action_item_dict(item))
                for item in actions.interview
            ],
            offer=[
                ReportActionItem.model_validate(action_item_dict(item))
                for item in actions.offer
            ],
            interested=[
                ReportActionItem.model_validate(action_item_dict(item))
                for item in actions.interested
            ],
            awaiting_progress=[
                ReportActionItem.model_validate(action_item_dict(item))
                for item in actions.awaiting_progress
            ],
        ),
        reminders=ReportReminders(
            overdue_count=report.reminders.overdue_count,
            due_today_utc_count=report.reminders.due_today_utc_count,
            open_total=report.reminders.open_total,
            upcoming=[
                ReminderItem.model_validate(
                    reminder_item_dict(
                        ReminderListEntry(reminder=snapshot.reminder, job=snapshot.job)
                    )
                )
                for snapshot in report.reminders.upcoming
            ],
        ),
        review_queue=ReportReviewQueue(
            grouped=review.grouped,
            source=review.source,
            source_name=review.source_name,
            candidate_count=review.candidate_count,
            note=review.note,
            items=[
                ReportReviewQueueItem.model_validate(
                    report_review_queue_item_dict(item)
                )
                for item in review.items
            ],
        ),
        recommendations=ReportRecommendations(
            grouped=recommendations.grouped,
            source=recommendations.source,
            source_name=recommendations.source_name,
            min_score=recommendations.min_score,
            candidate_count=recommendations.candidate_count,
            note=recommendations.note,
            ranked_jobs=[
                RankedJobItem.model_validate(ranked_job_item_dict(item))
                for item in recommendations.ranked_jobs
            ],
            ranked_groups=[
                RankedGroupItem.model_validate(ranked_group_item_dict(item))
                for item in recommendations.ranked_groups
            ],
        ),
        collection_health=ReportCollectionHealth(
            sources=[
                ReportSourceStatus.model_validate(source_status_item_dict(status))
                for status in report.health.per_source
            ],
        ),
    )


def _empty_report_response(now: datetime) -> ReportResponse:
    """Build the all-zero/empty report for a missing database.

    ``GET /report`` never creates a database file, parent directory, or lock
    file: the missing-database report is a fully static all-zero/empty
    snapshot whose review-queue and recommendations sections match the
    profile-less seeded report (``source="none"`` with the canonical note).
    """
    return ReportResponse(
        as_of=now.isoformat(),
        overview=ReportOverview(
            total_jobs=0,
            canonical_groups=0,
            grouped_members=0,
            distinct_opportunities=0,
            tracked_applications=0,
            saved_searches=0,
            reviewed_groups=0,
            unreviewed_groups=0,
            shortlisted_groups=0,
            dismissed_groups=0,
            review_later_groups=0,
            sources=[],
            due_sources=[],
            source_stats=[],
        ),
        application_pipeline=ReportApplicationPipeline(
            status_counts=dict.fromkeys(APPLICATION_STATUSES, 0),
            recently_changed=[],
        ),
        action_items=ReportActionItems(
            interview=[], offer=[], interested=[], awaiting_progress=[]
        ),
        reminders=ReportReminders(
            overdue_count=0, due_today_utc_count=0, open_total=0, upcoming=[]
        ),
        review_queue=ReportReviewQueue(
            grouped=False,
            source="none",
            source_name=None,
            candidate_count=0,
            note=_NO_PROFILE_REVIEW_QUEUE_NOTE,
            items=[],
        ),
        recommendations=ReportRecommendations(
            grouped=False,
            source="none",
            source_name=None,
            min_score=0,
            candidate_count=0,
            note=_NO_PROFILE_RECOMMENDATIONS_NOTE,
            ranked_jobs=[],
            ranked_groups=[],
        ),
        collection_health=ReportCollectionHealth(sources=[]),
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
            "PUT /applications/{job_id}, POST /reminders, "
            "PATCH /reminders/{reminder_id}, "
            "PUT /reminders/{reminder_id}/complete, "
            "PUT /reminders/{reminder_id}/reopen, "
            "DELETE /reminders/{reminder_id}, "
            "POST /saved-searches, "
            "DELETE /saved-searches/{name}, and "
            "PATCH /saved-searches/{name} write "
            "through the shared review-decision, application-tracking, reminder, "
            "and saved-search domain services "
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
        query: str = "",
        source: str | None = None,
        remote: bool | None = None,
        location: str | None = None,
        employment_type: str | None = None,
        untracked: bool = False,
        include_statuses: Annotated[list[str] | None, Query()] = None,
        exclude_statuses: Annotated[list[str] | None, Query()] = None,
    ) -> JobListResponse:
        """List stored jobs, optionally narrowed by keyword and filters.

        A non-empty ``query`` routes the listing through the shared
        ``search_jobs`` pipeline (case-insensitive substring match across title,
        company, and description); an omitted or empty ``query`` keeps the
        original ``list_jobs`` behavior byte-identical. Filters compose with the
        keyword using AND semantics, ``total`` counts the full filtered result
        set before ``offset``/``limit`` slicing, and ordering stays canonical
        (``published_at DESC NULLS LAST, id ASC``). Descriptions may
        participate in matching but are never returned.

        The optional application-tracking filters ``untracked``,
        ``include_statuses``, and ``exclude_statuses`` mirror the ranked-jobs
        tracking filters and the CLI search vocabulary. With no tracking rule
        active the response is byte-identical to the original listing; with a
        rule active the base keyword/filter set is narrowed through the shared
        application filter (``application_statuses_for_jobs`` and
        ``apply_filter_to_flat``) before pagination. ``untracked=true`` keeps
        only untracked jobs, ``include_statuses`` keeps only tracked jobs whose
        current status is in the selected set, and ``exclude_statuses`` hides
        tracked jobs with the selected statuses while untracked jobs remain
        eligible. Supplied statuses are validated through the shared
        ``validate_status`` vocabulary and the pairwise conflicts
        (``untracked`` with either status list, and the two status lists
        together) are rejected with ``422``; ``total`` counts the full
        post-filter set and ordering stays canonical.
        """
        include = list(include_statuses) if include_statuses else []
        exclude = list(exclude_statuses) if exclude_statuses else []
        if include and exclude:
            raise HTTPException(
                status_code=422,
                detail="include_statuses and exclude_statuses cannot be combined.",
            )
        if untracked and (include or exclude):
            raise HTTPException(
                status_code=422,
                detail=(
                    "untracked cannot be combined with include_statuses or "
                    "exclude_statuses."
                ),
            )
        for status in (*include, *exclude):
            try:
                validate_status(status)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None

        if connection is None:
            return JobListResponse(items=[], total=0, offset=offset, limit=limit)
        if query:
            base = search_jobs(
                connection,
                query,
                remote=remote,
                location=location,
                source=source,
                employment_type=employment_type,
                limit=None,
            )
        else:
            base = list_jobs(
                connection,
                remote=remote,
                location=location,
                source=source,
                employment_type=employment_type,
            )

        application_filter = ApplicationFilter(
            untracked=untracked,
            include_statuses=frozenset(include),
            exclude_statuses=frozenset(exclude),
        )
        if application_filter.applies():
            job_statuses = application_statuses_for_jobs(
                connection, {job.id for job in base}
            )
            matches = apply_filter_to_flat(base, job_statuses, application_filter)
        else:
            matches = base

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

    @app.put("/reminders/{reminder_id}/reopen", response_model=ReminderItem)
    def reopen_reminder_endpoint(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        reminder_id: int,
    ) -> ReminderItem:
        """Reopen a completed reminder (write).

        All logic lives in the shared ``reminders.service.reopen_reminder``
        domain service: this route only translates the canonical unknown-reminder
        error and returns the canonical ``ReminderItem`` projection (exactly the
        shape ``GET /reminders/{reminder_id}`` returns). The writable connection
        runs under the per-database collection lock, so this endpoint
        coordinates with every CLI/collector writer; lock contention maps to a
        privacy-safe ``503`` and a missing or uninitialized database to the
        canonical privacy-safe ``500``. Reopening an already-open reminder is an
        idempotent no-op (no row mutation, original timestamps preserved). The
        route declares no request body, never accepts a message, and never
        creates notification attempts or triggers notification delivery.
        """
        try:
            stored, _changed = reopen_reminder(connection, reminder_id=reminder_id)
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

    @app.delete("/reminders/{reminder_id}", response_model=ReminderItem)
    def delete_reminder_endpoint(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        reminder_id: int,
    ) -> ReminderItem:
        """Delete a reminder (write).

        All logic lives in the shared ``reminders.service.delete_reminder``
        domain service: this route only translates the canonical
        unknown-reminder error and returns the canonical ``ReminderItem``
        projection of the deleted reminder (built from the row the domain
        service returns; the reminder is never re-read after deletion). The
        writable connection runs under the per-database collection lock, so
        this endpoint coordinates with every CLI/collector writer; lock
        contention maps to a privacy-safe ``503`` and a missing or uninitialized
        database to the canonical privacy-safe ``500``. A repeated DELETE of an
        already-deleted reminder is a canonical ``404`` (the resource no longer
        exists); a reminder referenced by ``reminder_notifications`` is
        protected by the existing ``ON DELETE RESTRICT`` constraint and its
        deletion surfaces as the canonical privacy-safe ``500``. The route
        declares no request body, never accepts a message, and never touches
        notification rows.
        """
        try:
            stored = delete_reminder(connection, reminder_id=reminder_id)
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

    @app.patch("/reminders/{reminder_id}", response_model=ReminderItem)
    def update_reminder_endpoint(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        reminder_id: int,
        payload: ReminderUpdateRequest,
    ) -> ReminderItem:
        """Update a reminder's kind and/or due time (write).

        A genuine partial update: the request body may supply ``kind``,
        ``due_at``, or both (at least one is required), and omitted fields
        retain their stored values. All logic lives in the shared
        ``reminders.service.update_reminder`` domain service: this route only
        validates the request body, translates known domain errors, and
        returns the canonical ``ReminderItem`` projection (exactly the shape
        ``GET /reminders/{reminder_id}`` returns). The stored reminder message
        is never accepted, cleared, inspected, or returned: the route always
        calls the domain with ``message=None`` and ``clear_message=False``, so
        a message previously created through the CLI survives HTTP updates
        byte-identically. The writable connection runs under the per-database
        collection lock, so this endpoint coordinates with every CLI/collector
        writer; lock contention maps to a privacy-safe ``503`` and a missing or
        uninitialized database to the canonical privacy-safe ``500``.
        Re-PATCHing identical values is an idempotent no-op (no row mutation,
        original ``updated_at`` preserved), and the update never creates
        notification attempts or triggers notification delivery.
        """
        try:
            stored, _changed = update_reminder(
                connection,
                reminder_id=reminder_id,
                kind=payload.kind,
                due_at=payload.due_at,
                message=None,
                clear_message=False,
            )
        except ReminderUnknownReminderError:
            raise HTTPException(
                status_code=404, detail=_no_reminder_found_message(reminder_id)
            ) from None
        except ReminderValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
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

    @app.post("/saved-searches", response_model=SavedSearchItem)
    def create_saved_search(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        payload: SavedSearchCreateRequest,
    ) -> SavedSearchItem:
        """Create a search-mode saved-search definition (write).

        HTTP authoring supports only ``mode="search"``: the request schema
        structurally restricts ``mode`` to the single literal ``"search"``
        (and defaults to it), so rank-mode creation is impossible over HTTP and
        no ``profile_path``, ``description``, or ``min_score`` is ever
        accepted. All logic lives in the shared
        ``saved_searches.service.create_search`` domain service: this route
        only validates the request body, forces the safe HTTP contract
        (``mode="search"``, ``profile_path=None``, ``description=None``,
        ``min_score=None``), translates known domain errors, and returns the
        canonical ``SavedSearchItem`` projection (exactly the shape
        ``GET /saved-searches/{name}`` returns). The writable connection runs
        under the per-database collection lock, so this endpoint coordinates
        with every CLI/collector writer; lock contention maps to a privacy-safe
        ``503`` and a missing or uninitialized database to the canonical
        privacy-safe ``500``. Creation is intentionally non-idempotent: a
        repeated identical POST fails with the canonical duplicate-name ``422``
        and creates no second row. The route performs no SQL, no commit, and no
        rollback; the domain service owns the transaction.
        """
        fields = SavedSearchFields(
            name=payload.name,
            query=payload.query,
            mode="search",
            description=None,
            profile_path=None,
            grouped=payload.grouped,
            source=payload.source,
            remote=payload.remote,
            location=payload.location,
            untracked=payload.untracked,
            include_statuses=tuple(payload.include_statuses),
            exclude_statuses=tuple(payload.exclude_statuses),
            min_score=None,
            result_limit=payload.result_limit,
        )
        try:
            stored = create_search(connection, fields=fields)
        except DuplicateNameError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except SavedSearchValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return SavedSearchItem.model_validate(saved_search_item_dict(stored))

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

    @app.delete("/saved-searches/{name}", response_model=SavedSearchItem)
    def delete_saved_search(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        name: str,
    ) -> SavedSearchItem:
        """Delete a saved-search definition (write).

        All logic lives in the shared ``saved_searches.service.delete_search``
        domain service: this route only translates the canonical unknown-search
        error and returns the canonical ``SavedSearchItem`` projection of the
        deleted definition (built from the row the domain service returns; the
        definition is never re-read after deletion, and the stored/original
        name casing is preserved). Lookup is case-insensitive through the
        shared service, exactly like ``GET /saved-searches/{name}``. The
        writable connection runs under the per-database collection lock, so
        this endpoint coordinates with every CLI/collector writer; lock
        contention maps to a privacy-safe ``503`` and a missing or
        uninitialized database to the canonical privacy-safe ``500``. A
        repeated DELETE of an already-deleted saved search is a canonical
        ``404`` (the resource no longer exists). The route declares no request
        body, performs no SQL, and never accepts or exposes ``profile_path``
        or ``description``.
        """
        try:
            stored = delete_search(connection, name=name)
        except UnknownSearchError:
            raise HTTPException(
                status_code=404, detail=_no_saved_search_found_message(name)
            ) from None
        return SavedSearchItem.model_validate(saved_search_item_dict(stored))

    @app.patch("/saved-searches/{name}", response_model=SavedSearchItem)
    def update_saved_search(
        connection: Annotated[sqlite3.Connection, Depends(writable_connection)],
        name: str,
        payload: SavedSearchUpdateRequest,
    ) -> SavedSearchItem:
        """Update a saved-search definition, or rename it (write).

        The generalized ``PATCH /saved-searches/{name}`` endpoint dispatches on
        the request body:

        - a rename-only body (``name`` and no definition change, no non-empty
          ``clears``) is handled by the shared
          ``saved_searches.service.rename_search`` domain service, exactly as
          before: source-name lookup is case-insensitive, same-name/case-only
          renames are idempotent no-ops preserving ``updated_at``, a duplicate
          target maps to ``422`` with the canonical domain text, and the
          renamed definition is returned from the domain without being
          re-read;
        - a definition-update body (one or more definition fields and/or
          non-empty ``clears``, and no ``name``) is handled by the shared
          ``saved_searches.service.update_search`` domain service: omitted and
          ``null`` fields retain the stored value, provided non-null values
          update it, ``[]`` empties a stored status list, ``false`` is a real
          supplied boolean, and ``clears`` performs exactly the domain
          supported resets;
        - a body mixing ``name`` with any definition change or non-empty
          ``clears`` is rejected with ``422`` because the two branches are
          dispatched to different domain services.

        The route performs only the thin HTTP-contract validation needed to
        make the request unambiguous (an empty/no-op body, a rename mixed with
        a definition update or clears, a field supplied while also cleared,
        and a status clear mixed with explicitly supplied status-filter
        fields) and delegates all business/domain validation to the existing
        saved-search domain. ``mode`` is immutable, ``profile_path`` and
        ``description`` are never accepted, loaded, cleared, or returned, and
        the stored ``profile_path`` of a rank-mode definition is carried
        forward byte-identically by the domain merge. The writable connection
        runs under the per-database collection lock, so this endpoint
        coordinates with every CLI/collector writer; lock contention maps to a
        privacy-safe ``503`` and a missing or uninitialized database to the
        canonical privacy-safe ``500``. The route performs no SQL, no commit,
        and no rollback; the domain service owns the transaction.
        """
        clears = tuple(payload.clears) if payload.clears else ()

        def _has_definition_change() -> bool:
            return any(
                getattr(payload, field) is not None
                for field in _SAVED_SEARCH_DEFINITION_FIELDS
            )

        if payload.name is not None:
            if _has_definition_change() or clears:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "A saved-search rename cannot be combined with "
                        "definition updates or clears in the same request."
                    ),
                )
            try:
                stored = rename_search(connection, name=name, new_name=payload.name)
            except DuplicateNameError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            except UnknownSearchError:
                raise HTTPException(
                    status_code=404, detail=_no_saved_search_found_message(name)
                ) from None
            except SavedSearchValidationError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            return SavedSearchItem.model_validate(saved_search_item_dict(stored))

        if not _has_definition_change() and not clears:
            raise HTTPException(
                status_code=422,
                detail="No saved-search fields to update were provided.",
            )

        if "source" in clears and payload.source is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot set and clear 'source' in the same request.",
            )
        if "location" in clears and payload.location is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot set and clear 'location' in the same request.",
            )
        if "remote" in clears and payload.remote is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot set and clear 'remote' in the same request.",
            )
        if "min_score" in clears and payload.min_score is not None:
            raise HTTPException(
                status_code=422,
                detail="Cannot set and clear 'min_score' in the same request.",
            )
        if "status_filters" in clears and any(
            getattr(payload, field) is not None
            for field in ("untracked", "include_statuses", "exclude_statuses")
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot clear status filters while supplying untracked, "
                    "include_statuses, or exclude_statuses."
                ),
            )

        patch = SavedSearchPatch(
            query=payload.query,
            grouped=payload.grouped,
            source=payload.source,
            remote=payload.remote,
            location=payload.location,
            untracked=payload.untracked,
            include_statuses=(
                tuple(payload.include_statuses)
                if payload.include_statuses is not None
                else None
            ),
            exclude_statuses=(
                tuple(payload.exclude_statuses)
                if payload.exclude_statuses is not None
                else None
            ),
            min_score=payload.min_score,
            result_limit=payload.result_limit,
        )
        try:
            stored, _changed = update_search(
                connection,
                name=name,
                patch=patch,
                clears=frozenset(clears),
            )
        except UnknownSearchError:
            raise HTTPException(
                status_code=404, detail=_no_saved_search_found_message(name)
            ) from None
        except SavedSearchValidationError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return SavedSearchItem.model_validate(saved_search_item_dict(stored))

    @app.get("/groups", response_model=GroupListResponse)
    def list_groups_endpoint(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
        query: str = "",
        source: str | None = None,
        remote: bool | None = None,
        location: str | None = None,
        employment_type: str | None = None,
        untracked: bool = False,
        include_statuses: Annotated[list[str] | None, Query()] = None,
        exclude_statuses: Annotated[list[str] | None, Query()] = None,
    ) -> GroupListResponse:
        """List distinct opportunities, optionally narrowed by keyword and filters.

        A non-empty ``query`` routes the listing through the shared
        ``search_grouped`` pipeline (case-insensitive substring match across
        title, company, and description on the candidate member rows BEFORE
        grouping, deterministic ``published_at DESC NULLS LAST`` then
        representative-id ordering); an omitted or empty ``query`` keeps the
        original ``list_all_groups`` listing byte-identical. Filters compose
        with the keyword using AND semantics, ``total`` counts the full filtered
        result set before ``offset``/``limit`` slicing, and descriptions may
        participate in matching but are never returned.

        The optional application-tracking filters ``untracked``,
        ``include_statuses``, and ``exclude_statuses`` mirror the
        job-listing/ranked-jobs tracking filters and the CLI search vocabulary.
        With no tracking rule active the response is byte-identical to the
        original listing; with a rule active the completed candidate groups are
        narrowed through the shared application filter
        (``application_statuses_for_jobs`` and ``apply_filter_to_groups``)
        before pagination. ``untracked=true`` keeps only groups whose members
        are all untracked, ``include_statuses`` keeps only tracked groups whose
        canonical group status (the lowest-job-id tracked member's status) is
        in the selected set, and ``exclude_statuses`` hides tracked groups with
        the selected statuses while untracked groups remain eligible. Filtering
        only removes whole groups; it never changes representative selection.
        Supplied statuses are validated through the shared ``validate_status``
        vocabulary and the pairwise conflicts (``untracked`` with either status
        list, and the two status lists together) are rejected with ``422``;
        ``total`` counts the full post-filter set. The response shape and
        ordering stay unchanged; no application status is added to any item.
        """
        include = list(include_statuses) if include_statuses else []
        exclude = list(exclude_statuses) if exclude_statuses else []
        if include and exclude:
            raise HTTPException(
                status_code=422,
                detail="include_statuses and exclude_statuses cannot be combined.",
            )
        if untracked and (include or exclude):
            raise HTTPException(
                status_code=422,
                detail=(
                    "untracked cannot be combined with include_statuses or "
                    "exclude_statuses."
                ),
            )
        for status in (*include, *exclude):
            try:
                validate_status(status)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None

        if connection is None:
            return GroupListResponse(items=[], total=0, offset=offset, limit=limit)
        if query:
            groups = search_grouped(
                connection,
                query,
                remote=remote,
                location=location,
                source=source,
                employment_type=employment_type,
                limit=None,
            )
        else:
            groups = list_all_groups(
                connection,
                remote=remote,
                location=location,
                source=source,
                employment_type=employment_type,
            )

        application_filter = ApplicationFilter(
            untracked=untracked,
            include_statuses=frozenset(include),
            exclude_statuses=frozenset(exclude),
        )
        if application_filter.applies():
            member_ids = {member.id for group in groups for member in group.members}
            statuses = application_statuses_for_jobs(connection, member_ids)
            groups = apply_filter_to_groups(groups, statuses, application_filter)

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
        representative job ID. The optional ``untracked``, ``include_statuses``,
        and ``exclude_statuses`` body fields mirror the CLI tracking filters
        and are validated here (pairwise conflicts and status values through
        the shared ``validate_status``) before the tracking filter is applied
        by the shared ``rank_candidates`` pipeline before scoring. ``total``
        reflects the ranked count after ``minimum_score`` filtering and before
        pagination. A missing database yields an empty envelope without
        creating a file, directory, or schema.
        """
        if connection is None:
            return RankedJobsResponse(items=[], total=0, offset=offset, limit=limit)
        untracked = payload.untracked or False
        include_statuses = (
            tuple(payload.include_statuses) if payload.include_statuses else ()
        )
        exclude_statuses = (
            tuple(payload.exclude_statuses) if payload.exclude_statuses else ()
        )
        if include_statuses and exclude_statuses:
            raise HTTPException(
                status_code=422,
                detail="include_statuses and exclude_statuses cannot be combined.",
            )
        if untracked and (include_statuses or exclude_statuses):
            raise HTTPException(
                status_code=422,
                detail=(
                    "untracked cannot be combined with include_statuses or "
                    "exclude_statuses."
                ),
            )
        for status in (*include_statuses, *exclude_statuses):
            try:
                validate_status(status)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
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
            untracked=untracked,
            include_statuses=include_statuses,
            exclude_statuses=exclude_statuses,
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

    @app.get("/report", response_model=ReportResponse)
    def report(
        connection: Annotated[sqlite3.Connection | None, Depends(read_only_connection)],
        limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 10,
    ) -> ReportResponse:
        """Return a profile-less Career Snapshot dashboard (read-only).

        Builds the report through the shared ``reporting.service.build_report``
        domain service with no profile and no saved search, then projects the
        immutable ``Report`` onto an explicit HTTP-safe allow-list. The
        profile-dependent review-queue and recommendations sections stay
        structurally present with ``source="none"`` and empty items, and
        private/internal fields (notes, reminder messages,
        ``notification_summary``, provider ``last_error`` text, scoring
        reasons/components, paths, and lock metadata) are never exposed.
        ``limit`` caps the report's list-oriented sections (recently changed,
        action items, upcoming reminders). A missing database yields an
        all-zero/empty report without creating a file, directory, or schema;
        an uninitialized or malformed database yields the canonical
        privacy-safe ``500``.
        """
        now = datetime.now(UTC)
        if connection is None:
            return _empty_report_response(now)
        return _report_response(build_report(connection, now=now, limit=limit))

    return app


app = create_app()


__all__: tuple[str, ...] = ("app", "create_app")
