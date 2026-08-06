"""Tests for job persistence."""

from pathlib import Path

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import JOB_COLUMNS, job_from_row, save_jobs
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

        stored = connection.execute("SELECT title FROM jobs").fetchone()

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

        rows = connection.execute("SELECT title FROM jobs").fetchall()

    assert first_result.inserted == 1
    assert second_result.updated == 1
    assert len(rows) == 1
    assert rows[0]["title"] == "Senior Python Engineer"


def test_job_columns_lists_stored_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        stored = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}

    columns = [column.strip() for column in JOB_COLUMNS.split(",")]

    assert columns == [
        "id",
        "source",
        "source_job_id",
        "title",
        "company",
        "location",
        "description",
        "url",
        "employment_type",
        "remote_status",
        "published_at",
        "collected_at",
        "created_at",
        "updated_at",
    ]
    assert set(columns) <= stored


def test_job_from_row_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(connection, [make_job()])
        row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs").fetchone()

    job = job_from_row(row)

    assert job.id == row["id"]
    assert job.source == "arbeitnow"
    assert job.source_job_id == "python-engineer-example"
    assert job.title == "Python Engineer"
    assert job.company == "Example Company"
    assert job.location == "Athens"
    assert job.description == "Build software."
    assert job.url == "https://example.com/job"
    assert job.employment_type == "Full-time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-07-29T10:00:00Z"
    assert job.collected_at == row["collected_at"]
    assert job.created_at == row["created_at"]
    assert job.updated_at == row["updated_at"]
