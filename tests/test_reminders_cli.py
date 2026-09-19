"""Tests for the reminder CLI command, exit codes, and lock behavior."""

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


def _make_reminder_db(tmp_path: Path) -> tuple[Path, int]:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    assert (
        cli.main(
            [
                "reminder",
                "add",
                str(job_id),
                "follow_up",
                "2026-08-10T14:00:00Z",
                "--message",
                "call recruiter",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    return db_path, job_id


def _table_rows(db_path: Path, table: str) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
        ]


def test_reminder_add_success(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert (
        cli.main(
            [
                "reminder",
                "add",
                str(job_id),
                "interview",
                "2026-08-10T14:00:00Z",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert (
        f"Added reminder 1 for job {job_id} (due 2026-08-10T14:00:00+00:00)."
        in captured.out
    )


def test_reminder_add_custom_database(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "custom" / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert (
        cli.main(
            [
                "reminder",
                "add",
                str(job_id),
                "review",
                "2026-08-10T14:00:00+00:00",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert f"Added reminder 1 for job {job_id}" in captured.out
    assert len(_table_rows(db_path, "reminders")) == 1


def test_reminder_add_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(
        [
            "reminder",
            "add",
            "999",
            "follow_up",
            "2026-08-10T14:00:00Z",
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_reminder_add_invalid_kind(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["reminder", "add", "1", "bogus", "2026-08-10T14:00:00Z"])
    assert exc_info.value.code == 2


def test_reminder_add_invalid_job_id(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "reminder",
                "add",
                "0",
                "follow_up",
                "2026-08-10T14:00:00Z",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_reminder_add_rejects_naive_timestamp(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    exit_code = cli.main(
        [
            "reminder",
            "add",
            str(job_id),
            "follow_up",
            "2026-08-10T14:00:00",
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "naive" in captured.err
    assert _table_rows(db_path, "reminders") == []


def test_reminder_add_rejects_date_only_timestamp(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    exit_code = cli.main(
        [
            "reminder",
            "add",
            str(job_id),
            "follow_up",
            "2026-08-10",
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "time component" in captured.err


def test_reminder_add_rejects_whitespace_message(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    exit_code = cli.main(
        [
            "reminder",
            "add",
            str(job_id),
            "follow_up",
            "2026-08-10T14:00:00Z",
            "--message",
            "   ",
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "empty or whitespace-only" in captured.err


def test_reminder_list_default_shows_open_soonest_first(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    for kind, due_at in (
        ("review", "2026-08-20T09:00:00Z"),
        ("interview", "2026-08-05T09:00:00Z"),
        ("offer", "2026-08-10T09:00:00Z"),
    ):
        assert (
            cli.main(
                [
                    "reminder",
                    "add",
                    str(job_id),
                    kind,
                    due_at,
                    "--database",
                    str(db_path),
                ]
            )
            == 0
        )
    capsys.readouterr()

    assert cli.main(["reminder", "list", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "Showing open reminders (soonest first)" in captured.out
    out = captured.out
    assert out.index("Interview") < out.index("Offer") < out.index("Review")


def test_reminder_list_state_filter(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    assert cli.main(["reminder", "complete", "1", "--database", str(db_path)]) == 0
    capsys.readouterr()

    assert cli.main(["reminder", "list", "--open", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "No reminders found." in captured.out

    assert (
        cli.main(["reminder", "list", "--completed", "--database", str(db_path)]) == 0
    )
    captured = capsys.readouterr()
    assert "Reminder 1" in captured.out
    assert "completed" in captured.out


def test_reminder_list_mutually_exclusive_state(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            ["reminder", "list", "--open", "--completed", "--database", str(db_path)]
        )
    assert exc_info.value.code == 2


def test_reminder_list_invalid_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["reminder", "list", "--limit", "0", "--database", str(db_path)])
    assert exc_info.value.code == 2


def test_reminder_list_invalid_after(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    exit_code = cli.main(
        ["reminder", "list", "--after", "not-a-date", "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Due timestamp" in captured.err


def test_reminder_show_output(tmp_path: Path, capsys) -> None:
    db_path, job_id = _make_reminder_db(tmp_path)

    assert cli.main(["reminder", "show", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "Reminder 1" in captured.out
    assert "Follow up" in captured.out
    assert f"job {job_id}" in captured.out
    assert "2026-08-10T14:00:00+00:00" in captured.out
    assert "call recruiter" in captured.out


def test_reminder_show_unknown(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    exit_code = cli.main(["reminder", "show", "99", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No reminder found with ID 99." in captured.err


def test_reminder_show_zero_id(tmp_path: Path) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["reminder", "show", "0", "--database", str(db_path)])
    assert exc_info.value.code == 2


def test_reminder_complete_output(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)

    assert cli.main(["reminder", "complete", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "Completed reminder 1." in captured.out

    assert cli.main(["reminder", "complete", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "already completed; no change." in captured.out


def test_reminder_reopen_output(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    assert cli.main(["reminder", "complete", "1", "--database", str(db_path)]) == 0
    capsys.readouterr()

    assert cli.main(["reminder", "reopen", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "Reopened reminder 1." in captured.out

    assert cli.main(["reminder", "reopen", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "already open; no change." in captured.out


def test_reminder_update_output(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)

    assert (
        cli.main(
            [
                "reminder",
                "update",
                "1",
                "--kind",
                "interview",
                "--due-at",
                "2026-08-11T09:00:00+02:00",
                "--message",
                "second round",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Updated reminder 1." in captured.out
    assert cli.main(["reminder", "show", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "Interview" in captured.out
    assert "2026-08-11T07:00:00+00:00" in captured.out
    assert "second round" in captured.out


def test_reminder_update_noop(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)

    assert (
        cli.main(
            [
                "reminder",
                "update",
                "1",
                "--kind",
                "follow_up",
                "--due-at",
                "2026-08-10T14:00:00+00:00",
                "--message",
                "call recruiter",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "No changes to reminder 1." in captured.out


def test_reminder_update_clear_message(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)

    assert (
        cli.main(
            [
                "reminder",
                "update",
                "1",
                "--clear-message",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Updated reminder 1." in captured.out
    assert cli.main(["reminder", "show", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "call recruiter" not in captured.out


def test_reminder_update_message_and_clear_conflict(tmp_path: Path) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "reminder",
                "update",
                "1",
                "--message",
                "new",
                "--clear-message",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_reminder_update_unknown(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    exit_code = cli.main(
        ["reminder", "update", "99", "--kind", "interview", "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No reminder found with ID 99." in captured.err


def test_reminder_delete_output(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)

    assert cli.main(["reminder", "delete", "1", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "Deleted reminder 1." in captured.out
    assert _table_rows(db_path, "reminders") == []

    exit_code = cli.main(["reminder", "delete", "1", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No reminder found with ID 1." in captured.err


def test_application_show_includes_reminders(tmp_path: Path, capsys) -> None:
    db_path, job_id = _make_reminder_db(tmp_path)
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
    assert "Reminders:" in captured.out
    assert (
        "- [1] Follow up, due 2026-08-10T14:00:00+00:00, open; call recruiter"
        in captured.out
    )


def test_reminder_add_conflicts_with_held_lock(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    with collection_lock(db_path):
        exit_code = cli.main(
            [
                "reminder",
                "add",
                str(job_id),
                "follow_up",
                "2026-08-10T14:00:00Z",
                "--database",
                str(db_path),
            ]
        )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    assert _table_rows(db_path, "reminders") == []


def test_reminder_complete_conflicts_with_held_lock(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)

    with collection_lock(db_path):
        exit_code = cli.main(["reminder", "complete", "1", "--database", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    with connect(db_path) as connection:
        completed_at = connection.execute(
            "SELECT completed_at FROM reminders WHERE id = 1"
        ).fetchone()["completed_at"]
    assert completed_at is None


def test_reminder_read_commands_available_while_locked(tmp_path: Path, capsys) -> None:
    db_path, _job_id = _make_reminder_db(tmp_path)
    before = _table_rows(db_path, "reminders")

    with collection_lock(db_path):
        assert cli.main(["reminder", "list", "--database", str(db_path)]) == 0
    capsys.readouterr()
    with collection_lock(db_path):
        assert cli.main(["reminder", "show", "1", "--database", str(db_path)]) == 0
    capsys.readouterr()

    assert _table_rows(db_path, "reminders") == before


def test_reminder_commands_use_no_network(tmp_path: Path, capsys, monkeypatch) -> None:
    """The reminder workflow must never touch the network."""

    def _boom(*args, **kwargs):
        raise AssertionError("reminder commands must not fetch over the network")

    import careeros.collectors.http as collector_http

    monkeypatch.setattr(collector_http, "urlopen", _boom)

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert (
        cli.main(
            [
                "reminder",
                "add",
                str(job_id),
                "interview",
                "2026-08-10T14:00:00Z",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["reminder", "list", "--database", str(db_path)]) == 0
    assert cli.main(["reminder", "show", "1", "--database", str(db_path)]) == 0
    capsys.readouterr()
    assert cli.main(["reminder", "complete", "1", "--database", str(db_path)]) == 0
    capsys.readouterr()
    assert cli.main(["reminder", "delete", "1", "--database", str(db_path)]) == 0
    capsys.readouterr()
