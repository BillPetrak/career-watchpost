"""Tests for the reminder section of the Career Snapshot report."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.reminders.service import (
    add_reminder,
    complete_reminder,
)
from careeros.reporting.render import render_report, render_report_json
from careeros.reporting.service import build_report

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str = "Python Engineer",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Acme GmbH",
        location="Berlin",
        description="Build things with Python.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "careeros.sqlite3")
    initialize_schema(connection)
    save_jobs(
        connection,
        [_make_job(source="arbeitnow", source_job_id="a-1")],
    )
    return connection


def _reminders_table(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, job_id, kind, message, due_at, completed_at,"
                " created_at, updated_at FROM reminders ORDER BY id"
            )
        ]


def _seed_reminders(connection: sqlite3.Connection) -> None:
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
        message="call recruiter",
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


def test_empty_reminders_section(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        report = build_report(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()

    assert report.reminders.overdue_count == 0
    assert report.reminders.due_today_utc_count == 0
    assert report.reminders.open_total == 0
    assert report.reminders.upcoming == ()

    text = render_report(report)
    assert "Reminders" in text
    assert "Overdue:         0" in text
    assert "Due today (UTC): 0" in text
    assert "Open total:      0" in text
    assert "none" in text
    assert "CareerOS does not assume your local timezone." in text


def test_reminder_counts_and_upcoming(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        _seed_reminders(connection)
        report = build_report(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()

    assert report.reminders.overdue_count == 2  # 01 Aug review, 03AugT10 follow_up
    assert report.reminders.due_today_utc_count == 2  # 10:00 + 15:00 on 03 Aug (UTC)
    assert report.reminders.open_total == 3
    upcoming_kinds = [s.reminder.kind for s in report.reminders.upcoming]
    assert upcoming_kinds == ["interview"]  # only 15:00 > 12:00 now


def test_upcoming_ordered_by_urgency(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        for kind, due_at in (
            ("review", "2026-08-10T09:00:00+00:00"),
            ("interview", "2026-08-05T09:00:00+00:00"),
            ("follow_up", "2026-08-07T09:00:00+00:00"),
        ):
            add_reminder(connection, job_id=1, kind=kind, due_at=due_at, now=_EPOCH)
        report = build_report(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()

    upcoming = [s.reminder.kind for s in report.reminders.upcoming]
    assert upcoming == ["interview", "follow_up", "review"]


def test_reminder_limit_caps_upcoming(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        for due_at in [
            "2026-08-05T09:00:00+00:00",
            "2026-08-06T09:00:00+00:00",
            "2026-08-07T09:00:00+00:00",
            "2026-08-08T09:00:00+00:00",
        ]:
            add_reminder(
                connection, job_id=1, kind="interview", due_at=due_at, now=_EPOCH
            )
        report = build_report(connection, now=_EPOCH, limit=3)
    finally:
        connection.close()

    assert len(report.reminders.upcoming) == 3
    assert report.reminders.open_total == 4


def test_report_text_and_json_equivalent(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        _seed_reminders(connection)
        report = build_report(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()

    text = render_report(report)
    payload = json.loads(render_report_json(report))

    reminders = payload["reminders"]
    assert reminders["overdue_count"] == 2
    assert reminders["due_today_utc_count"] == 2
    assert reminders["open_total"] == 3
    assert len(reminders["upcoming"]) == 1
    assert reminders["upcoming"][0]["reminder"]["kind"] == "interview"
    assert reminders["upcoming"][0]["job"]["source"] == "arbeitnow"

    for snapshot in reminders["upcoming"]:
        reminder = snapshot["reminder"]
        expected = f"{reminder['kind']}, due {reminder['due_at']}"
        assert expected in text


def test_report_does_not_mutate_reminders(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    connection = _open(tmp_path)
    _seed_reminders(connection)
    connection.close()

    before = _reminders_table(db_path)
    with connect(db_path) as connection:
        build_report(connection, now=_EPOCH, limit=10)

    assert _reminders_table(db_path) == before


def test_report_deterministic_with_fixed_now(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        _seed_reminders(connection)
        report_a = build_report(connection, now=_EPOCH, limit=10)
        report_b = build_report(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()

    assert render_report(report_a) == render_report(report_b)
    assert render_report_json(report_a) == render_report_json(report_b)
    assert report_a.reminders == report_b.reminders


def test_report_works_while_lock_held(tmp_path: Path, capsys) -> None:
    connection = _open(tmp_path)
    connection.close()

    with collection_lock(tmp_path / "careeros.sqlite3"):
        with connect(tmp_path / "careeros.sqlite3") as connection:
            _seed_reminders(connection)
        with connect(tmp_path / "careeros.sqlite3") as connection:
            report = build_report(connection, now=_EPOCH, limit=10)

    assert report.reminders.open_total == 3


def test_report_now_advances_filters(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        _seed_reminders(connection)
        earlier = _EPOCH - timedelta(hours=6)
        report_earlier = build_report(connection, now=earlier, limit=10)
        report_later = build_report(
            connection, now=_EPOCH + timedelta(days=1), limit=10
        )
    finally:
        connection.close()

    assert report_earlier.reminders.upcoming[0].reminder.kind == "follow_up"
    assert report_later.reminders.overdue_count == 3  # everything except completed
    assert report_later.reminders.upcoming == ()
