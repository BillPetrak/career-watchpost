"""Tests for job retrieval by ID."""

from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import get_job_by_id
from careeros.database.schema import initialize_schema

_SEED_JOB = CollectedJob(
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
)


def _seed_one_job(connection) -> int:
    """Seed _SEED_JOB and return its database ID."""
    from careeros.database.jobs import save_jobs

    save_jobs(connection, [_SEED_JOB])
    return connection.execute(
        "SELECT id FROM jobs WHERE source_job_id = ?",
        (_SEED_JOB.source_job_id,),
    ).fetchone()["id"]


def test_get_job_by_id_returns_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        job_id = _seed_one_job(connection)
        result = get_job_by_id(connection, job_id)

    assert result is not None
    assert result.title == "Python Developer"


def test_get_job_by_id_returns_none_for_missing(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_one_job(connection)
        result = get_job_by_id(connection, 9999)

    assert result is None


def test_get_job_by_id_field_mapping(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        job_id = _seed_one_job(connection)
        result = get_job_by_id(connection, job_id)

    assert result is not None
    assert result.id == job_id
    assert result.source == "arbeitnow"
    assert result.source_job_id == "python-dev-1"
    assert result.title == "Python Developer"
    assert result.company == "Tech Corp"
    assert result.location == "Athens"
    assert result.description == "Build Python backends."
    assert result.url == "https://example.com/1"
    assert result.employment_type == "Full-time"
    assert result.remote_status == "remote"
    assert result.published_at == "2026-07-29T10:00:00Z"
    assert result.collected_at is not None
    assert result.created_at is not None
    assert result.updated_at is not None


def test_show_successful_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)
        job_id = _seed_one_job(connection)

    from careeros.cli import run_show

    exit_code = run_show(job_id, database_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Python Developer" in captured.out
    assert "Tech Corp" in captured.out
    assert "Athens" in captured.out
    assert "remote" in captured.out
    assert "Full-time" in captured.out
    assert "arbeitnow" in captured.out
    assert "python-dev-1" in captured.out
    assert "Build Python backends." in captured.out


def test_show_missing_job_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    from careeros.cli import run_show

    exit_code = run_show(1, database_path)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "No job found with ID 1." in captured.err


def test_show_invalid_job_id(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    from careeros.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["show", "0", "--database", str(database_path)])

    assert exc_info.value.code != 0


def test_show_custom_database(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    custom_db = tmp_path / "custom" / "jobs.sqlite3"

    with connect(custom_db) as connection:
        initialize_schema(connection)
        job_id = _seed_one_job(connection)

    from careeros.cli import run_show

    exit_code = run_show(job_id, custom_db)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Python Developer" in captured.out


def test_clean_escaped_html() -> None:
    from careeros.cli import clean_description

    result = clean_description("&lt;div&gt;Hello &amp; World&lt;/div&gt;")
    assert result == "Hello & World"


def test_clean_ordinary_html() -> None:
    from careeros.cli import clean_description

    html = "<p>First</p><ul><li>A</li><li>B</li></ul>"
    result = clean_description(html)
    assert result == "First\nA\nB"


def test_clean_plain_text() -> None:
    from careeros.cli import clean_description

    result = clean_description("Hello, World!")
    assert result == "Hello, World!"


def test_show_missing_description(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)

        from careeros.database.jobs import save_jobs

        job = CollectedJob(
            source="arbeitnow",
            source_job_id="no-desc-job",
            title="No Description",
            company="Test",
            location="Nowhere",
            description=None,
            url="https://example.com",
            employment_type="Full-time",
            remote_status=None,
            published_at=None,
            collected_at="2026-07-29T12:00:00+00:00",
        )
        save_jobs(connection, [job])
        job_id = connection.execute(
            "SELECT id FROM jobs WHERE source_job_id = ?",
            ("no-desc-job",),
        ).fetchone()["id"]

    from careeros.cli import run_show

    exit_code = run_show(job_id, database_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "N/A" in captured.out
