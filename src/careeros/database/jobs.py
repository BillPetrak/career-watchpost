"""Job persistence and retrieval operations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from careeros.collectors.arbeitnow import CollectedJob


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
    limit: int = 20,
) -> list[StoredJob]:
    """Search jobs by keyword across title, company, and description.

    Optional filters narrow results by remote status, location, or source.
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

    params.append(limit)

    rows = connection.execute(
        f"""
        SELECT id, source, source_job_id, title, company, location,
               description, url, employment_type, remote_status,
               published_at, collected_at, created_at, updated_at
        FROM jobs
        WHERE {" AND ".join(conditions)}
        ORDER BY published_at DESC NULLS LAST
        LIMIT ?
        """,
        params,
    ).fetchall()

    result: list[StoredJob] = []
    for row in rows:
        result.append(
            StoredJob(
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
            ),
        )
    return result


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
