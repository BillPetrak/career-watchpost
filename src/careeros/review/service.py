"""Review-decision workflow and daily review queue service.

This module owns validation, transaction semantics, the injected-clock seam,
and queue generation. It is presentation-agnostic: it never writes to stdout or
stderr. Raw SQL lives in ``careeros.database.review``; the CLI and the reporting
service only call these functions.

Decision rules:

- Setting a new decision creates the current state and appends one history
  event whose ``old_decision`` is ``None``.
- A real transition updates the current decision and appends an event.
- Repeating the identical decision with no note is an idempotent no-op.
- Repeating the identical decision with a note appends a same-decision audit
  event while retaining the same current state.
- Clearing an existing decision removes the current row and appends an event
  with ``new_decision`` ``None``.
- Clearing an already-unreviewed job is an idempotent no-op.

Queue generation is evaluated per canonical duplicate group. A group is
unreviewed only when none of its members has a decision; otherwise the group
carries the resolved decision (conflicts resolve to the lowest ``jobs.id`` and
are surfaced deterministically). Decisions are anchored to ``jobs.id`` (never
the derived grouping tables), so regrouping can never delete or orphan them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from careeros.applications.service import (
    GroupTrackingInfo,
    application_statuses_for_jobs,
    group_tracking_info,
)
from careeros.database.groups import GroupedJob, list_all_groups, search_grouped
from careeros.database.jobs import StoredJob, get_job_by_id
from careeros.database.review import (
    ClearDecisionResult,
    DecisionListItem,
    ReviewEvent,
    SetDecisionResult,
    StoredDecision,
    append_event,
    delete_decision,
    insert_decision,
    update_decision,
)
from careeros.database.review import (
    decision_records_for_jobs as _decision_records_for_jobs,
)
from careeros.database.review import (
    decisions_for_jobs as _decisions_for_jobs,
)
from careeros.database.review import (
    get_decision as _get_decision,
)
from careeros.database.review import (
    list_decision_events as _list_decision_events,
)
from careeros.database.review import (
    list_decisions as _list_decisions,
)
from careeros.preferences.learning import (
    remove_review_evidence,
    replace_review_evidence,
)
from careeros.preferences.models import AdaptivePreferenceSet
from careeros.preferences.service import build_adaptive_preference_set
from careeros.ranking.feedback import FeedbackSet
from careeros.ranking.scorer import CandidateProfile, RankedGroup, score_groups
from careeros.review.models import (
    UnknownJobError,
    ValidationError,
    validate_decision,
    validate_queue_state,
)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _serialize(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


@dataclass(frozen=True)
class GroupDecision:
    """The resolved decision of one canonical group.

    ``decision`` is ``None`` when every member is unreviewed. ``conflict`` is
    True when members disagree and the resolved value had to be picked from the
    lowest ``jobs.id``; ``owner`` is that deciding member when it exists.
    """

    decision: str | None
    conflict: bool
    owner: StoredJob | None


@dataclass(frozen=True)
class ReviewGroupSummary:
    """Group-level review counters for reports and dashboards."""

    reviewed_groups: int
    unreviewed_groups: int
    shortlisted_groups: int
    dismissed_groups: int
    review_later_groups: int


@dataclass(frozen=True)
class QueueItem:
    """One ranked queue entry (a canonical group, or a single job in flat mode)."""

    group: GroupedJob
    group_decision: GroupDecision
    ranked: RankedGroup
    application: GroupTrackingInfo


@dataclass(frozen=True)
class QueueOutcome:
    """A ranked, filtered review queue."""

    grouped: bool
    state: str
    items: tuple[QueueItem, ...]
    candidate_count: int
    qualified_count: int
    min_score: int
    include_tracked: bool


@dataclass(frozen=True)
class ReviewedOpportunity:
    """One reviewed opportunity with its canonical decision owner."""

    group: GroupedJob
    group_decision: GroupDecision
    decision_record: StoredDecision


@dataclass(frozen=True)
class ReviewedOpportunityPage:
    """A deterministic page of reviewed opportunities and all-state counts."""

    grouped: bool
    decision: str
    items: tuple[ReviewedOpportunity, ...]
    counts: Mapping[str, int]
    total: int
    offset: int
    limit: int


def _single_member_group(member: StoredJob) -> GroupedJob:
    """Wrap one job as a single-member group for flat queue evaluation."""
    return GroupedJob(
        group_id=None,
        representative=member,
        members=(member,),
        sources=(member.source,),
        urls=(member.url,),
    )


def resolve_group_decision(
    group: GroupedJob,
    decisions: Mapping[int, str],
) -> GroupDecision:
    """Resolve a canonical group's decision deterministically.

    A group is unreviewed when none of its current members has a decision.
    When several members disagree, the lowest ``jobs.id`` wins and ``conflict``
    is set so callers can surface the ambiguity rather than relying on SQL row
    order.
    """
    decided = [
        (member.id, decisions[member.id])
        for member in group.members
        if member.id in decisions
    ]
    if not decided:
        return GroupDecision(decision=None, conflict=False, owner=None)
    decided.sort(key=lambda pair: pair[0])
    lowest_id, resolved = decided[0]
    distinct = {value for _member_id, value in decided}
    owner = next((member for member in group.members if member.id == lowest_id), None)
    return GroupDecision(
        decision=resolved,
        conflict=len(distinct) > 1,
        owner=owner,
    )


def _state_accepts(state: str, decision: str | None) -> bool:
    """Return True when a resolved decision passes the ``--state`` filter."""
    if state == "any":
        return True
    if state == "unreviewed":
        return decision is None
    return decision == state


def set_decision(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    decision: str,
    note: str | None = None,
    now: datetime | None = None,
) -> SetDecisionResult:
    """Set or update a job's review decision, recording one history event.

    Raises :class:`UnknownJobError` for a missing job and
    :class:`ValidationError` for an invalid decision. All writes are committed
    in a single transaction.
    """
    try:
        validate_decision(decision)
    except ValueError as error:
        raise ValidationError(str(error)) from error
    if get_job_by_id(connection, job_id) is None:
        raise UnknownJobError(f"No job found with ID {job_id}.")

    timestamp = _serialize(now if now is not None else _utc_now())
    existing = _get_decision(connection, job_id)

    try:
        if existing is None:
            decision_id = insert_decision(
                connection,
                job_id=job_id,
                decision=decision,
                created_at=timestamp,
                updated_at=timestamp,
            )
            event_id = append_event(
                connection,
                job_id=job_id,
                old_decision=None,
                new_decision=decision,
                note=note,
                created_at=timestamp,
            )
            if decision in ("shortlist", "dismiss"):
                replace_review_evidence(
                    connection,
                    job_id=job_id,
                    review_event_id=event_id,
                    decision=decision,
                    observed_at=timestamp,
                )
            else:
                remove_review_evidence(connection, job_id=job_id)
            connection.commit()
            return SetDecisionResult(
                decision_id=decision_id,
                created=True,
                changed=True,
            )

        if decision != existing.decision:
            update_decision(
                connection,
                decision_id=existing.id,
                decision=decision,
                updated_at=timestamp,
            )
            event_id = append_event(
                connection,
                job_id=job_id,
                old_decision=existing.decision,
                new_decision=decision,
                note=note,
                created_at=timestamp,
            )
            if decision in ("shortlist", "dismiss"):
                replace_review_evidence(
                    connection,
                    job_id=job_id,
                    review_event_id=event_id,
                    decision=decision,
                    observed_at=timestamp,
                )
            else:
                remove_review_evidence(connection, job_id=job_id)
            connection.commit()
            return SetDecisionResult(
                decision_id=existing.id,
                created=False,
                changed=True,
            )

        if note is not None:
            append_event(
                connection,
                job_id=job_id,
                old_decision=decision,
                new_decision=decision,
                note=note,
                created_at=timestamp,
            )
            update_decision(
                connection,
                decision_id=existing.id,
                decision=decision,
                updated_at=timestamp,
            )
            connection.commit()
            return SetDecisionResult(
                decision_id=existing.id,
                created=False,
                changed=True,
            )

        return SetDecisionResult(
            decision_id=existing.id,
            created=False,
            changed=False,
        )
    except Exception:
        connection.rollback()
        raise


def clear_decision(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    now: datetime | None = None,
) -> ClearDecisionResult:
    """Clear a job's review decision, recording one history event.

    Clearing an already-unreviewed job is an idempotent no-op that appends no
    event. Raises :class:`UnknownJobError` for a missing job.
    """
    if get_job_by_id(connection, job_id) is None:
        raise UnknownJobError(f"No job found with ID {job_id}.")

    existing = _get_decision(connection, job_id)
    if existing is None:
        return ClearDecisionResult(changed=False)

    timestamp = _serialize(now if now is not None else _utc_now())
    try:
        delete_decision(connection, job_id=job_id)
        append_event(
            connection,
            job_id=job_id,
            old_decision=existing.decision,
            new_decision=None,
            note=None,
            created_at=timestamp,
        )
        remove_review_evidence(connection, job_id=job_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return ClearDecisionResult(changed=True)


def get_decision(
    connection: sqlite3.Connection,
    *,
    job_id: int,
) -> StoredDecision | None:
    """Return the current decision for a job, or None when it is unreviewed."""
    return _get_decision(connection, job_id)


def list_decisions(
    connection: sqlite3.Connection,
    *,
    job_id: int | None = None,
    decisions: Sequence[str] | None = None,
    limit: int = 20,
) -> list[DecisionListItem]:
    """Return current decisions joined with their jobs, newest first."""
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    if decisions is not None:
        for decision in decisions:
            try:
                validate_decision(decision)
            except ValueError as error:
                raise ValidationError(str(error)) from error
    return _list_decisions(
        connection,
        job_id=job_id,
        decisions=decisions,
        limit=limit,
    )


def list_decision_events(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    limit: int = 20,
) -> list[ReviewEvent]:
    """Return a job's decision history events, newest first."""
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    return _list_decision_events(connection, job_id=job_id, limit=limit)


def decisions_for_jobs(
    connection: sqlite3.Connection,
    job_ids: Iterable[int],
) -> dict[int, str]:
    """Map job ids to their current review decision (reviewed subset only)."""
    return _decisions_for_jobs(connection, job_ids)


def decision_records_for_jobs(
    connection: sqlite3.Connection,
    job_ids: Iterable[int],
) -> dict[int, StoredDecision]:
    """Map job ids to complete current review decision records."""
    return _decision_records_for_jobs(connection, job_ids)


def list_reviewed_opportunities(
    connection: sqlite3.Connection,
    *,
    decision: str = "shortlist",
    grouped: bool = True,
    offset: int = 0,
    limit: int = 20,
) -> ReviewedOpportunityPage:
    """List reviewed opportunities without ranking or other workflow joins."""
    if offset < 0:
        raise ValidationError("offset must be non-negative.")
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    try:
        validate_decision(decision)
    except ValueError as error:
        raise ValidationError(str(error)) from error

    canonical_groups = list_all_groups(connection)
    member_ids = {member.id for group in canonical_groups for member in group.members}
    records = _decision_records_for_jobs(connection, member_ids)
    decisions = {job_id: record.decision for job_id, record in records.items()}

    opportunities: list[ReviewedOpportunity] = []
    if grouped:
        candidates = canonical_groups
    else:
        candidates = [
            _single_member_group(member)
            for group in canonical_groups
            for member in group.members
        ]

    for group in candidates:
        resolved = resolve_group_decision(group, decisions)
        if resolved.decision is None or resolved.owner is None:
            continue
        record = records.get(resolved.owner.id)
        if record is None:
            continue
        opportunities.append(
            ReviewedOpportunity(
                group=group, group_decision=resolved, decision_record=record
            )
        )

    opportunities.sort(
        key=lambda item: (
            item.decision_record.updated_at,
            item.decision_record.id,
            -item.group.representative.id,
        ),
        reverse=True,
    )
    counts = {
        "shortlist": sum(
            item.group_decision.decision == "shortlist" for item in opportunities
        ),
        "review_later": sum(
            item.group_decision.decision == "review_later" for item in opportunities
        ),
        "dismiss": sum(
            item.group_decision.decision == "dismiss" for item in opportunities
        ),
    }
    selected = [
        item for item in opportunities if item.group_decision.decision == decision
    ]
    return ReviewedOpportunityPage(
        grouped=grouped,
        decision=decision,
        items=tuple(selected[offset : offset + limit]),
        counts=counts,
        total=len(selected),
        offset=offset,
        limit=limit,
    )


def review_group_summary(connection: sqlite3.Connection) -> ReviewGroupSummary:
    """Compute group-level review counters (read-only, no profile needed).

    Every distinct opportunity (canonical group or ungrouped singleton) is
    classified as reviewed or unreviewed, with the decided groups broken down by
    decision value using the same deterministic conflict resolution as the
    queue.
    """
    groups = list_all_groups(connection)
    member_ids = {member.id for group in groups for member in group.members}
    decisions = _decisions_for_jobs(connection, member_ids)

    reviewed = 0
    unreviewed = 0
    shortlisted = 0
    dismissed = 0
    review_later = 0
    for group in groups:
        resolved = resolve_group_decision(group, decisions)
        if resolved.decision is None:
            unreviewed += 1
            continue
        reviewed += 1
        if resolved.decision == "shortlist":
            shortlisted += 1
        elif resolved.decision == "dismiss":
            dismissed += 1
        else:
            review_later += 1

    return ReviewGroupSummary(
        reviewed_groups=reviewed,
        unreviewed_groups=unreviewed,
        shortlisted_groups=shortlisted,
        dismissed_groups=dismissed,
        review_later_groups=review_later,
    )


def build_queue(
    connection: sqlite3.Connection,
    *,
    profile: CandidateProfile,
    state: str = "unreviewed",
    min_score: int = 0,
    include_tracked: bool = False,
    source: str | None = None,
    location: str | None = None,
    remote: bool | None = None,
    employment_type: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
    grouped: bool = True,
    feedback: FeedbackSet | None = None,
    adaptive_preferences: AdaptivePreferenceSet | None = None,
    use_adaptive: bool = True,
) -> QueueOutcome:
    """Build a ranked review queue (read-only).

    Flow: candidate member rows are filtered by ``keyword`` (when non-empty)
    and ``source``/``location``/``remote``/``employment_type`` BEFORE grouping;
    the state filter and the
    default application exclusion are applied before scoring; every eligible
    group is ranked with the shared scorer; ``min_score`` trims the ranked
    list; only then is the final ``limit`` applied. This guarantees the
    ordering is identical to the existing rank/report scorer ordering.

    A non-empty ``keyword`` routes candidate loading through the shared
    ``search_grouped`` pipeline (case-insensitive substring match across title,
    company, and description on the candidate member rows BEFORE grouping), so
    a canonical group only appears when at least one of its members matches
    the keyword; ``None`` or an empty keyword keeps the original
    ``list_all_groups`` loading path byte-identical. ``search_grouped`` is
    called with ``limit=None`` because queue state/application filtering and
    scoring happen before the final ``limit`` cap.

    ``feedback`` carries the review-feedback set (built once by the caller);
    None disables review feedback. Since the default ``state`` excludes
    already-reviewed opportunities, candidates never receive feedback from
    their own decisions.
    """
    validate_queue_state(state)
    if limit < 1:
        raise ValidationError("limit must be at least 1.")
    if not 0 <= min_score <= 100:
        raise ValidationError("min_score must be between 0 and 100.")

    if keyword:
        groups = search_grouped(
            connection,
            keyword,
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
    if not grouped:
        expanded: list[GroupedJob] = []
        for group in groups:
            for member in group.members:
                expanded.append(_single_member_group(member))
        groups = expanded

    member_ids = {member.id for group in groups for member in group.members}
    decisions = _decisions_for_jobs(connection, member_ids)
    statuses = application_statuses_for_jobs(connection, member_ids)

    eligible: list[tuple[GroupedJob, GroupDecision, GroupTrackingInfo]] = []
    for group in groups:
        resolved = resolve_group_decision(group, decisions)
        if not _state_accepts(state, resolved.decision):
            continue
        tracking = group_tracking_info(group, statuses)
        if tracking.status is not None and not include_tracked:
            continue
        eligible.append((group, resolved, tracking))

    candidate_count = len(eligible)

    if not eligible:
        return QueueOutcome(
            grouped=grouped,
            state=state,
            items=(),
            candidate_count=0,
            qualified_count=0,
            min_score=min_score,
            include_tracked=include_tracked,
        )

    if adaptive_preferences is None:
        adaptive_preferences = build_adaptive_preference_set(
            connection,
            supplied_profile=profile,
            enabled=use_adaptive,
        )

    ranked: Sequence[RankedGroup] = score_groups(
        [entry[0] for entry in eligible],
        profile,
        feedback=feedback,
        adaptive_preferences=adaptive_preferences,
    )
    if min_score > 0:
        ranked = [entry for entry in ranked if entry.match_score >= min_score]
    qualified_count = len(ranked)

    meta = {
        group.representative.id: (resolved, tracking)
        for group, resolved, tracking in eligible
    }
    items = []
    for ranked_group in ranked:
        group = ranked_group.group
        resolved, tracking = meta[group.representative.id]
        items.append(
            QueueItem(
                group=group,
                group_decision=resolved,
                ranked=ranked_group,
                application=tracking,
            )
        )

    return QueueOutcome(
        grouped=grouped,
        state=state,
        items=tuple(items[:limit]),
        candidate_count=candidate_count,
        qualified_count=qualified_count,
        min_score=min_score,
        include_tracked=include_tracked,
    )


__all__: tuple[str, ...] = (
    "GroupDecision",
    "QueueItem",
    "QueueOutcome",
    "ReviewedOpportunity",
    "ReviewedOpportunityPage",
    "ReviewGroupSummary",
    "ValidationError",
    "UnknownJobError",
    "build_queue",
    "clear_decision",
    "decisions_for_jobs",
    "decision_records_for_jobs",
    "list_reviewed_opportunities",
    "get_decision",
    "list_decision_events",
    "list_decisions",
    "resolve_group_decision",
    "review_group_summary",
    "set_decision",
    "validate_decision",
)
