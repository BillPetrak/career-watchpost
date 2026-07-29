"""Job persistence operations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

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
