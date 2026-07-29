"""Tests for the rank CLI command."""

import json
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema


def _write_profile(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _seed_jobs(connection) -> None:
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
            description="Analyze data with Python and SQL.",
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
    ]
    save_jobs(connection, jobs)


def test_successful_rank(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {
            "desired_titles": ["Python Developer"],
            "skills": ["Python"],
            "preferred_locations": ["Athens"],
            "remote_preference": "remote",
            "excluded_terms": [],
        },
    )

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Python Developer" in captured.out
    assert "Data Scientist" in captured.out
    assert "[Score " in captured.out
    assert "Reasons:" in captured.out


def test_ranking_order(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {"desired_titles": ["Python Developer"], "skills": ["Python"]},
    )

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    lines = captured.out.splitlines()
    score_lines = [line for line in lines if "[Score " in line]
    assert len(score_lines) >= 2
    assert "Python Developer" in score_lines[0]
    assert "Data Scientist" in score_lines[1]


def test_deterministic_tie_break(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Developer"]})

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    run_rank("Developer", profile_path, db_path)
    first = capsys.readouterr()

    run_rank("Developer", profile_path, db_path)
    second = capsys.readouterr()

    assert first.out == second.out


def test_reasons_in_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {"desired_titles": ["Python Developer"], "skills": ["Python"]},
    )

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    run_rank("Developer", profile_path, db_path)
    captured = capsys.readouterr()

    assert "+20" in captured.out
    assert "+8" in captured.out


def test_includes_url(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    run_rank("Developer", profile_path, db_path)
    captured = capsys.readouterr()

    assert "URL:" in captured.out
    assert "https://example.com/1" in captured.out


def test_remote_filter(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Developer", profile_path, db_path, remote=True)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Python Developer" in captured.out
    assert "Rust Engineer" not in captured.out


def test_location_filter(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Data", profile_path, db_path, location="Berlin")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Data Scientist" in captured.out


def test_source_filter(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Developer", profile_path, db_path, source="nonexistent")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No matching jobs found." in captured.out


def test_custom_database_path(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "custom" / "jobs.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Developer", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Python Developer" in captured.out


def test_zero_matches(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    with connect(db_path) as conn:
        initialize_schema(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Nonexistent", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No matching jobs found." in captured.out


def test_invalid_limit_raises_error(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    from careeros.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "rank",
                "engineer",
                "--profile",
                str(profile_path),
                "--limit",
                "0",
            ]
        )

    assert exc_info.value.code != 0


def test_missing_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "nonexistent.json"
    db_path = tmp_path / "careeros.sqlite3"

    with connect(db_path) as conn:
        initialize_schema(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("engineer", profile_path, db_path)

    assert exit_code == 1


def test_malformed_profile(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{broken", encoding="utf-8")
    db_path = tmp_path / "careeros.sqlite3"

    with connect(db_path) as conn:
        initialize_schema(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("engineer", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Failed to load profile" in captured.err


def test_missing_profile_return_code(tmp_path: Path) -> None:
    profile_path = tmp_path / "nonexistent.json"
    db_path = tmp_path / "careeros.sqlite3"

    with connect(db_path) as conn:
        initialize_schema(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("engineer", profile_path, db_path)

    assert exit_code == 1


def test_rank_with_limit(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed_jobs(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Developer", profile_path, db_path, limit=1)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Python Developer" in captured.out
    assert "Data Scientist" not in captured.out
