"""Tests for job persistence."""

from pathlib import Path

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema


def make_job(*, title: str = "Python Engineer") -> CollectedJob:
    """Create a job suitable for persistence tests."""
    return CollectedJob(
        source="arbeitnow",
        source_job_id="python-engineer-example",
        title=title,
        company="Example Company",
        location="Athens",
        description="Build software.",
        url="https://example.com/job",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-07-29T12:00:00+00:00",
    )


def test_save_jobs_inserts_new_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        result = save_jobs(connection, [make_job()])

        stored = connection.execute(
            "SELECT title FROM jobs"
        ).fetchone()

    assert result.inserted == 1
    assert result.updated == 0
    assert stored is not None
    assert stored["title"] == "Python Engineer"


def test_save_jobs_updates_existing_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)

        first_result = save_jobs(connection, [make_job()])
        second_result = save_jobs(
            connection,
            [make_job(title="Senior Python Engineer")],
        )

        rows = connection.execute(
            "SELECT title FROM jobs"
        ).fetchall()

    assert first_result.inserted == 1
    assert second_result.updated == 1
    assert len(rows) == 1
    assert rows[0]["title"] == "Senior Python Engineer"
