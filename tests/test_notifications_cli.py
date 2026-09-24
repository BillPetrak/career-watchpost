"""CLI tests for reminder notification delivery (notify-due, notify-history)."""

from pathlib import Path

import pytest

from careeros import cli
from careeros.cli.commands.reminders import deliver
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.locking import notification_lock
from careeros.notifications.models import DeliveryResult


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


def _seed_due_reminder_db(tmp_path: Path) -> tuple[Path, int, int]:
    """Seed one job plus one past-due open reminder; return (db, job_id, id)."""
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    reminder_id = _add_reminder(db_path, job_id)
    return db_path, job_id, reminder_id


def _add_reminder(
    db_path: Path,
    job_id: int,
    kind: str = "follow_up",
    due_at: str = "2020-01-01T00:00:00Z",
    message: str | None = None,
) -> int:
    args = ["reminder", "add", str(job_id), kind, due_at, "--database", str(db_path)]
    if message is not None:
        args += ["--message", message]
    assert cli.main(args) == 0
    with connect(db_path) as connection:
        row = connection.execute("SELECT MAX(id) FROM reminders").fetchone()
    return int(row[0])


def _notification_rows(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT reminder_id, scheduled_due_at, status, error, backend "
                "FROM reminder_notifications ORDER BY id"
            )
        ]


def _notify_due_args(db_path: Path) -> list[str]:
    return [
        "reminder",
        "notify-due",
        "--backend",
        "stdout",
        "--database",
        str(db_path),
    ]


def test_notify_due_dry_run_previews_without_writing(tmp_path: Path, capsys) -> None:
    db_path, job_id, reminder_id = _seed_due_reminder_db(tmp_path)

    exit_code = cli.main([*_notify_due_args(db_path), "--dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Mode: dry-run" in captured.out
    assert "Due reminders: 1" in captured.out
    assert (
        f"[notify] reminder {reminder_id} job {job_id}: Python Developer "
        "(Tech Corp, arbeitnow) - follow_up due" in captured.out
    )
    assert "Delivered: 0" in captured.out
    assert "Dry run: no notification was sent and nothing was written." in captured.out
    assert _notification_rows(db_path) == []


def test_notify_due_live_delivers_and_records(tmp_path: Path, capsys) -> None:
    db_path, _job_id, reminder_id = _seed_due_reminder_db(tmp_path)

    exit_code = cli.main(_notify_due_args(db_path))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Mode: live" in captured.out
    assert "Due reminders: 1" in captured.out
    assert "Delivered: 1" in captured.out
    assert "Failed:    0" in captured.out
    rows = _notification_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == reminder_id
    assert rows[0][2] == "delivered"
    assert rows[0][4] == "stdout"


def test_notify_due_is_idempotent_for_delivered_occurrence(
    tmp_path: Path, capsys
) -> None:
    db_path, _job_id, _reminder_id = _seed_due_reminder_db(tmp_path)

    assert cli.main(_notify_due_args(db_path)) == 0
    capsys.readouterr()

    exit_code = cli.main(_notify_due_args(db_path))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Due reminders: 0" in captured.out
    assert "Delivered: 0" in captured.out
    assert len(_notification_rows(db_path)) == 1


def test_notify_due_failure_is_recorded_and_retried(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db_path, _job_id, reminder_id = _seed_due_reminder_db(tmp_path)

    real_deliver = deliver

    def _fail(_backend: str, _occurrence) -> DeliveryResult:
        return DeliveryResult(success=False, error="delivery exploded")

    monkeypatch.setattr("careeros.cli.commands.reminders.deliver", _fail)

    exit_code = cli.main(_notify_due_args(db_path))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Delivered: 0" in captured.out
    assert "Failed:    1" in captured.out
    assert "reminder 1 (job" in captured.out
    assert "delivery exploded" in captured.out
    rows = _notification_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == reminder_id
    assert rows[0][2] == "failed"
    assert rows[0][3] == "delivery exploded"

    # A failed delivery leaves the occurrence eligible, so a later run retries.
    monkeypatch.setattr("careeros.cli.commands.reminders.deliver", real_deliver)
    capsys.readouterr()
    assert cli.main(_notify_due_args(db_path)) == 0
    captured = capsys.readouterr()
    assert "Due reminders: 1" in captured.out
    assert "Delivered: 1" in captured.out
    assert len(_notification_rows(db_path)) == 2
    assert _notification_rows(db_path)[1][2] == "delivered"


def test_notify_due_rejects_nonpositive_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    _add_reminder(db_path, job_id)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "reminder",
                "notify-due",
                "--limit",
                "0",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_notify_due_desktop_backend_unavailable(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    import careeros.notifications.backend as notify_backend

    monkeypatch.setattr(notify_backend.shutil, "which", lambda _name: None)

    db_path, _job_id, _reminder_id = _seed_due_reminder_db(tmp_path)

    exit_code = cli.main(
        [
            "reminder",
            "notify-due",
            "--backend",
            "desktop",
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "--backend desktop requires notify-send on PATH" in captured.err
    assert _notification_rows(db_path) == []


def test_notify_due_desktop_explains_delivered_meaning(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    from careeros.notifications.models import DeliveryResult

    db_path, _job_id, _reminder_id = _seed_due_reminder_db(tmp_path)

    def _ok(_backend: str, _occurrence) -> DeliveryResult:
        return DeliveryResult(success=True)

    monkeypatch.setattr("careeros.cli.commands.reminders.deliver", _ok)

    exit_code = cli.main(
        [
            "reminder",
            "notify-due",
            "--backend",
            "desktop",
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Delivered: 1" in captured.out
    assert "notify-send (the desktop notification service)" in captured.out
    assert "cannot prove that a visible banner was rendered" in captured.out


def test_notify_due_conflicts_with_held_notification_lock(
    tmp_path: Path, capsys
) -> None:
    db_path, _job_id, _reminder_id = _seed_due_reminder_db(tmp_path)

    with notification_lock(db_path):
        exit_code = cli.main(_notify_due_args(db_path))
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Notification already running for database:" in captured.err
    assert _notification_rows(db_path) == []


def test_notify_history_empty(tmp_path: Path, capsys) -> None:
    db_path, _job_id, _reminder_id = _seed_due_reminder_db(tmp_path)

    exit_code = cli.main(["reminder", "notify-history", "--database", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No notification attempts found." in captured.out


def test_notify_history_lists_attempts(tmp_path: Path, capsys) -> None:
    db_path, _job_id, reminder_id = _seed_due_reminder_db(tmp_path)
    assert cli.main(_notify_due_args(db_path)) == 0
    capsys.readouterr()

    exit_code = cli.main(["reminder", "notify-history", "--database", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Notification history" in captured.out
    assert f"Attempt 1 | reminder {reminder_id} | Delivered" in captured.out
    assert "Backend:   stdout" in captured.out


def test_notify_history_filters_by_reminder_and_failure(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    db_path, _job_id, first_reminder = _seed_due_reminder_db(tmp_path)
    second_reminder = _add_reminder(db_path, _job_id)

    real_deliver = deliver

    def _fail(_backend: str, _occurrence) -> DeliveryResult:
        return DeliveryResult(success=False, error="boom")

    # Run 1 delivers the first reminder; run 2 fails on the second (one claim
    # per run keeps the two occurrences on separate runs).
    assert cli.main([*_notify_due_args(db_path), "--limit", "1"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("careeros.cli.commands.reminders.deliver", _fail)
    assert cli.main([*_notify_due_args(db_path), "--limit", "1"]) == 1
    capsys.readouterr()
    monkeypatch.setattr("careeros.cli.commands.reminders.deliver", real_deliver)

    exit_code = cli.main(
        ["reminder", "notify-history", "--failed", "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Attempt 2 | reminder {second_reminder} | Failed" in captured.out
    assert "Error:     boom" in captured.out
    assert f"reminder {first_reminder}" not in captured.out

    exit_code = cli.main(
        [
            "reminder",
            "notify-history",
            "--reminder",
            str(first_reminder),
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"reminder {first_reminder} | Delivered" in captured.out
    assert f"reminder {second_reminder}" not in captured.out
