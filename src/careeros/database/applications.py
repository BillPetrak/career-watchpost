"""Application-tracking persistence layer.

Stores one current application record per tracked job plus an append-only
history of every status transition. Application records are anchored to the
stable ``jobs.id`` and are deliberately NOT linked to the derived grouping
tables, so ``group regroup`` (which deletes and rebuilds
``job_groups``/``job_group_members``) can never delete or orphan them.

Row-level functions here never commit on their own; callers own the
transaction so that an application-row write and its history event are
committed together atomically.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from careeros.database.jobs import (
    JOB_COLUMNS,
    StoredJob,
    job_from_row,
)

_JOB_COLUMNS: tuple[str, ...] = tuple(JOB_COLUMNS.split(", "))


@dataclass(frozen=True)
class StoredApplication:
    """One current application record, keyed by the tracked job."""

    id: int
    job_id: int
    current_status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ApplicationEvent:
    """A single append-only status-transition history entry."""

    id: int
    application_id: int
    old_status: str | None
    new_status: str
    note: str | None
    created_at: str
    note_redacted_at: str | None = None


@dataclass(frozen=True)
class ApplicationListItem:
    """An application joined with its stored job for display."""

    application: StoredApplication
    job: StoredJob
    latest_note: str | None


@dataclass(frozen=True)
class SetApplicationResult:
    """Outcome of a set-status operation."""

    application_id: int
    created: bool
    changed: bool


@dataclass(frozen=True)
class ApplicationPipelineData:
    """One consistent, flat application-pipeline read snapshot."""

    items: list[ApplicationListItem]
    status_counts: dict[str, int]
    total: int


def preparation_summary_for_applications(
    connection: sqlite3.Connection, application_ids: Sequence[int], *, now: str
) -> dict[int, dict[str, object]]:
    """Return compact readiness summaries in one bounded aggregate query."""
    if not application_ids:
        return {}
    placeholders = ",".join("?" * len(application_ids))
    rows = connection.execute(
        f"""SELECT a.id AS application_id, COALESCE(p.revision, 0) AS revision,
            p.requirements_reviewed_revision, p.ready_confirmed_revision,
            SUM(CASE WHEN i.required=1 AND i.kind != 'deadline'
                AND i.fulfillment_state='open' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN i.confirmed=0 THEN 1 ELSE 0 END) AS unconfirmed_count,
            SUM(CASE WHEN i.required=1 AND i.kind='deadline'
                AND i.fulfillment_state='open' AND i.deadline_at IS NOT NULL
                AND i.deadline_at <= ? THEN 1 ELSE 0 END) AS passed_count
            FROM applications a LEFT JOIN application_preparations p
            ON p.application_id=a.id LEFT JOIN application_requirement_items i
            ON i.application_id=a.id WHERE a.id IN ({placeholders})
            GROUP BY a.id""",
        (now, *application_ids),
    ).fetchall()
    result: dict[int, dict[str, object]] = {}
    for row in rows:
        blocking = int(row["unconfirmed_count"] or 0) + int(row["open_count"] or 0)
        if int(row["passed_count"] or 0):
            blocking += int(row["passed_count"])
        reviewed = row["requirements_reviewed_revision"] == row["revision"]
        if not reviewed:
            blocking += 1
        ready = row["ready_confirmed_revision"] == row["revision"]
        category = (
            "user_confirmed_ready"
            if reviewed and blocking == 0 and ready
            else (
                "requirements_satisfied"
                if reviewed and blocking == 0
                else ("incomplete" if reviewed else "unknown")
            )
        )
        result[int(row["application_id"])] = {
            "category": category,
            "blocking_count": blocking,
        }
    return result


def _application_from_row(row: sqlite3.Row) -> StoredApplication:
    """Build a StoredApplication from a SQLite row."""
    return StoredApplication(
        id=row["id"],
        job_id=row["job_id"],
        current_status=row["current_status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: sqlite3.Row) -> ApplicationEvent:
    """Build an ApplicationEvent from a SQLite row."""
    return ApplicationEvent(
        id=row["id"],
        application_id=row["application_id"],
        old_status=row["old_status"],
        new_status=row["new_status"],
        note=row["note"],
        created_at=row["created_at"],
        note_redacted_at=row["note_redacted_at"]
        if "note_redacted_at" in row.keys()
        else None,
    )


def _list_item_from_row(row: sqlite3.Row) -> ApplicationListItem:
    """Build an ApplicationListItem from a joined applications/jobs row."""
    return ApplicationListItem(
        application=StoredApplication(
            id=row["app_id"],
            job_id=row["job_id"],
            current_status=row["current_status"],
            created_at=row["app_created_at"],
            updated_at=row["app_updated_at"],
        ),
        job=job_from_row(row),
        latest_note=row["latest_note"],
    )


def get_application(
    connection: sqlite3.Connection,
    job_id: int,
) -> StoredApplication | None:
    """Return the application for a job, or None when the job is untracked."""
    row = connection.execute(
        """
        SELECT id, job_id, current_status, created_at, updated_at
        FROM applications
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return _application_from_row(row)


def insert_application(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    current_status: str,
    created_at: str,
    updated_at: str,
) -> int:
    """Create an application row for a job and return its id."""
    cursor = connection.execute(
        """
        INSERT INTO applications (job_id, current_status, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (job_id, current_status, created_at, updated_at),
    )
    application_id = cursor.lastrowid
    if application_id is None:
        raise RuntimeError("insert_application did not return a row id")
    return int(application_id)


def update_application_status(
    connection: sqlite3.Connection,
    *,
    application_id: int,
    current_status: str,
    updated_at: str,
) -> None:
    """Update a stored application's current status and update time."""
    connection.execute(
        """
        UPDATE applications
        SET current_status = ?, updated_at = ?
        WHERE id = ?
        """,
        (current_status, updated_at, application_id),
    )


def append_event(
    connection: sqlite3.Connection,
    *,
    application_id: int,
    old_status: str | None,
    new_status: str,
    note: str | None,
    created_at: str,
) -> int:
    """Append an immutable history event and return its id."""
    cursor = connection.execute(
        """
        INSERT INTO application_events (
            application_id, old_status, new_status, note, created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (application_id, old_status, new_status, note, created_at),
    )
    event_id = cursor.lastrowid
    if event_id is None:
        raise RuntimeError("append_event did not return a row id")
    return int(event_id)


def latest_event_note(
    connection: sqlite3.Connection,
    *,
    application_id: int,
) -> str | None:
    """Return the most recent non-empty note, or None.

    ``latest`` here means the newest event that actually carries a note, so a
    later note-less status transition never hides an earlier note.
    """
    row = connection.execute(
        """
        SELECT note
        FROM application_events
        WHERE application_id = ? AND note IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (application_id,),
    ).fetchone()
    if row is None:
        return None
    value = row["note"]
    return str(value) if value is not None else None


def list_applications(
    connection: sqlite3.Connection,
    *,
    job_id: int | None = None,
    statuses: Sequence[str] | None = None,
    limit: int = 20,
) -> list[ApplicationListItem]:
    """Return tracked applications joined with their jobs.

    Ordering is deterministic: ``created_at DESC`` then ``id DESC``. An
    optional ``statuses`` filter restricts results to those current statuses.
    An optional ``job_id`` filter restricts results to one job's application
    (at most one current row per job, enforced by the ``UNIQUE`` job_id
    constraint; untracked and unknown jobs both return an empty list).
    """
    conditions: list[str] = []
    parameters: list[object] = []
    if job_id is not None:
        conditions.append("a.job_id = ?")
        parameters.append(job_id)
    if statuses:
        placeholders = ", ".join("?" * len(statuses))
        conditions.append(f"a.current_status IN ({placeholders})")
        parameters.extend(statuses)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    rows = connection.execute(
        f"""
        SELECT a.id AS app_id,
               a.job_id AS job_id,
               a.current_status AS current_status,
               a.created_at AS app_created_at,
               a.updated_at AS app_updated_at,
               {", ".join(f"j.{column}" for column in _JOB_COLUMNS)},
               (SELECT e.note
                FROM application_events e
                WHERE e.application_id = a.id AND e.note IS NOT NULL
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT 1) AS latest_note
        FROM applications a
        JOIN jobs j ON j.id = a.job_id
        {where_clause}
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT ?
        """,
        (*parameters, limit),
    ).fetchall()
    return [_list_item_from_row(row) for row in rows]


def application_pipeline(
    connection: sqlite3.Connection,
    *,
    statuses: Sequence[str] | None,
    limit: int,
    offset: int,
) -> ApplicationPipelineData:
    """Read counts, total, and one ordered page in a single snapshot.

    This deliberately omits event notes and uses concrete job ownership only.
    """
    conditions = ""
    parameters: tuple[object, ...] = ()
    if statuses:
        placeholders = ", ".join("?" * len(statuses))
        conditions = f" WHERE current_status IN ({placeholders})"
        parameters = tuple(statuses)
    connection.execute("BEGIN")
    try:
        count_rows = connection.execute(
            "SELECT current_status, COUNT(*) AS count FROM applications "
            "GROUP BY current_status"
        ).fetchall()
        status_counts = {
            str(row["current_status"]): int(row["count"]) for row in count_rows
        }
        total = int(
            connection.execute(
                f"SELECT COUNT(*) AS count FROM applications{conditions}", parameters
            ).fetchone()["count"]
        )
        rows = connection.execute(
            f"""
            SELECT a.id AS app_id, a.job_id AS job_id,
                   a.current_status AS current_status,
                   a.created_at AS app_created_at,
                   a.updated_at AS app_updated_at,
                   {", ".join(f"j.{column}" for column in _JOB_COLUMNS)},
                   NULL AS latest_note
            FROM applications a JOIN jobs j ON j.id = a.job_id
            {conditions.replace("current_status", "a.current_status")}
            ORDER BY a.updated_at DESC, a.id DESC
            LIMIT ? OFFSET ?
            """,
            (*parameters, limit, offset),
        ).fetchall()
    finally:
        connection.rollback()
    return ApplicationPipelineData(
        items=[_list_item_from_row(row) for row in rows],
        status_counts=status_counts,
        total=total,
    )


def list_application_events(
    connection: sqlite3.Connection,
    *,
    application_id: int,
    limit: int = 20,
) -> list[ApplicationEvent]:
    """Return a job's history events, newest first.

    Ordering is deterministic: ``created_at DESC`` then ``id DESC``.
    """
    rows = connection.execute(
        """
        SELECT id, application_id, old_status, new_status, note, created_at,
               note_redacted_at
        FROM application_events
        WHERE application_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (application_id, limit),
    ).fetchall()
    return [_event_from_row(row) for row in rows]


def statuses_for_jobs(
    connection: sqlite3.Connection,
    job_ids: Iterable[int],
) -> dict[int, str]:
    """Return a map of job id -> current status for the tracked subset.

    Untracked jobs are absent from the returned mapping.
    """
    ids = tuple(dict.fromkeys(int(job_id) for job_id in job_ids))
    if not ids:
        return {}
    placeholders = ", ".join("?" * len(ids))
    rows = connection.execute(
        f"""
        SELECT job_id, current_status
        FROM applications
        WHERE job_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {int(row["job_id"]): row["current_status"] for row in rows}
