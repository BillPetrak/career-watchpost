"""Tests for the reminders service layer (business rules, validation, rollback)."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import careeros.reminders.service as reminder_service
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.reminders.service import (
    UnknownJobError,
    UnknownReminderError,
    ValidationError,
    add_reminder,
    complete_reminder,
    delete_reminder,
    get_reminder,
    list_reminders,
    reminders_for_job,
    reopen_reminder,
    require_reminder,
    update_reminder,
)

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _make_job(*, source: str, source_job_id: str) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title="Python Engineer",
        company=("Acme GmbH" if source == "arbeitnow" else "Acme Inc"),
        location="Berlin",
        description="Build Python backends.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _job_ids(connection) -> dict[str, int]:
    return {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }


def _seed_jobs(db_path: Path) -> sqlite3.Connection:
    connection = connect(db_path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job(source="arbeitnow", source_job_id="a-1"),
            _make_job(source="remoteok", source_job_id="r-1"),
        ],
    )
    return connection


def test_add_reminder_basic(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        reminder = add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="call recruiter",
            now=_EPOCH,
        )
        assert reminder.id == 1
        assert reminder.job_id == 1
        assert reminder.kind == "follow_up"
        assert reminder.message == "call recruiter"
        assert reminder.due_at == "2026-08-10T14:00:00+00:00"
        assert reminder.completed_at is None
        assert reminder.created_at == _EPOCH.isoformat()
        assert reminder.updated_at == _EPOCH.isoformat()
    finally:
        connection.close()


def test_add_reminder_normalizes_offset_to_utc(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        reminder = add_reminder(
            connection,
            job_id=1,
            kind="review",
            due_at="2026-08-10T09:00:00+02:00",
            now=_EPOCH,
        )
        assert reminder.due_at == "2026-08-10T07:00:00+00:00"
    finally:
        connection.close()


def test_add_reminder_rejects_naive_timestamp(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        with pytest.raises(ValidationError):
            add_reminder(
                connection,
                job_id=1,
                kind="review",
                due_at="2026-08-10T14:00:00",
                now=_EPOCH,
            )
    finally:
        connection.close()


def test_add_reminder_rejects_date_only_timestamp(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        with pytest.raises(ValidationError):
            add_reminder(
                connection,
                job_id=1,
                kind="review",
                due_at="2026-08-10",
                now=_EPOCH,
            )
    finally:
        connection.close()


def test_add_reminder_rejects_malformed_timestamp(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        with pytest.raises(ValidationError):
            add_reminder(
                connection, job_id=1, kind="review", due_at="not-a-date", now=_EPOCH
            )
    finally:
        connection.close()


def test_add_reminder_rejects_whitespace_message(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        with pytest.raises(ValidationError):
            add_reminder(
                connection,
                job_id=1,
                kind="review",
                due_at="2026-08-10T14:00:00+00:00",
                message="   ",
                now=_EPOCH,
            )
    finally:
        connection.close()


def test_add_reminder_unknown_job(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        with pytest.raises(UnknownJobError):
            add_reminder(
                connection,
                job_id=999,
                kind="review",
                due_at="2026-08-10T14:00:00+00:00",
                now=_EPOCH,
            )
    finally:
        connection.close()


def test_add_reminder_invalid_kind(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        with pytest.raises(ValidationError):
            add_reminder(
                connection,
                job_id=1,
                kind="bogus",
                due_at="2026-08-10T14:00:00+00:00",
                now=_EPOCH,
            )
    finally:
        connection.close()


def test_multiple_reminders_per_job(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=1,
            kind="interview",
            due_at="2026-08-11T09:00:00+00:00",
            now=_EPOCH,
        )
        reminders = list_reminders(connection)
        assert len(reminders) == 2
        assert reminders[0].reminder.kind == "follow_up"
        assert reminders[1].reminder.kind == "interview"
    finally:
        connection.close()


def test_list_orders_by_due_then_id(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        for due_at, kind in (
            ("2026-08-10T14:00:00+00:00", "follow_up"),
            ("2026-08-05T09:00:00+00:00", "interview"),
            ("2026-08-10T14:00:00+00:00", "offer"),
            ("2026-08-01T08:00:00+00:00", "review"),
        ):
            add_reminder(connection, job_id=1, kind=kind, due_at=due_at, now=_EPOCH)
        reminders = [r.reminder for r in list_reminders(connection)]
        assert [r.kind for r in reminders] == [
            "review",
            "interview",
            "follow_up",
            "offer",
        ]
        assert [r.id for r in reminders] == [4, 2, 1, 3]
    finally:
        connection.close()


def test_list_state_filters(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="review",
            due_at="2026-08-01T10:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-03T10:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=1,
            kind="interview",
            due_at="2026-08-03T15:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=1,
            kind="offer",
            due_at="2026-08-05T15:00:00+00:00",
            now=_EPOCH,
        )
        complete_reminder(connection, reminder_id=4, now=_EPOCH)

        overdue = [
            r.reminder.id
            for r in list_reminders(connection, state="overdue", now=_EPOCH)
        ]
        due = [
            r.reminder.id for r in list_reminders(connection, state="due", now=_EPOCH)
        ]
        upcoming = [
            r.reminder.id
            for r in list_reminders(connection, state="upcoming", now=_EPOCH)
        ]
        completed = [
            r.reminder.id for r in list_reminders(connection, state="completed")
        ]
        open_reminders = [
            r.reminder.id for r in list_reminders(connection, state="open")
        ]

        assert overdue == [1, 2]
        assert due == [1, 2]
        assert upcoming == [3]
        assert completed == [4]
        assert open_reminders == [1, 2, 3]
    finally:
        connection.close()


def test_list_job_and_kind_filters(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=2,
            kind="interview",
            due_at="2026-08-11T09:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=2,
            kind="follow_up",
            due_at="2026-08-12T09:00:00+00:00",
            now=_EPOCH,
        )
        by_job = [r.reminder.id for r in list_reminders(connection, job_id=2)]
        by_kind = [r.reminder.id for r in list_reminders(connection, kind="follow_up")]
        both = [
            r.reminder.id
            for r in list_reminders(connection, job_id=2, kind="interview")
        ]

        assert by_job == [2, 3]
        assert by_kind == [1, 3]
        assert both == [2]
    finally:
        connection.close()


def test_list_after_and_before(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-05T09:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=1,
            kind="interview",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=1,
            kind="review",
            due_at="2026-08-15T09:00:00+00:00",
            now=_EPOCH,
        )
        after = [
            r.reminder.id
            for r in list_reminders(connection, after="2026-08-06T00:00:00+00:00")
        ]
        before = [
            r.reminder.id
            for r in list_reminders(connection, before="2026-08-12T00:00:00+00:00")
        ]
        window = [
            r.reminder.id
            for r in list_reminders(
                connection,
                after="2026-08-06T00:00:00+00:00",
                before="2026-08-12T00:00:00+00:00",
            )
        ]

        assert after == [2, 3]
        assert before == [1, 2]
        assert window == [2]
    finally:
        connection.close()


def test_list_invalid_filter_values(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        with pytest.raises(ValidationError):
            list_reminders(connection, state="bogus")
        with pytest.raises(ValidationError):
            list_reminders(connection, kind="bogus")
        with pytest.raises(ValidationError):
            list_reminders(connection, after="not-a-date")
        with pytest.raises(ValidationError):
            list_reminders(connection, limit=0)
    finally:
        connection.close()


def test_get_and_require_reminder(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        assert get_reminder(connection, reminder_id=1) is not None
        assert get_reminder(connection, reminder_id=99) is None
        assert require_reminder(connection, reminder_id=1).id == 1
        with pytest.raises(UnknownReminderError):
            require_reminder(connection, reminder_id=99)
    finally:
        connection.close()


def test_reminders_for_job(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=2,
            kind="interview",
            due_at="2026-08-11T09:00:00+00:00",
            now=_EPOCH,
        )
        job_1 = [r.reminder.id for r in reminders_for_job(connection, job_id=1)]
        assert job_1 == [1]
    finally:
        connection.close()


def test_update_fields(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="old note",
            now=_EPOCH,
        )
        updated, changed = update_reminder(
            connection,
            reminder_id=1,
            kind="interview",
            due_at="2026-08-11T09:00:00+00:00",
            message="new note",
            now=_EPOCH,
        )
        assert changed is True
        assert updated.kind == "interview"
        assert updated.due_at == "2026-08-11T09:00:00+00:00"
        assert updated.message == "new note"
        assert updated.updated_at == _EPOCH.isoformat()
    finally:
        connection.close()


def test_update_clear_message(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="old note",
            now=_EPOCH,
        )
        updated, changed = update_reminder(
            connection, reminder_id=1, clear_message=True, now=_EPOCH
        )
        assert changed is True
        assert updated.message is None
    finally:
        connection.close()


def test_update_noop_keeps_updated_at(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="note",
            now=_EPOCH,
        )
        later = datetime.fromisoformat("2026-08-04T12:00:00+00:00")
        updated, changed = update_reminder(
            connection,
            reminder_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="note",
            now=later,
        )
        assert changed is False
        assert updated.updated_at == _EPOCH.isoformat()
    finally:
        connection.close()


def test_update_invalid_values(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        with pytest.raises(ValidationError):
            update_reminder(connection, reminder_id=1, kind="bogus", now=_EPOCH)
        with pytest.raises(ValidationError):
            update_reminder(connection, reminder_id=1, due_at="2026-08-10", now=_EPOCH)
        with pytest.raises(ValidationError):
            update_reminder(
                connection,
                reminder_id=1,
                message="x",
                clear_message=True,
                now=_EPOCH,
            )
        with pytest.raises(UnknownReminderError):
            update_reminder(connection, reminder_id=99, kind="interview", now=_EPOCH)
    finally:
        connection.close()


def test_complete_and_reopen_are_idempotent(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="note",
            now=_EPOCH,
        )
        later = datetime.fromisoformat("2026-08-05T12:00:00+00:00")

        completed, changed = complete_reminder(connection, reminder_id=1, now=later)
        assert changed is True
        assert completed.completed_at == later.isoformat()
        assert completed.due_at == "2026-08-10T14:00:00+00:00"
        assert completed.kind == "follow_up"
        assert completed.message == "note"

        completed_again, changed_again = complete_reminder(
            connection, reminder_id=1, now=later
        )
        assert changed_again is False
        assert completed_again.completed_at == later.isoformat()

        reopened, reopened_changed = reopen_reminder(
            connection, reminder_id=1, now=later
        )
        assert reopened_changed is True
        assert reopened.completed_at is None

        reopened_again, reopened_again_changed = reopen_reminder(
            connection, reminder_id=1, now=later
        )
        assert reopened_again_changed is False
        assert reopened_again.completed_at is None
    finally:
        connection.close()


def test_complete_reopen_unknown_reminder(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        with pytest.raises(UnknownReminderError):
            complete_reminder(connection, reminder_id=99, now=_EPOCH)
        with pytest.raises(UnknownReminderError):
            reopen_reminder(connection, reminder_id=99, now=_EPOCH)
        with pytest.raises(UnknownReminderError):
            delete_reminder(connection, reminder_id=99)
    finally:
        connection.close()


def test_delete_reminder(tmp_path: Path) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        add_reminder(
            connection,
            job_id=1,
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        deleted = delete_reminder(connection, reminder_id=1)
        assert deleted.id == 1
        assert list_reminders(connection) == []
        assert get_reminder(connection, reminder_id=1) is None
    finally:
        connection.close()


def test_add_rolls_back_when_persistence_fails(tmp_path: Path, monkeypatch) -> None:
    connection = _seed_jobs(tmp_path / "careeros.sqlite3")
    try:
        real_insert = reminder_service._insert_reminder

        def _boom(*args, **kwargs):
            real_insert(*args, **kwargs)
            raise sqlite3.OperationalError("simulated failure")

        monkeypatch.setattr(reminder_service, "_insert_reminder", _boom)
        with pytest.raises(sqlite3.Error):
            add_reminder(
                connection,
                job_id=1,
                kind="follow_up",
                due_at="2026-08-10T14:00:00+00:00",
                now=_EPOCH,
            )
        rows = connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        assert rows == 0
    finally:
        monkeypatch.undo()
        connection.close()


def test_reminders_for_two_group_members_remain_distinct(tmp_path: Path) -> None:
    """Two members of one canonical group each keep their own reminder."""
    db_path = tmp_path / "careeros.sqlite3"
    connection = connect(db_path)
    try:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(source="arbeitnow", source_job_id="a-1"),
                _make_job(source="remoteok", source_job_id="r-1"),
            ],
        )
        ensure_grouped(connection)
        ids = _job_ids(connection)
        assert ids["a-1"] != ids["r-1"]

        add_reminder(
            connection,
            job_id=ids["a-1"],
            kind="interview",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=ids["r-1"],
            kind="follow_up",
            due_at="2026-08-11T09:00:00+00:00",
            now=_EPOCH,
        )
        reminders = list_reminders(connection)
        assert len(reminders) == 2
        assert {r.reminder.job_id for r in reminders} == {ids["a-1"], ids["r-1"]}
    finally:
        connection.close()
