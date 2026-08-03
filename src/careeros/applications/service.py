"""Application-tracking workflow service.

This module owns the transition semantics and is presentation-agnostic: it
never writes to stdout or stderr. Raw SQL lives in
``careeros.database.applications``; the CLI only formats results.

Transition rules for :func:`set_application_status`:

- First assignment creates an application row and one history event whose
  ``old_status`` is ``None``, so the first transition is explicit.
- A real transition updates the current status and appends an event.
- Repeating the identical status with no note is an idempotent no-op.
- Repeating the identical status with a note appends a same-status event,
  supporting "note without a status transition".

Every write is transactional: the application-row change and its history event
are committed together, and any failure rolls the whole operation back.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from careeros.applications.models import validate_status
from careeros.database.applications import (
    ApplicationEvent,
    ApplicationListItem,
    SetApplicationResult,
    StoredApplication,
    append_event,
    insert_application,
    update_application_status,
)
from careeros.database.applications import (
    get_application as _get_application,
)
from careeros.database.applications import (
    latest_event_note as _latest_event_note,
)
from careeros.database.applications import (
    list_application_events as _list_application_events,
)
from careeros.database.applications import (
    list_applications as _list_applications,
)
from careeros.database.applications import (
    statuses_for_jobs as _statuses_for_jobs,
)
from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob, get_job_by_id


class ApplicationError(Exception):
    """Base class for application-tracking domain errors."""


class UnknownJobError(ApplicationError):
    """Raised when a status is set for a job id that does not exist."""


@dataclass(frozen=True)
class ApplicationFilter:
    """A reusable application-aware search/rank filter.

    ``include_statuses`` and ``exclude_statuses`` only ever apply to tracked
    jobs; untracked jobs pass both unless ``untracked`` is set. Mutual
    exclusivity between the modes is enforced by callers.
    """

    untracked: bool = False
    include_statuses: frozenset[str] = frozenset()
    exclude_statuses: frozenset[str] = frozenset()

    def applies(self) -> bool:
        """Return True when at least one tracking rule is active."""
        return (
            self.untracked or bool(self.include_statuses) or bool(self.exclude_statuses)
        )

    def keep(self, status: str | None) -> bool:
        """Return True when a job/group with ``status`` survives the filter.

        ``--untracked`` keeps only untracked results; ``--status`` keeps only
        results with a selected status (untracked results are not in the
        selected set); ``--exclude-status`` hides selected statuses while
        untracked results pass.
        """
        if self.untracked:
            return status is None
        if status is None:
            return not self.include_statuses
        if self.include_statuses and status not in self.include_statuses:
            return False
        if status in self.exclude_statuses:
            return False
        return True


@dataclass(frozen=True)
class GroupTrackingInfo:
    """Tracking facts for one canonical group, for display only."""

    status: str | None
    owner: StoredJob | None
    on_representative: bool


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _serialize(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def set_application_status(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    status: str,
    note: str | None = None,
    now: datetime | None = None,
) -> SetApplicationResult:
    """Set or update a job's application status, recording one history event.

    Raises :class:`UnknownJobError` for a missing job and ``ValueError`` for
    an invalid status. All writes are committed in a single transaction.
    """
    validate_status(status)
    if get_job_by_id(connection, job_id) is None:
        raise UnknownJobError(f"No job found with ID {job_id}.")

    timestamp = _serialize(now if now is not None else _utc_now())
    existing = _get_application(connection, job_id)

    try:
        if existing is None:
            application_id = insert_application(
                connection,
                job_id=job_id,
                current_status=status,
                created_at=timestamp,
                updated_at=timestamp,
            )
            append_event(
                connection,
                application_id=application_id,
                old_status=None,
                new_status=status,
                note=note,
                created_at=timestamp,
            )
            connection.commit()
            return SetApplicationResult(
                application_id=application_id,
                created=True,
                changed=True,
            )

        if status != existing.current_status:
            update_application_status(
                connection,
                application_id=existing.id,
                current_status=status,
                updated_at=timestamp,
            )
            append_event(
                connection,
                application_id=existing.id,
                old_status=existing.current_status,
                new_status=status,
                note=note,
                created_at=timestamp,
            )
            connection.commit()
            return SetApplicationResult(
                application_id=existing.id,
                created=False,
                changed=True,
            )

        if note is not None:
            append_event(
                connection,
                application_id=existing.id,
                old_status=status,
                new_status=status,
                note=note,
                created_at=timestamp,
            )
            update_application_status(
                connection,
                application_id=existing.id,
                current_status=status,
                updated_at=timestamp,
            )
            connection.commit()
            return SetApplicationResult(
                application_id=existing.id,
                created=False,
                changed=True,
            )

        return SetApplicationResult(
            application_id=existing.id,
            created=False,
            changed=False,
        )
    except sqlite3.Error:
        connection.rollback()
        raise


def get_application(
    connection: sqlite3.Connection,
    *,
    job_id: int,
) -> StoredApplication | None:
    """Return the application for a job, or None when it is untracked."""
    return _get_application(connection, job_id)


def latest_event_note(
    connection: sqlite3.Connection,
    *,
    application_id: int,
) -> str | None:
    """Return the most recent event's note, or None."""
    return _latest_event_note(connection, application_id=application_id)


def list_applications(
    connection: sqlite3.Connection,
    *,
    statuses: Sequence[str] | None = None,
    limit: int = 20,
) -> list[ApplicationListItem]:
    """Return tracked applications joined with their jobs, newest first."""
    return _list_applications(connection, statuses=statuses, limit=limit)


def list_application_events(
    connection: sqlite3.Connection,
    *,
    application_id: int,
    limit: int = 20,
) -> list[ApplicationEvent]:
    """Return a job's history events, newest first."""
    return _list_application_events(
        connection,
        application_id=application_id,
        limit=limit,
    )


def application_statuses_for_jobs(
    connection: sqlite3.Connection,
    job_ids: Iterable[int],
) -> dict[int, str]:
    """Map job ids to their current application status (tracked subset only)."""
    return _statuses_for_jobs(connection, job_ids)


def group_tracked_status(
    group: GroupedJob,
    statuses: Mapping[int, str],
) -> str | None:
    """Return the group's tracked status, or None when no member is tracked.

    A canonical group is tracked when any of its current members has an
    application record; the deterministic representative status is the lowest-
    id tracked member's status.
    """
    for member in group.members:
        if member.id in statuses:
            return statuses[member.id]
    return None


def group_tracking_info(
    group: GroupedJob,
    statuses: Mapping[int, str],
) -> GroupTrackingInfo:
    """Describe how a canonical group is tracked, for display only."""
    for member in group.members:
        if member.id in statuses:
            return GroupTrackingInfo(
                status=statuses[member.id],
                owner=member,
                on_representative=member.id == group.representative.id,
            )
    return GroupTrackingInfo(status=None, owner=None, on_representative=False)


def apply_filter_to_flat(
    jobs: Sequence[StoredJob],
    statuses: Mapping[int, str],
    application_filter: ApplicationFilter,
) -> list[StoredJob]:
    """Filter flat results with a reusable application-aware filter."""
    return [job for job in jobs if application_filter.keep(statuses.get(job.id))]


def apply_filter_to_groups(
    groups: Sequence[GroupedJob],
    statuses: Mapping[int, str],
    application_filter: ApplicationFilter,
) -> list[GroupedJob]:
    """Filter grouped results with a reusable application-aware filter.

    Filtering only ever removes whole groups; it never changes which member is
    the representative or how a surviving group is scored.
    """
    return [
        group
        for group in groups
        if application_filter.keep(group_tracked_status(group, statuses))
    ]
