"""Pydantic request and response schemas for the CareerOS API."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Bounds for inline candidate-profile fields submitted to POST /ranked-jobs.
# They mirror the candidate-profile file semantics (all optional, unknown
# fields ignored, entries stripped, empty-after-strip rejected, duplicates
# preserved) while bounding request size on the network surface.
MAX_PROFILE_ITEMS = 100
MAX_PROFILE_TERM = 100
MAX_REMOTE_PREFERENCE = 50

ProfileTerm = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_PROFILE_TERM)
]


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


class ApplicationHistoryEvent(BaseModel):
    """One append-only application status transition.

    Mirrors the shared ``application_event_dict`` projection.
    ``old_status`` is ``None`` for the first assignment and ``new_status`` is
    always set, exactly as represented by the domain model; no replacement
    values are invented. The event note and the internal application id are
    deliberately omitted: the note is user-written free text the HTTP API
    never exposes.
    """

    event_id: int
    job_id: int
    old_status: str | None
    new_status: str
    created_at: str


class ApplicationHistoryResponse(BaseModel):
    """A paginated page of one job's application history, newest first."""

    items: list[ApplicationHistoryEvent]
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


class SavedSearchItem(BaseModel):
    """One stored saved-search definition for the HTTP API.

    Mirrors the shared ``saved_search_item_dict`` projection: an explicit
    allow-list of the public definition fields. ``profile_path`` (a local
    filesystem path) and ``description`` (unused user-authored free text) are
    deliberately omitted: the HTTP API never exposes filesystem paths or
    workflow free text. ``query`` is intentionally public — it is the essential
    search definition being viewed, not private workflow annotation.
    ``remote`` is ``None`` (no filter) or ``True`` (remote only), exactly as
    the domain stores it.
    """

    id: int
    name: str
    mode: str
    query: str
    grouped: bool
    source: str | None
    remote: bool | None
    location: str | None
    untracked: bool
    include_statuses: list[str]
    exclude_statuses: list[str]
    min_score: int | None
    result_limit: int
    created_at: str
    updated_at: str


class SavedSearchListResponse(BaseModel):
    """A paginated page of saved-search definitions.

    ``total`` is the count of stored saved searches before pagination.
    """

    items: list[SavedSearchItem]
    total: int
    offset: int
    limit: int


class ReviewDecisionItem(BaseModel):
    """One current review decision joined with its job.

    Mirrors the shared ``review_decision_item_dict`` projection. The latest
    review note and the decision event history are deliberately omitted: they
    are user-written free text and local workflow audit state the HTTP API
    never exposes.
    """

    decision_id: int
    job_id: int
    decision: str
    created_at: str
    updated_at: str
    job: Job


class ReviewDecisionListResponse(BaseModel):
    """A paginated page of current review decisions, newest first."""

    items: list[ReviewDecisionItem]
    total: int
    offset: int
    limit: int


class ReviewDecisionEvent(BaseModel):
    """One append-only review-decision transition.

    Mirrors the shared ``review_decision_event_dict`` projection.
    ``old_decision`` is ``None`` for the first assignment and ``new_decision``
    is ``None`` for a clearing transition, exactly as represented by the domain
    model; no replacement values are invented. The event note is deliberately
    omitted: it is user-written free text the HTTP API never exposes.
    """

    event_id: int
    job_id: int
    old_decision: str | None
    new_decision: str | None
    created_at: str


class ReviewDecisionHistoryResponse(BaseModel):
    """A paginated page of one job's decision history, newest first."""

    items: list[ReviewDecisionEvent]
    total: int
    offset: int
    limit: int


class Group(BaseModel):
    """A distinct opportunity: one canonical duplicate group, or one ungrouped job.

    Mirrors the shared ``group_dict`` projection. ``group_id`` is ``None`` for
    ungrouped singleton jobs. Matcher diagnostics (``reason``,
    ``remote_sig``, ``location_sig``) and the group ``created_at`` are
    deliberately omitted: they are internal matching detail the HTTP API never
    exposes. ``members`` are ordered ascending by job ID, ``sources`` and
    ``urls`` in ordered-distinct form.
    """

    group_id: str | None
    representative: Job
    members: list[Job]
    sources: list[str]
    urls: list[str | None]


class GroupListResponse(BaseModel):
    """A paginated page of distinct opportunities."""

    items: list[Group]
    total: int
    offset: int
    limit: int


class GroupSummaryResponse(BaseModel):
    """Aggregate grouping counts for the current database.

    ``distinct_opportunities`` is derived as ``total_jobs - grouped_jobs +
    groups``, matching the report Overview's definition of a distinct
    opportunity.
    """

    total_jobs: int
    groups: int
    grouped_jobs: int
    distinct_opportunities: int


class CandidateProfileRequest(BaseModel):
    """Candidate preferences submitted inline for a ranking request.

    Mirrors the candidate-profile file semantics: every field is optional,
    unknown fields are silently ignored, list entries are whitespace-stripped
    (entries that become empty after stripping are rejected), duplicates are
    preserved, and an empty profile is valid. The API never reads profile
    files, never persists the submitted profile, and never logs its contents.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    desired_titles: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )
    skills: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )
    required_skills: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )
    preferred_locations: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )
    remote_preference: Annotated[
        str | None, StringConstraints(max_length=MAX_REMOTE_PREFERENCE)
    ] = None
    excluded_terms: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )
    hard_excluded_terms: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )
    preferred_employment_types: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )
    preferred_seniority: list[ProfileTerm] = Field(
        default_factory=list, max_length=MAX_PROFILE_ITEMS
    )


class RankRequest(BaseModel):
    """Request body for ``POST /ranked-jobs``: an inline candidate profile.

    Unknown top-level fields are silently ignored, so an attempted
    ``profile_path`` (or any other probe) is inert and never acted upon.
    """

    model_config = ConfigDict(extra="ignore")

    profile: CandidateProfileRequest


class RankedJobItem(BaseModel):
    """One scored job in a ranked response.

    Mirrors the shared ``ranked_job_item_dict`` projection. ``reasons`` and
    ``score_components`` are deliberately omitted: they can echo submitted
    profile terms or embed private review-feedback text, and the HTTP API never
    exposes profile contents or workflow state.
    """

    job: Job
    match_score: int
    score: int


class RankedGroupItem(BaseModel):
    """One scored canonical group in a grouped ranked response.

    Mirrors the shared ``ranked_group_item_dict`` projection. ``reasons`` and
    ``score_components`` are deliberately omitted: they can echo submitted
    profile terms or embed private review-feedback text, and the HTTP API never
    exposes profile contents or workflow state. Review decisions, application
    status, notes, and reminder messages are likewise never exposed.
    """

    group: Group
    match_score: int
    score: int


class RankedJobsResponse(BaseModel):
    """A paginated page of ranked results, best match first.

    ``items`` are flat raw jobs when ``grouped`` is false and one entry per
    canonical duplicate group when ``grouped`` is true (matching the shared
    grouped ranking pipeline). ``total`` is the count of ranked results after
    ``minimum_score`` filtering and before ``offset``/``limit`` pagination.
    """

    items: list[RankedJobItem | RankedGroupItem]
    total: int
    offset: int
    limit: int


class ReviewQueueRequest(BaseModel):
    """Request body for ``POST /review-queue``: an inline candidate profile.

    Unknown top-level fields are silently ignored, so an attempted
    ``profile_path`` (or any other probe) is inert and never acted upon. The
    API never reads profile files, never persists or logs the submitted
    profile, and never exposes its contents.
    """

    model_config = ConfigDict(extra="ignore")

    profile: CandidateProfileRequest


class ReviewQueueItem(BaseModel):
    """One ranked daily-review-queue entry.

    Mirrors a narrowly pruned subset of the shared review renderer's queue-item
    shape: the canonical group, the two score values, the resolved group
    decision (``None`` when unreviewed), and the group's tracked application
    status (``None`` when untracked). ``reasons``, ``score_components``,
    decision notes, application notes, conflict/owner detail, and reminder
    messages are deliberately omitted: they can echo submitted profile terms or
    embed private free text, which the HTTP API never exposes.
    """

    group: Group
    match_score: int
    score: int
    review_decision: str | None
    application_status: str | None


class ReviewQueueResponse(BaseModel):
    """A paginated page of ranked review-queue entries, best match first.

    ``total`` is the count of queue entries after ``state``/application/
    ``minimum_score`` filtering and before ``offset``/``limit`` pagination.
    """

    items: list[ReviewQueueItem]
    total: int
    offset: int
    limit: int


__all__: tuple[str, ...] = (
    "ApplicationHistoryEvent",
    "ApplicationHistoryResponse",
    "ApplicationItem",
    "ApplicationListResponse",
    "CandidateProfileRequest",
    "CollectionRun",
    "CollectionRunListResponse",
    "FieldCoverageStats",
    "Group",
    "GroupListResponse",
    "GroupSummaryResponse",
    "HealthResponse",
    "Job",
    "JobListResponse",
    "MAX_PROFILE_ITEMS",
    "MAX_PROFILE_TERM",
    "MAX_REMOTE_PREFERENCE",
    "RankRequest",
    "RankedGroupItem",
    "RankedJobItem",
    "RankedJobsResponse",
    "ReminderItem",
    "ReminderListResponse",
    "ReviewDecisionEvent",
    "ReviewDecisionHistoryResponse",
    "ReviewDecisionItem",
    "ReviewDecisionListResponse",
    "ReviewQueueItem",
    "ReviewQueueRequest",
    "ReviewQueueResponse",
    "SavedSearchItem",
    "SavedSearchListResponse",
    "SourceStats",
    "SourceStatsListResponse",
)
