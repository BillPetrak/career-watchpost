"""Tests for the schema-v7 notification tables, migration, and durability."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.reminders.service import add_reminder

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _make_job(*, source: str, source_job_id: str) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title="Python Engineer",
        company="Acme GmbH",
        location="Berlin",
        description="Build Python backends.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


def _index_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    return {row["name"] for row in rows}


def _seed_reminder(connection: sqlite3.Connection) -> int:
    save_jobs(connection, [_make_job(source="arbeitnow", source_job_id="a-1")])
    return add_reminder(
        connection,
        job_id=1,
        kind="follow_up",
        due_at="2026-08-10T14:00:00+00:00",
        message="call recruiter",
        now=_EPOCH,
    ).id


def test_fresh_schema_has_notifications_table(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert version["value"] == str(SCHEMA_VERSION)
    assert "reminder_notifications" in _table_names(connection)


def test_schema_version_is_ten(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 13


def test_notification_indexes_exist(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        indexes = _index_names(connection)

    assert "idx_reminder_notifications_delivered_occurrence" in indexes
    assert "idx_reminder_notifications_reminder" in indexes
    assert "idx_reminder_notifications_attempted" in indexes


def test_repeated_initialization_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        initialize_schema(connection)
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert version["value"] == str(SCHEMA_VERSION)


def _seed_v6_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP TABLE IF EXISTS application_requirement_items;
        DROP TABLE IF EXISTS application_preparations;
        DROP TABLE IF EXISTS review_events;
        DROP TABLE IF EXISTS review_decisions;
        DROP TABLE IF EXISTS reminder_notifications;
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value)"
        " VALUES ('schema_version', '6')"
    )
    save_jobs(
        connection,
        [
            _make_job(source="arbeitnow", source_job_id="a-1"),
            _make_job(source="remoteok", source_job_id="r-1"),
        ],
    )
    add_reminder(
        connection,
        job_id=1,
        kind="follow_up",
        due_at="2026-08-10T14:00:00+00:00",
        message="call recruiter",
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=2,
        kind="interview",
        due_at="2026-08-11T09:00:00+00:00",
        now=_EPOCH,
    )
    connection.execute(
        """
        INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
        VALUES ('jobicy', '2026-08-02T08:00:00+00:00',
                '2026-08-02T08:00:05+00:00', 1)
        """
    )
    connection.execute(
        """
        INSERT INTO applications (job_id, current_status, created_at, updated_at)
        VALUES (1, 'applied', '2026-08-03T12:00:00+00:00',
                '2026-08-03T12:00:00+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO saved_searches (
            name, query, mode, result_limit, created_at, updated_at
        )
        VALUES ('engineer', 'Engineer', 'search', 20,
                '2026-08-03T12:00:00+00:00', '2026-08-03T12:00:00+00:00')
        """
    )
    connection.commit()


def test_v6_database_requires_explicit_migration_without_changes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_v6_database(connection)

        counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "jobs",
                "collection_runs",
                "applications",
                "saved_searches",
                "reminders",
            )
        }
        snapshot_before = tuple(connection.iterdump())

        with pytest.raises(ValueError, match="explicit migration"):
            initialize_schema(connection)
        snapshot_after = tuple(connection.iterdump())

        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        counts_after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }

    assert snapshot_after == snapshot_before
    assert version["value"] == "6"
    assert {
        "reminder_notifications",
        "review_decisions",
        "review_events",
        "application_preparations",
        "application_requirement_items",
    }.isdisjoint(_table_names(connection))
    assert counts_after == counts_before


def test_notification_requires_existing_reminder(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO reminder_notifications (
                    reminder_id, scheduled_due_at, attempted_at, status, backend
                )
                VALUES (999, '2026-08-10T14:00:00+00:00',
                        '2026-08-03T12:00:00+00:00', 'attempted', 'stdout')
                """
            )


def test_notification_rejects_unknown_status(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_reminder(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO reminder_notifications (
                    reminder_id, scheduled_due_at, attempted_at, status, backend
                )
                VALUES (1, '2026-08-10T14:00:00+00:00',
                        '2026-08-03T12:00:00+00:00', 'bogus', 'stdout')
                """
            )


def test_delivered_occurrence_is_unique_per_reminder(tmp_path: Path) -> None:
    """Exactly-once: only one delivered row may exist per (reminder, due_at)."""
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        reminder_id = _seed_reminder(connection)
        connection.execute(
            """
            INSERT INTO reminder_notifications (
                reminder_id, scheduled_due_at, attempted_at, delivered_at,
                status, backend
            )
            VALUES (?, '2026-08-10T14:00:00+00:00', '2026-08-03T12:00:00+00:00',
                    '2026-08-03T12:00:01+00:00', 'delivered', 'stdout')
            """,
            (reminder_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO reminder_notifications (
                    reminder_id, scheduled_due_at, attempted_at, delivered_at,
                    status, backend
                )
                VALUES (?, '2026-08-10T14:00:00+00:00', '2026-08-03T12:05:00+00:00',
                        '2026-08-03T12:05:01+00:00', 'delivered', 'stdout')
                """,
                (reminder_id,),
            )


def test_failed_attempts_can_repeat_per_occurrence(tmp_path: Path) -> None:
    """Retries are allowed: multiple failed rows for one occurrence coexist."""
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        reminder_id = _seed_reminder(connection)
        for minutes in (0, 5, 10):
            connection.execute(
                """
                INSERT INTO reminder_notifications (
                    reminder_id, scheduled_due_at, attempted_at, status,
                    error, backend
                )
                VALUES (?, '2026-08-10T14:00:00+00:00', ?, 'failed',
                        'boom', 'stdout')
                """,
                (
                    reminder_id,
                    f"2026-08-03T12:{minutes:02d}:00+00:00",
                ),
            )
        connection.commit()
        count = connection.execute(
            "SELECT COUNT(*) FROM reminder_notifications"
        ).fetchone()[0]
    assert count == 3


def test_deleting_reminder_is_restricted_by_notifications(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        reminder_id = _seed_reminder(connection)
        connection.execute(
            """
            INSERT INTO reminder_notifications (
                reminder_id, scheduled_due_at, attempted_at, status, backend
            )
            VALUES (?, '2026-08-10T14:00:00+00:00', '2026-08-03T12:00:00+00:00',
                    'attempted', 'stdout')
            """,
            (reminder_id,),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
