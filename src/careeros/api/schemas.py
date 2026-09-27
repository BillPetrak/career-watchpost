"""Pydantic request and response schemas for the CareerOS API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


class CareerProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialVersionItem(BaseModel):
    version_id: str
    version_number: int
    display_filename: str
    declared_media_type: str
    byte_size: int
    created_at: str
    is_current: bool


class MaterialSummary(BaseModel):
    material_id: str
    name: str
    category: str
    status: str
    revision: int
    version_count: int
    current_version: MaterialVersionItem | None
    retired_at: str | None
    created_at: str
    updated_at: str


class MaterialDetail(MaterialSummary):
    versions: list[MaterialVersionItem]


class MaterialListResponse(BaseModel):
    items: list[MaterialSummary]
    total: int
    offset: int
    limit: int


class CareerProfilePage(CareerProfileResponse):
    items: list[JsonValue]
    total: int
    offset: int
    limit: int


class CareerProfileSourcesResponse(CareerProfileResponse):
    items: list[CareerProfileSource]
    total: int
    offset: int
    limit: int


class CareerProfileAnalyzeResponse(CareerProfileResponse):
    material_id: str
    version_id: str
    extraction_run_id: str
    fact_run_id: str
    candidate_count: int
    outcome: str


class CareerProfileCandidatesResponse(CareerProfileResponse):
    items: list[CareerProfileCandidateSummary]
    total: int
    offset: int
    limit: int


class CareerProfileFactRun(CareerProfileResponse):
    id: str
    outcome: str
    mode: str
    input_kind: str
    created_at: str
    recognized: bool
    candidate_count: int | None = None


class CareerProfileCandidateSummary(CareerProfileResponse):
    id: str
    fact_run_id: str
    fact_type: str
    value: JsonValue
    original_value: JsonValue
    confidence: float | None = None
    origin: str
    review: CareerProfileReviewProjection
    merge_eligible_candidate_ids: list[str] = Field(default_factory=list)
    merge_eligible_count: int = 0


class CareerProfileExtraction(CareerProfileResponse):
    id: str
    outcome: str
    source_format: str
    created_at: str
    fact_runs: list[CareerProfileFactRun]


class CareerProfileVersionLineage(CareerProfileResponse):
    id: str
    version_number: int
    display_filename: str
    media_type: str
    created_at: str
    extractions: list[CareerProfileExtraction]


class CareerProfileSource(CareerProfileResponse):
    id: str
    name: str
    status: str
    versions: list[CareerProfileVersionLineage]


class CareerProfileReviewProjection(CareerProfileResponse):
    status: Literal["pending", "approved", "approved_edit", "rejected"]
    value: JsonValue | None = None
    event_id: int | None = None
    created_at: str | None = None


class CareerProfileEvidence(CareerProfileResponse):
    ordinal: int
    text: str
    source_label: str
    start_byte: int
    end_byte: int


class CareerProfileReviewEvent(CareerProfileResponse):
    id: int
    candidate_id: str
    decision: Literal["approved", "approved_edit", "rejected"]
    created_at: str


class CareerProfileEvidencePage(CareerProfileResponse):
    items: list[CareerProfileEvidence]
    total: int
    offset: int
    limit: int


class CareerProfileReviewHistoryPage(CareerProfileResponse):
    items: list[CareerProfileReviewEvent]
    total: int
    offset: int
    limit: int


class CareerProfileCandidateResponse(CareerProfileResponse):
    id: str
    fact_run_id: str
    fact_type: str
    value: JsonValue
    original_value: JsonValue
    confidence: float | None = None
    origin: str
    review: CareerProfileReviewProjection
    provenance: CareerProfileProvenance
    evidence: CareerProfileEvidencePage
    review_history: CareerProfileReviewHistoryPage
    merge_eligible_candidate_ids: list[str] = Field(default_factory=list)
    merge_eligible_count: int = 0


class CareerProfileReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    decision: Literal["approved", "approved_edit", "rejected"]
    edited_value: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def validate_edited_value(self) -> CareerProfileReviewRequest:
        if (self.decision == "approved_edit") != (self.edited_value is not None):
            raise ValueError("edited_value must be supplied only for approved_edit")
        return self


class CareerProfileProvenance(CareerProfileResponse):
    extraction_run_id: str
    fact_run_id: str
    source_name: str
    source_format: str


class CareerProfileVersionProjection(CareerProfileResponse):
    id: str
    version_number: int
    parent_version_id: str | None
    origin: str
    created_at: str


class CareerProfileFactProjection(CareerProfileResponse):
    logical_fact_id: str
    fact_type: str
    value: JsonValue
    origin: str
    privacy_classification: Literal["public", "private", "sensitive"]
    sort_order: int
    source_candidate_ids: list[str]
    assembly_primary_candidate_id: str | None = None


class CareerProfileSnapshotProjection(CareerProfileResponse):
    version: CareerProfileVersionProjection
    facts: list[CareerProfileFactProjection]


class CareerProfileCurrentResponse(CareerProfileResponse):
    revision: int
    profile: CareerProfileSnapshotProjection | None


class CareerProfileHistoryResponse(CareerProfileResponse):
    items: list[CareerProfileSnapshotProjection]
    total: int
    offset: int
    limit: int


class CareerProfileVersionResponse(CareerProfileResponse):
    version: CareerProfileVersionProjection
    facts: list[CareerProfileFactProjection]


class CareerProfileSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    expected_revision: int = Field(ge=0)
    entries: list[CareerProfileEntry]


class CareerProfileSaveResponse(BaseModel):
    revision: int
    profile: CareerProfileSnapshotProjection | None


class CareerProfileCandidateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["candidate"]
    primary_candidate_id: str
    supporting_candidate_ids: list[str] = Field(default_factory=list)
    logical_fact_id: str | None = None
    distinct_identity: bool = False
    privacy_override: Literal["public", "private", "sensitive"] | None = None


class CareerProfileExistingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["existing"]
    logical_fact_id: str
    privacy_override: Literal["public", "private", "sensitive"] | None = None


CareerProfileEntry = CareerProfileCandidateEntry | CareerProfileExistingEntry

for _career_profile_model in (
    CareerProfileSourcesResponse,
    CareerProfileCandidateSummary,
    CareerProfileCandidateResponse,
    CareerProfileCurrentResponse,
    CareerProfileVersionResponse,
    CareerProfileSaveRequest,
    CareerProfileSaveResponse,
):
    _career_profile_model.model_rebuild()


class MaterialUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    category: str | None = None

    @model_validator(mode="after")
    def has_update(self) -> MaterialUpdateRequest:
        if self.name is None and self.category is None:
            raise ValueError("at least one material field is required")
        return self


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


class PackageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyPackageRequest(PackageModel):
    pass


class MaterialSelectionRequest(PackageModel):
    material_version_id: str


class PackageAnswerRequest(PackageModel):
    text: str


class PackageConfirmationRequest(PackageModel):
    confirmed: Literal[True]


class PackageRebaseRequest(PackageModel):
    rebase: Literal[True]


class PackageApprovalRequest(PackageModel):
    approved: Literal[True]


class PackageFinalizationRequest(PackageModel):
    finalize: Literal[True]


class PackageDeletionRequest(PackageModel):
    confirm: Literal[True]


class PackageLifecycle(PackageModel):
    has_draft: bool
    draft_approved_for_current_revision: bool
    finalized_count: int
    latest_finalized_at: str | None


class PackageDraftSummary(PackageModel):
    version_id: str
    version_number: int
    revision: int
    preparation_revision: int
    preparation_stale: bool
    job_stale: bool
    job_reconfirmation_required: bool
    approval_status: Literal["not_approved", "stale", "current"]
    created_at: str
    updated_at: str


class PackageHistoryItem(PackageModel):
    version_id: str
    version_number: int
    revision: int
    preparation_revision: int
    approved_at: str
    finalized_at: str


class PackageHistoryPage(PackageModel):
    items: list[PackageHistoryItem]
    total: int
    offset: int
    limit: int


class PackageSummaryResponse(PackageModel):
    application_id: int
    job_id: int
    package_id: str | None
    draft: PackageDraftSummary | None
    history: PackageHistoryPage


class PackageFork(PackageModel):
    version_id: str
    version_number: int


class PackageJobSnapshot(PackageModel):
    job_id: int
    source: str
    source_job_id: str | None
    title: str
    company: str | None
    location: str | None
    url: str | None
    employment_type: str | None
    remote_status: str | None
    published_at: str | None
    updated_at: str
    description_present: bool


class PackageRequirement(PackageModel):
    requirement_id: str
    kind: Literal["material", "link", "question", "task", "deadline", "other"]
    label: str
    detail: str | None
    required: bool
    fulfillment_state: Literal["open", "satisfied", "not_applicable"]
    deadline_at: str | None
    sort_order: int
    origin: Literal["manual"]
    capture_method: Literal["manual"]
    confirmed: bool
    created_at: str
    updated_at: str


class PackageMaterialSelection(PackageModel):
    requirement_id: str
    material_version_id: str
    material_id: str
    material_name: str
    material_category: Literal[
        "resume", "cover_letter_base", "portfolio", "supporting_document", "other"
    ]
    version_number: int
    display_filename: str
    declared_media_type: str
    byte_size: int
    origin: Literal["manual"]
    capture_method: Literal["manual"]
    current_availability: Literal["available", "retired", "unavailable"]
    created_at: str
    updated_at: str


class PackageAnswer(PackageModel):
    requirement_id: str
    text: str
    origin: Literal["manual"]
    capture_method: Literal["manual"]
    confirmed: bool
    confirmed_at: str | None
    created_at: str
    updated_at: str


class PackageCompletenessReason(PackageModel):
    position: int
    code: str
    blocking: bool
    requirement_id: str | None
    message: str


class PackageCompleteness(PackageModel):
    category: Literal["stale", "incomplete", "complete"]
    blocking_count: int
    evaluated_at: str
    reasons: list[PackageCompletenessReason]


class PackageApproval(PackageModel):
    status: Literal["not_approved", "stale", "current"]
    approved_revision: int | None
    approved_at: str | None


class PackageVersionCommon(PackageModel):
    package_id: str
    application_id: int
    job_id: int
    version_id: str
    version_number: int
    revision: int
    preparation_revision: int
    forked_from: PackageFork | None
    job_reconfirmation_required_at: str | None
    created_at: str
    updated_at: str
    job_snapshot: PackageJobSnapshot
    approval: PackageApproval
    requirements: list[PackageRequirement]
    material_selections: list[PackageMaterialSelection]
    answers: list[PackageAnswer]
    completeness: PackageCompleteness


class PackageDraftDetail(PackageVersionCommon):
    state: Literal["draft"]
    preparation_stale: bool
    job_stale: bool
    job_reconfirmation_required: bool


class PackageFinalizedDetail(PackageVersionCommon):
    state: Literal["finalized"]
    finalized_at: str


class ApplicationPipelineItem(ApplicationItem):
    """Pipeline-only application projection with compact preparation state."""

    preparation: dict[str, object] | None = None
    package: PackageLifecycle | None = None


class ApplicationListResponse(BaseModel):
    """A paginated page of tracked applications, newest first."""

    items: list[ApplicationItem]
    total: int
    offset: int
    limit: int


class ApplicationPipelineCounts(BaseModel):
    """Zero-filled current-status and derived pipeline counts."""

    interested: int
    applied: int
    interview: int
    offer: int
    rejected: int
    withdrawn: int
    active: int
    completed: int
    all: int


class ApplicationPipelineResponse(BaseModel):
    """A server-authoritative flat application pipeline page."""

    items: list[ApplicationPipelineItem]
    counts: ApplicationPipelineCounts
    view: Literal[
        "active", "completed", "all", "interested", "applied", "interview", "offer"
    ]
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


class TodayReminderItem(ReminderItem):
    """A Today reminder with optional same-concrete-job application context."""

    active_application_status: (
        Literal["interested", "applied", "interview", "offer"] | None
    ) = None


class TodayReminderSection(BaseModel):
    total: int
    items: list[TodayReminderItem]


class TodayApplicationPreparation(BaseModel):
    category: Literal[
        "unknown", "incomplete", "requirements_satisfied", "user_confirmed_ready"
    ]
    blocking_count: int


class TodayApplicationPackage(BaseModel):
    has_draft: Literal[True]
    draft_approved_for_current_revision: bool
    draft_preparation_revision_stale: bool


class TodayApplicationItem(BaseModel):
    application_id: int
    job_id: int
    current_status: Literal["interested", "applied", "interview", "offer"]
    updated_at: str
    job: Job
    classification: Literal["overdue", "due_today", "attention"]
    next_action: Literal[
        "review_preparation_deadline",
        "complete_preparation",
        "confirm_preparation_ready",
        "review_rebase_package",
        "continue_package",
        "manage_application",
    ]
    earliest_deadline_at: str | None
    preparation: TodayApplicationPreparation | None
    package: TodayApplicationPackage | None


class TodayApplicationSection(BaseModel):
    total: int
    items: list[TodayApplicationItem]


class TodayApplications(BaseModel):
    overdue: TodayApplicationSection
    due_today: TodayApplicationSection
    work: TodayApplicationSection


class TodayShortlistSection(BaseModel):
    total: int
    items: list[ReviewedOpportunityItem]


class TodayReminders(BaseModel):
    overdue: TodayReminderSection
    due_today: TodayReminderSection


class TodayShortlist(BaseModel):
    unstarted: TodayShortlistSection


class TodayReviewNext(BaseModel):
    candidate_total: int
    total: int
    limit: Literal[3]
    minimum_score: Literal[1]
    personalized: bool
    use_learned_preferences: bool
    items: list[ReviewQueueItem]


class TodayReview(BaseModel):
    next: TodayReviewNext


class TodayResponse(BaseModel):
    """One bounded, coherent Action Center snapshot."""

    as_of: str
    timezone: Literal["UTC"]
    today_ends_at: str
    limit: int
    reminders: TodayReminders
    applications: TodayApplications
    shortlist: TodayShortlist
    review: TodayReview


class ReminderCreateRequest(BaseModel):
    """Request body for ``POST /reminders``.

    ``job_id`` must reference an existing stored job; ``kind`` and ``due_at``
    are validated by the existing reminder domain (``validate_kind`` and
    ``parse_due_at``), which this schema deliberately does not duplicate.
    ``due_at`` must be a full ISO-8601 timestamp with an explicit UTC offset
    (``Z`` accepted); naive and date-only values are rejected by the domain.
    Unknown body fields — including any ``message`` or ``note`` — are rejected
    with ``422``: this mutation request uses ``extra="forbid"`` so reminder
    free text is never accepted, persisted, or returned by the HTTP API.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: Annotated[int, Field(ge=1)]
    kind: str
    due_at: str


class ReminderUpdateRequest(BaseModel):
    """Request body for ``PATCH /reminders/{reminder_id}``.

    A genuine partial update: ``kind`` and/or ``due_at`` may be supplied, and
    omitted fields retain their stored values. At least one non-null field is
    required: an empty body or a body whose only fields are ``null`` is
    rejected with ``422``. ``kind`` and ``due_at`` are validated by the
    existing reminder domain (``validate_kind`` and ``parse_due_at``), which
    this schema deliberately does not duplicate. Unknown body fields —
    including any ``message``, ``note``, ``description``, ``profile_path``,
    ``clear_message``, ``completed_at``, or ``job_id`` — are rejected with
    ``422``: this mutation request uses ``extra="forbid"`` so reminder free
    text is never accepted, cleared, or returned by the HTTP API.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str | None = None
    due_at: str | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> ReminderUpdateRequest:
        if self.kind is None and self.due_at is None:
            raise ValueError("At least one of 'kind' or 'due_at' must be provided.")
        return self


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


SavedSearchClearTarget = Literal[
    "source",
    "location",
    "remote",
    "min_score",
    "status_filters",
]


class SavedSearchUpdateRequest(BaseModel):
    """Request body for ``PATCH /saved-searches/{name}``.

    A generalized partial update: either a rename-only body (the replacement
    ``name`` and nothing else) or a definition-update body (one or more
    definition fields and/or ``clears``). Mixing a rename with definition
    updates or non-empty ``clears`` is rejected by the route with ``422``
    because the two branches are dispatched to different domain services
    (``rename_search`` vs ``update_search``). All definition fields follow the
    domain's partial-update semantics: omitted fields and explicit ``null``
    retain the stored value, provided non-null values update it, and ``[]``
    sets a stored status list to empty. ``clears`` uses the narrow literal
    vocabulary of the domain's clear targets (``source``, ``location``,
    ``remote``, ``min_score``, ``status_filters``); supplying a field while
    also clearing it is rejected by the route with ``422``. Definition
    validation (name/query/source/location normalization, status validation,
    include/exclude and untracked conflicts, ``min_score``/``result_limit``
    bounds, and rank-mode rules) is deliberately delegated to the existing
    saved-search domain (``validate_fields`` / ``update_search``), which this
    schema does not duplicate. Unknown body fields — including any
    ``profile_path``, ``description``, ``mode``, ``message``, ``note``, ``id``,
    ``created_at``, or ``updated_at`` — are rejected with ``422``: this
    mutation request uses ``extra="forbid"`` so no private or local-only field
    is ever accepted, persisted, or returned, and the stored ``profile_path``
    of a rank-mode definition is never accepted, cleared, loaded, or exposed.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    query: str | None = None
    grouped: bool | None = None
    source: str | None = None
    remote: bool | None = None
    location: str | None = None
    untracked: bool | None = None
    include_statuses: list[str] | None = None
    exclude_statuses: list[str] | None = None
    min_score: int | None = None
    result_limit: int | None = None
    clears: list[SavedSearchClearTarget] | None = None


class SavedSearchCreateRequest(BaseModel):
    """Request body for ``POST /saved-searches``.

    Search-mode saved-search authoring only: ``mode`` is structurally restricted
    to the single literal ``"search"`` (and defaults to it), so rank-mode
    creation is impossible over HTTP — rank workflows require a local
    ``profile_path``, which the HTTP API never accepts. All definition fields
    (name normalization, query validation, source/location validation, status
    validation, include/exclude and untracked conflicts, ``result_limit``
    validity, and duplicate-name handling) are validated by the existing
    saved-search domain (``validate_fields`` / ``create_search``), which this
    schema deliberately does not duplicate. Unknown body fields — including any
    ``profile_path``, ``description``, ``min_score``, ``message``, ``note``,
    ``id``, ``created_at``, or ``updated_at`` — are rejected with ``422``: this
    mutation request uses ``extra="forbid"`` so no private or local-only field
    is ever accepted, persisted, or returned.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    query: str
    mode: Literal["search"] = "search"
    grouped: bool = False
    source: str | None = None
    remote: bool | None = None
    location: str | None = None
    untracked: bool = False
    include_statuses: list[str] = Field(default_factory=list)
    exclude_statuses: list[str] = Field(default_factory=list)
    result_limit: int = 20


class SavedSearchExecuteRequest(BaseModel):
    """Request body for ``POST /saved-searches/{name}/execute``.

    ``profile`` is required only for ``rank``-mode saved searches (mirroring the
    candidate-profile file semantics through the shared
    :class:`CandidateProfileRequest`); it must be absent for ``search``-mode
    ones. Unknown top-level fields, including an attempted ``profile_path``
    probe, are silently ignored and never cause filesystem access: the stored
    ``profile_path`` of a saved search is never read or dereferenced.
    """

    model_config = ConfigDict(extra="ignore")

    profile: CandidateProfileRequest | None = None
    use_learned_preferences: bool = True


class SavedSearchExecutionResponse(BaseModel):
    """The result of executing one stored saved search.

    ``mode`` is the stored definition's mode (``search`` or ``rank``) and
    ``grouped`` the stored definition's grouping flag, echoed so the envelope is
    self-describing. ``items`` are exactly one of the four approved shapes per
    mode/grouping combination: canonical :class:`Job` projections (search,
    flat), canonical :class:`Group` projections (search, grouped),
    :class:`RankedJobItem` values (rank, flat), or :class:`RankedGroupItem`
    values (rank, grouped). ``total`` is the length of the pipeline output
    after the stored ``result_limit`` (and stored rank ``min_score``) caps and
    before ``offset``/``limit`` slicing.
    """

    mode: str
    grouped: bool
    items: list[Job | Group | RankedJobItem | RankedGroupItem]
    total: int
    offset: int
    limit: int
    # Present only for the explicit browser snapshot variant.  Ordinary
    # paginated executions omit this field for backwards-compatible shape.
    saved_search: SavedSearchItem | None = None


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


class ClearReviewDecisionResponse(BaseModel):
    """The result of clearing one job's current review decision.

    ``job_id`` echoes the cleared job and ``changed`` is ``True`` when a current
    decision actually existed and was removed, ``False`` when the job was
    already unreviewed (an idempotent no-op that appends no event and mutates no
    row). The response carries only these two fields: review decision notes and
    the decision event history are local workflow state the HTTP API never
    exposes, and after a clear there is no current decision to project.
    """

    job_id: int
    changed: bool


class ApplicationStatusRequest(BaseModel):
    """Request body for ``PUT /applications/{job_id}``.

    ``status`` is validated against the shared application-status vocabulary
    (``interested``, ``applied``, ``interview``, ``offer``, ``rejected``,
    ``withdrawn``) by the existing domain validator; no other field is
    supported. Unknown body fields — including any ``note`` — are rejected
    with ``422``: this mutation request uses ``extra="forbid"`` so an
    unsupported field is never silently accepted and discarded, and the HTTP
    contract never accepts user-written free text.
    """

    model_config = ConfigDict(extra="forbid")

    status: str


class ReviewDecisionRequest(BaseModel):
    """Request body for ``PUT /review-decisions/{job_id}``.

    ``decision`` is validated against the shared review-decision vocabulary
    (``shortlist``, ``dismiss``, ``review_later``) by the existing domain
    validator; no other field is supported. Unknown body fields — including any
    ``note`` — are rejected with ``422``: this mutation request uses
    ``extra="forbid"`` so an unsupported field is never silently accepted and
    discarded.
    """

    model_config = ConfigDict(extra="forbid")

    decision: str


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


class ReviewedOpportunityItem(BaseModel):
    """One grouped or concrete reviewed opportunity."""

    group: Group
    review_decision: Literal["shortlist", "review_later", "dismiss"]
    decision_job_id: int
    decision_updated_at: str
    conflict: bool


class ReviewedOpportunityCounts(BaseModel):
    """Counts across all reviewed opportunities, before selection/pagination."""

    shortlist: int
    review_later: int
    dismiss: int


class ReviewedOpportunityResponse(BaseModel):
    """A page of reviewed opportunities and authoritative decision counts."""

    items: list[ReviewedOpportunityItem]
    counts: ReviewedOpportunityCounts
    grouped: bool
    decision: Literal["shortlist", "review_later", "dismiss"]
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


class ExplicitPreferenceRequest(BaseModel):
    """One complete replacement for persistent explicit preference authority."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

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


class ExplicitPreferenceProfileResponse(BaseModel):
    desired_titles: list[str]
    skills: list[str]
    required_skills: list[str]
    preferred_locations: list[str]
    remote_preference: str | None
    excluded_terms: list[str]
    hard_excluded_terms: list[str]
    preferred_employment_types: list[str]
    preferred_seniority: list[str]


class ExplicitPreferenceStateResponse(BaseModel):
    configured: bool
    updated_at: str | None
    profile: ExplicitPreferenceProfileResponse
    locked_dimensions: list[str]


class LearnedPreferenceExampleResponse(BaseModel):
    job_id: int
    title: str
    company: str | None


class LearnedPreferenceResponse(BaseModel):
    key: str
    dimension: str
    display_value: str
    normalized_value: str
    polarity: Literal["positive", "negative"] | None
    positive_count: int
    negative_count: int
    net_evidence: int
    strength_level: int
    active: bool
    activation_reason: str
    suppressed: bool
    positive_examples: list[LearnedPreferenceExampleResponse]
    negative_examples: list[LearnedPreferenceExampleResponse]


class LearnedPreferenceListResponse(BaseModel):
    items: list[LearnedPreferenceResponse]


class PreferenceResponse(BaseModel):
    explicit: ExplicitPreferenceStateResponse
    learned: list[LearnedPreferenceResponse]
    learning_reset_at: str | None
    learning_reset_review_event_id: int


class LearnedPreferenceSuppressionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suppressed: Annotated[bool, Field(strict=True)]


class RankRequest(BaseModel):
    """Request body for ``POST /ranked-jobs``: an inline candidate profile.

    Unknown top-level fields are silently ignored, so an attempted
    ``profile_path`` (or any other probe) is inert and never acted upon. The
    optional ``untracked``, ``include_statuses``, and ``exclude_statuses``
    fields mirror the CLI search/rank tracking filters; omitted or explicit
    ``null`` means no filter, and status values and combinations are validated
    at the route using the shared application-status vocabulary. The optional
    ``unreviewed``, ``include_decisions``, and ``exclude_decisions`` fields
    mirror the ``GET /jobs``/``GET /groups`` review-state filters; omitted or
    explicit ``null``/``false`` and empty lists mean no filter, and decision
    values and combinations are validated at the route using the shared
    ``validate_decision`` vocabulary. Review decisions are filter-only: they
    influence selection after ranking but are never exposed in the response.
    """

    model_config = ConfigDict(extra="ignore")

    profile: CandidateProfileRequest
    use_learned_preferences: bool = True
    untracked: bool | None = None
    include_statuses: list[str] | None = None
    exclude_statuses: list[str] | None = None
    unreviewed: bool | None = None
    include_decisions: list[str] | None = None
    exclude_decisions: list[str] | None = None


class ExplanationFactor(BaseModel):
    criterion: Literal[
        "title",
        "skill",
        "location",
        "remote",
        "employment",
        "seniority",
        "required_skill",
        "preferred_skill",
        "description",
        "excluded_term",
        "hard_exclusion",
        "feedback",
        "learned_preference",
    ]
    label: str
    value: str | None
    effect: Literal["positive", "negative", "neutral", "blocking", "unknown"]
    points: int


class ExplanationScore(BaseModel):
    base_score: int
    preference_adjustment: int
    contribution_total: int
    match_score: int
    resolution: Literal["unchanged", "capped", "floored", "excluded"]


class RankingExplanation(BaseModel):
    scope: Literal["job", "opportunity"]
    listing_count: int
    summary: str
    score: ExplanationScore
    matches: list[ExplanationFactor]
    gaps: list[ExplanationFactor]
    unknowns: list[ExplanationFactor]
    other_factors: list[ExplanationFactor]


class RankedJobItem(BaseModel):
    """One scored job in a ranked response.

    Mirrors the shared ``ranked_job_item_dict`` projection. The public
    ``explanation`` is a deliberate structured projection; raw reasons and
    score components remain private.
    """

    job: Job
    match_score: int
    score: int
    explanation: RankingExplanation


class RankedGroupItem(BaseModel):
    """One scored canonical group in a grouped ranked response.

    Mirrors the shared ``ranked_group_item_dict`` projection. Its explanation
    describes the union-scored opportunity; raw diagnostics and workflow state
    remain private.
    """

    group: Group
    match_score: int
    score: int
    explanation: RankingExplanation


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
    use_learned_preferences: bool = True


class ReviewQueueItem(BaseModel):
    """One ranked daily-review-queue entry.

    Mirrors a narrowly pruned subset of the shared review renderer's queue-item
    shape: the canonical group, score values, resolved workflow state, and the
    structured public ranking explanation. Raw diagnostics and private notes
    remain omitted.
    """

    group: Group
    match_score: int
    score: int
    review_decision: str | None
    application_status: str | None
    explanation: RankingExplanation


class ReviewQueueResponse(BaseModel):
    """A paginated page of ranked review-queue entries, best match first.

    ``total`` is the count of queue entries after ``state``/application/
    ``minimum_score`` filtering and before ``offset``/``limit`` pagination.
    """

    items: list[ReviewQueueItem]
    total: int
    offset: int
    limit: int


class ReportSourceStatus(BaseModel):
    """One source's collection status for the Career Snapshot response.

    A privacy-pruned subset of the report's source-status projection: the
    provider ``last_error`` text is deliberately omitted (it can embed URLs,
    hostnames, local paths, or tracebacks), as are lock/pid/command metadata.
    ``last_attempt`` and ``next_due`` are ISO-8601 strings or ``None``.
    """

    source: str
    min_interval_seconds: int
    documented: bool
    last_attempt: str | None
    last_succeeded: bool | None
    fetched: int
    inserted: int
    updated: int
    next_due: str | None
    due: bool
    incomplete_runs: int


class ReportOverview(BaseModel):
    """Top-level Career Snapshot counters and per-source snapshot.

    ``sources`` holds the privacy-pruned source-health projections,
    ``due_sources`` the source names currently due, and ``source_stats`` the
    unified per-source statistics (mirroring ``GET /sources/stats``).
    """

    total_jobs: int
    canonical_groups: int
    grouped_members: int
    distinct_opportunities: int
    tracked_applications: int
    saved_searches: int
    reviewed_groups: int
    unreviewed_groups: int
    shortlisted_groups: int
    dismissed_groups: int
    review_later_groups: int
    sources: list[ReportSourceStatus]
    due_sources: list[str]
    source_stats: list[SourceStats]


class ReportRecentlyChangedItem(BaseModel):
    """One recently-changed tracked application in the Career Snapshot.

    The latest application note is deliberately omitted: it is user-written
    free text the HTTP API never exposes.
    """

    job: Job
    status: str
    created_at: str
    updated_at: str


class ReportApplicationPipeline(BaseModel):
    """Application-status counts and the most recently changed applications.

    ``status_counts`` carries one count per canonical ``APPLICATION_STATUSES``
    entry, zero-filled, preserving the domain order.
    """

    status_counts: dict[str, int]
    recently_changed: list[ReportRecentlyChangedItem]


class ReportActionItem(BaseModel):
    """One active action item in the Career Snapshot.

    The latest application note is deliberately omitted: it is user-written
    free text the HTTP API never exposes.
    """

    kind: str
    label: str
    job: Job
    status: str
    updated_at: str


class ReportActionItems(BaseModel):
    """Action items grouped by category, in prominence order."""

    interview: list[ReportActionItem]
    offer: list[ReportActionItem]
    interested: list[ReportActionItem]
    awaiting_progress: list[ReportActionItem]


class ReportReminders(BaseModel):
    """Reminder counts and upcoming reminders in the Career Snapshot.

    ``notification_summary`` is deliberately omitted: it is worker/operational
    observability the HTTP API never exposes. Upcoming entries use the
    canonical reminder projection, so reminder messages never appear.
    """

    overdue_count: int
    due_today_utc_count: int
    open_total: int
    upcoming: list[ReminderItem]


class ReportReviewQueueItem(BaseModel):
    """One ranked review-queue entry in the Career Snapshot.

    The structured explanation is public; raw reasons, score components, and
    feedback provenance remain private.
    """

    group: Group
    match_score: int
    explanation: RankingExplanation


class ReportReviewQueue(BaseModel):
    """The daily review queue section of the Career Snapshot.

    ``GET /report`` is profile-less, so ``source`` is ``"none"``, ``items`` is
    empty, and ``candidate_count`` is zero: the section is structurally present
    without any profile scoring.
    """

    grouped: bool
    source: str
    source_name: str | None
    candidate_count: int
    note: str | None
    items: list[ReportReviewQueueItem]


class ReportRecommendations(BaseModel):
    """The recommended-opportunities section of the Career Snapshot.

    ``GET /report`` is profile-less, so ``source`` is ``"none"``,
    ``ranked_jobs``/``ranked_groups`` are empty, and ``candidate_count`` is
    zero: the section is structurally present without any profile scoring.
    """

    grouped: bool
    source: str
    source_name: str | None
    min_score: int
    candidate_count: int
    note: str | None
    ranked_jobs: list[RankedJobItem]
    ranked_groups: list[RankedGroupItem]


class ReportCollectionHealth(BaseModel):
    """Per-source collection health, using the privacy-pruned projection."""

    sources: list[ReportSourceStatus]


class ReportRequest(BaseModel):
    """Request body for ``POST /report``: an optional inline candidate profile.

    ``profile`` is optional and mirrors the ``CandidateProfileRequest``
    semantics (all fields optional, unknown fields silently ignored,
    whitespace-stripped entries, bounded size, duplicates preserved, an empty
    profile valid). The submitted profile is never persisted, logged, or echoed
    in the response. Unknown top-level fields, including an attempted
    ``profile_path`` probe, are silently ignored and never cause filesystem
    access. When ``profile`` is omitted (no body, ``{}``, or an explicit
    ``null``), ``POST /report`` behaves exactly like the profile-less
    ``GET /report``.
    """

    model_config = ConfigDict(extra="ignore")

    profile: CandidateProfileRequest | None = None
    use_learned_preferences: bool = True


class ReportResponse(BaseModel):
    """The profile-less Career Snapshot dashboard response.

    Field order is stable and deterministic; ``as_of`` is the only value that
    changes between requests.
    """

    as_of: str
    overview: ReportOverview
    application_pipeline: ReportApplicationPipeline
    action_items: ReportActionItems
    reminders: ReportReminders
    review_queue: ReportReviewQueue
    recommendations: ReportRecommendations
    collection_health: ReportCollectionHealth


__all__: tuple[str, ...] = (
    "CareerProfilePage",
    "CareerProfileSourcesResponse",
    "CareerProfileAnalyzeResponse",
    "CareerProfileCandidatesResponse",
    "CareerProfileCandidateResponse",
    "CareerProfileReviewRequest",
    "CareerProfileCurrentResponse",
    "CareerProfileHistoryResponse",
    "CareerProfileVersionResponse",
    "CareerProfileSaveRequest",
    "CareerProfileSaveResponse",
    "ApplicationHistoryEvent",
    "ApplicationHistoryResponse",
    "ApplicationItem",
    "ApplicationListResponse",
    "ApplicationPipelineCounts",
    "ApplicationPipelineResponse",
    "ApplicationStatusRequest",
    "CandidateProfileRequest",
    "ClearReviewDecisionResponse",
    "CollectionRun",
    "CollectionRunListResponse",
    "FieldCoverageStats",
    "Group",
    "GroupListResponse",
    "GroupSummaryResponse",
    "ReviewedOpportunityCounts",
    "ReviewedOpportunityItem",
    "ReviewedOpportunityResponse",
    "HealthResponse",
    "Job",
    "JobListResponse",
    "MAX_PROFILE_ITEMS",
    "MAX_PROFILE_TERM",
    "MAX_REMOTE_PREFERENCE",
    "RankRequest",
    "RankedGroupItem",
    "RankedJobItem",
    "RankingExplanation",
    "ExplanationFactor",
    "ExplanationScore",
    "RankedJobsResponse",
    "ReminderCreateRequest",
    "ReportActionItem",
    "ReportActionItems",
    "ReportApplicationPipeline",
    "ReportCollectionHealth",
    "ReportOverview",
    "ReportRecentlyChangedItem",
    "ReportRecommendations",
    "ReportReminders",
    "ReportRequest",
    "ReportResponse",
    "ReportReviewQueue",
    "ReportReviewQueueItem",
    "ReportSourceStatus",
    "ReminderItem",
    "ReminderListResponse",
    "TodayReminderItem",
    "TodayReminderSection",
    "TodayApplicationItem",
    "TodayApplicationSection",
    "TodayApplications",
    "TodayResponse",
    "TodayReminders",
    "TodayReview",
    "TodayReviewNext",
    "TodayShortlist",
    "TodayShortlistSection",
    "ReminderUpdateRequest",
    "ReviewDecisionEvent",
    "ReviewDecisionHistoryResponse",
    "ReviewDecisionItem",
    "ReviewDecisionListResponse",
    "ReviewDecisionRequest",
    "ReviewQueueItem",
    "ReviewQueueRequest",
    "ReviewQueueResponse",
    "SavedSearchClearTarget",
    "SavedSearchCreateRequest",
    "SavedSearchExecuteRequest",
    "SavedSearchExecutionResponse",
    "SavedSearchItem",
    "SavedSearchListResponse",
    "SavedSearchUpdateRequest",
    "SourceStats",
    "SourceStatsListResponse",
)
