"""Tests for job search."""

from pathlib import Path

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import search_jobs
from careeros.database.schema import initialize_schema


def _seed_jobs(connection) -> None:
    """Insert sample jobs for search tests."""
    from careeros.database.jobs import save_jobs

    jobs = [
        CollectedJob(
            source="arbeitnow",
            source_job_id="python-dev-1",
            title="Python Developer",
            company="Tech Corp",
            location="Athens",
            description="Build Python backends.",
            url="https://example.com/1",
            employment_type="Full-time",
            remote_status="remote",
            published_at="2026-07-29T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
        CollectedJob(
            source="arbeitnow",
            source_job_id="data-sci-1",
            title="Data Scientist",
            company="Data Inc",
            location="Berlin",
            description="Analyze data with Python.",
            url="https://example.com/2",
            employment_type="Full-time",
            remote_status="remote",
            published_at="2026-07-28T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
        CollectedJob(
            source="arbeitnow",
            source_job_id="rust-dev-1",
            title="Rust Engineer",
            company="Systems Co",
            location="Athens",
            description="Build systems software.",
            url="https://example.com/3",
            employment_type="Contract",
            remote_status="onsite_or_unspecified",
            published_at="2026-07-27T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
        CollectedJob(
            source="linkedin",
            source_job_id="frontend-1",
            title="Frontend Developer",
            company="Web Ltd",
            location="London",
            description="Build React UIs.",
            url="https://example.com/4",
            employment_type="Full-time",
            remote_status="onsite_or_unspecified",
            published_at="2026-07-26T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
    ]
    save_jobs(connection, jobs)


def test_search_by_title(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "Python")

    assert len(results) == 2
    assert results[0].title == "Python Developer"
    assert results[1].title == "Data Scientist"


def test_search_by_company(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "Tech Corp")

    assert len(results) == 1
    assert results[0].company == "Tech Corp"


def test_search_by_description(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "React")

    assert len(results) == 1
    assert results[0].title == "Frontend Developer"


def test_search_remote_filter(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "Developer", remote=True)

    assert len(results) == 1
    assert results[0].title == "Python Developer"
    assert results[0].remote_status == "remote"


def test_search_location_filter(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "Build", location="Athens")

    assert len(results) == 2
    assert all(job.location == "Athens" for job in results)


def test_search_source_filter(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "Developer", source="linkedin")

    assert len(results) == 1
    assert results[0].source == "linkedin"


def test_search_all_filters_combined(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(
            connection,
            "Developer",
            remote=True,
            location="Athens",
            source="arbeitnow",
        )

    assert len(results) == 1
    assert results[0].title == "Python Developer"


def test_search_no_matches_returns_empty(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "NonExistentKeyword")

    assert results == []


def test_search_respects_limit(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "Python", limit=1)

    assert len(results) == 1


def test_search_empty_database(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        results = search_jobs(connection, "Python")

    assert results == []


def test_search_returns_stored_job_with_all_fields(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)
        results = search_jobs(connection, "Python Developer")

    assert len(results) == 1
    job = results[0]
    assert job.id >= 1
    assert job.source == "arbeitnow"
    assert job.source_job_id == "python-dev-1"
    assert job.title == "Python Developer"
    assert job.company == "Tech Corp"
    assert job.location == "Athens"
    assert job.description == "Build Python backends."
    assert job.url == "https://example.com/1"
    assert job.employment_type == "Full-time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-07-29T10:00:00Z"
    assert job.collected_at is not None
    assert job.created_at is not None
    assert job.updated_at is not None
