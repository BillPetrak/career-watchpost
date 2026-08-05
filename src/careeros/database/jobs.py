"""Job persistence and retrieval operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from careeros.collectors.models import CollectedJob

_SELECT_COLUMNS = (
    "id, source, source_job_id, title, company, location,"
    " description, url, employment_type, remote_status,"
    " published_at, collected_at, created_at, updated_at"
)


def _stored_job_from_row(row: sqlite3.Row) -> StoredJob:
    """Build a StoredJob from a SQLite row."""
    return StoredJob(
        id=row["id"],
        source=row["source"],
        source_job_id=row["source_job_id"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        description=row["description"],
        url=row["url"],
        employment_type=row["employment_type"],
        remote_status=row["remote_status"],
        published_at=row["published_at"],
        collected_at=row["collected_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@dataclass(frozen=True)
class SaveResult:
    """Summary of a job-storage operation."""

    inserted: int
    updated: int

    @property
    def total(self) -> int:
        """Return the total number of processed jobs."""
        return self.inserted + self.updated


@dataclass(frozen=True)
class StoredJob:
    """A job row read from the database, reusable across search, ranking, and beyond."""

    id: int
    source: str
    source_job_id: str | None
    title: str
    company: str | None
    location: str | None
    description: str | None
    url: str | None
    employment_type: str | None
    remote_status: str | None
    published_at: str | None
    collected_at: str
    created_at: str
    updated_at: str


def search_jobs(
    connection: sqlite3.Connection,
    keyword: str,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    employment_type: str | None = None,
    limit: int | None = 20,
) -> list[StoredJob]:
    """Search jobs by keyword across title, company, and description.

    Optional filters narrow results by remote status, location, source, or
    employment type (partial match). ``limit`` caps the returned rows; None
    returns every match.
    """
    pattern = f"%{keyword}%"
    conditions: list[str] = [
        "(title LIKE ? OR company LIKE ? OR description LIKE ?)",
    ]
    params: list[str | int] = [pattern, pattern, pattern]

    if remote:
        conditions.append("remote_status = 'remote'")

    if location is not None:
        conditions.append("location LIKE ?")
        params.append(f"%{location}%")

    if source is not None:
        conditions.append("source = ?")
        params.append(source)

    if employment_type is not None:
        conditions.append("employment_type LIKE ?")
        params.append(f"%{employment_type}%")

    sql = f"""
        SELECT {_SELECT_COLUMNS}
        FROM jobs
        WHERE {" AND ".join(conditions)}
        ORDER BY published_at DESC NULLS LAST, id ASC
    """
    if limit is None:
        rows = connection.execute(sql, params).fetchall()
    else:
        rows = connection.execute(sql + " LIMIT ?", [*params, limit]).fetchall()

    return [_stored_job_from_row(row) for row in rows]


def list_jobs(
    connection: sqlite3.Connection,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    employment_type: str | None = None,
) -> list[StoredJob]:
    """Return stored jobs with optional filters, in canonical search order.

    Mirrors ``search_jobs`` without a keyword so an export can list every
    stored job deterministically (``published_at DESC NULLS LAST, id ASC``)
    with the same filters applied.
    """
    conditions: list[str] = []
    params: list[str] = []

    if remote:
        conditions.append("remote_status = 'remote'")

    if location is not None:
        conditions.append("location LIKE ?")
        params.append(f"%{location}%")

    if source is not None:
        conditions.append("source = ?")
        params.append(source)

    if employment_type is not None:
        conditions.append("employment_type LIKE ?")
        params.append(f"%{employment_type}%")

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    rows = connection.execute(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM jobs
        {where_clause}
        ORDER BY published_at DESC NULLS LAST, id ASC
        """,
        params,
    ).fetchall()
    return [_stored_job_from_row(row) for row in rows]


def get_job_by_id(
    connection: sqlite3.Connection,
    job_id: int,
) -> StoredJob | None:
    """Retrieve a single job by its database ID, or None if not found."""
    row = connection.execute(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()

    if row is None:
        return None
    return _stored_job_from_row(row)


def save_jobs(
    connection: sqlite3.Connection,
    jobs: Iterable[CollectedJob],
) -> SaveResult:
    """Insert new jobs and update previously collected jobs."""
    inserted = 0
    updated = 0

    for job in jobs:
        existing = connection.execute(
            """
            SELECT 1
            FROM jobs
            WHERE source = ? AND source_job_id = ?
            """,
            (job.source, job.source_job_id),
        ).fetchone()

        if existing is None:
            inserted += 1
        else:
            updated += 1

        connection.execute(
            """
            INSERT INTO jobs (
                source,
                source_job_id,
                title,
                company,
                location,
                description,
                url,
                employment_type,
                remote_status,
                published_at,
                collected_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_job_id) DO UPDATE SET
                title = excluded.title,
                company = excluded.company,
                location = excluded.location,
                description = excluded.description,
                url = excluded.url,
                employment_type = excluded.employment_type,
                remote_status = excluded.remote_status,
                published_at = excluded.published_at,
                collected_at = excluded.collected_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                job.source,
                job.source_job_id,
                job.title,
                job.company,
                job.location,
                job.description,
                job.url,
                job.employment_type,
                job.remote_status,
                job.published_at,
                job.collected_at,
            ),
        )

    connection.commit()
    return SaveResult(inserted=inserted, updated=updated)


def list_all_jobs(connection: sqlite3.Connection) -> list[StoredJob]:
    """Return every stored job in ascending database-id order."""
    rows = connection.execute(
        f"SELECT {_SELECT_COLUMNS} FROM jobs ORDER BY id"
    ).fetchall()
    return [_stored_job_from_row(row) for row in rows]
