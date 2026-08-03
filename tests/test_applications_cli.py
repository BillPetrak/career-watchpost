"""Tests for the application CLI command and its exit codes."""

from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock


def _seed_jobs(connection) -> int:
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
        )
    ]
    save_jobs(connection, jobs)
    return connection.execute(
        "SELECT id FROM jobs WHERE source_job_id = ?",
        ("python-dev-1",),
    ).fetchone()["id"]


def _make_tracked_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    assert (
        cli.main(
            [
                "application",
                "set",
                str(job_id),
                "applied",
                "--note",
                "sent CV",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    cli.main(
        ["application", "set", str(job_id), "interview", "--database", str(db_path)]
    )
    return db_path


def test_application_set_first_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert (
        cli.main(
            [
                "application",
                "set",
                str(job_id),
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert f"Tracked job {job_id} as applied." in captured.out


def test_application_set_same_status_is_noop(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    cli.main(["application", "set", str(job_id), "applied", "--database", str(db_path)])
    capsys.readouterr()

    assert (
        cli.main(
            ["application", "set", str(job_id), "applied", "--database", str(db_path)]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert f"Job {job_id} is already applied; no change." in captured.out


def test_application_set_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(
        ["application", "set", "999", "applied", "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_application_set_invalid_status(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["application", "set", "1", "hired", "--database", str(db_path)])
    assert exc_info.value.code == 2


def test_application_set_invalid_job_id(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["application", "set", "0", "applied", "--database", str(db_path)])
    assert exc_info.value.code == 2


def test_application_show_tracked_output(tmp_path: Path, capsys) -> None:
    db_path = _make_tracked_db(tmp_path)
    with connect(db_path) as connection:
        job_id = connection.execute("SELECT id FROM jobs LIMIT 1").fetchone()["id"]

    exit_code = cli.main(
        ["application", "show", str(job_id), "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Application for job {job_id} (Python Developer)" in captured.out
    assert "Python Developer" in captured.out
    assert "Tech Corp" in captured.out
    assert "arbeitnow" in captured.out
    assert "https://example.com/1" in captured.out
    assert "Interview" in captured.out
    assert "First tracked:" in captured.out
    assert "Last updated:" in captured.out
    assert "sent CV" in captured.out


def test_application_show_untracked_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    exit_code = cli.main(
        ["application", "show", str(job_id), "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Job {job_id} is not currently tracked." in captured.out


def test_application_show_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(["application", "show", "999", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_application_history_output(tmp_path: Path, capsys) -> None:
    db_path = _make_tracked_db(tmp_path)
    with connect(db_path) as connection:
        job_id = connection.execute(
            "SELECT job_id FROM applications LIMIT 1"
        ).fetchone()["job_id"]

    exit_code = cli.main(
        ["application", "history", str(job_id), "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Application history for job {job_id}" in captured.out
    assert "- -> applied" in captured.out
    assert "applied -> interview" in captured.out
    assert "sent CV" in captured.out


def test_application_history_not_tracked(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    exit_code = cli.main(
        ["application", "history", str(job_id), "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Job {job_id} is not currently tracked." in captured.out


def test_application_history_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(["application", "history", "999", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_application_list_output(tmp_path: Path, capsys) -> None:
    db_path = _make_tracked_db(tmp_path)

    exit_code = cli.main(["application", "list", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Python Developer" in captured.out
    assert "Interview" in captured.out
    assert "Tech Corp" in captured.out
    assert "arbeitnow" in captured.out


def test_application_list_status_filter(tmp_path: Path, capsys) -> None:
    db_path = _make_tracked_db(tmp_path)

    exit_code = cli.main(
        ["application", "list", "--status", "applied", "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No applications tracked." in captured.out


def test_application_list_empty(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)

    exit_code = cli.main(["application", "list", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No applications tracked." in captured.out


def test_application_list_invalid_status(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            ["application", "list", "--status", "hired", "--database", str(db_path)]
        )
    assert exc_info.value.code == 2


def test_application_commands_invalid_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["application", "list", "--limit", "0", "--database", str(db_path)])
    assert exc_info.value.code == 2
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            ["application", "history", "1", "--limit", "0", "--database", str(db_path)]
        )
    assert exc_info.value.code == 2


def test_application_custom_database(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "custom" / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert (
        cli.main(
            [
                "application",
                "set",
                str(job_id),
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(["application", "show", str(job_id), "--database", str(db_path)]) == 0
    )
    captured = capsys.readouterr()
    assert "Applied" in captured.out


def _table_rows(db_path: Path, table: str) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
        ]


def test_application_set_conflicts_with_held_lock(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    with collection_lock(db_path):
        exit_code = cli.main(
            [
                "application",
                "set",
                str(job_id),
                "applied",
                "--database",
                str(db_path),
            ]
        )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    assert _table_rows(db_path, "applications") == []


def test_application_read_commands_available_while_locked(
    tmp_path: Path, capsys
) -> None:
    db_path = _make_tracked_db(tmp_path)
    with connect(db_path) as connection:
        job_id = connection.execute(
            "SELECT id FROM jobs ORDER BY id LIMIT 1"
        ).fetchone()["id"]

    before_apps = _table_rows(db_path, "applications")
    before_events = _table_rows(db_path, "application_events")

    with collection_lock(db_path):
        assert (
            cli.main(["application", "show", str(job_id), "--database", str(db_path)])
            == 0
        )
    capsys.readouterr()
    with collection_lock(db_path):
        assert cli.main(["application", "list", "--database", str(db_path)]) == 0
    capsys.readouterr()
    with collection_lock(db_path):
        assert (
            cli.main(
                ["application", "history", str(job_id), "--database", str(db_path)]
            )
            == 0
        )
    capsys.readouterr()

    assert _table_rows(db_path, "applications") == before_apps
    assert _table_rows(db_path, "application_events") == before_events
