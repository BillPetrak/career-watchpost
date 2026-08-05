"""Tests for the schema-v6 reminders tables, migration, and durability."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.reminders.service import add_reminder, complete_reminder

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


def test_fresh_schema_has_reminders_table(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert version["value"] == str(SCHEMA_VERSION)
    assert "reminders" in _table_names(connection)


def test_schema_version_is_eight(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 8


def test_reminder_indexes_exist(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        indexes = _index_names(connection)

    assert "idx_reminders_job" in indexes
    assert "idx_reminders_due" in indexes


def _seed_v5_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        DROP INDEX IF EXISTS idx_reminders_due;
        DROP INDEX IF EXISTS idx_reminders_job;
        DROP TABLE IF EXISTS reminders;
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value)"
        " VALUES ('schema_version', '5')"
    )
    save_jobs(
        connection,
        [
            _make_job(source="arbeitnow", source_job_id="a-1"),
            _make_job(source="remoteok", source_job_id="r-1"),
        ],
    )
    connection.execute(
        """
        INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
        VALUES ('jobicy', '2026-08-02T08:00:00+00:00',
                '2026-08-02T08:00:05+00:00', 1)
        """
    )
    connection.execute("INSERT INTO job_groups (id) VALUES ('group-1')")
    connection.execute(
        """
        INSERT INTO job_group_members (job_id, group_id, reason)
        VALUES (1, 'group-1', 'company+title match')
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
        INSERT INTO application_events (
            application_id, old_status, new_status, note, created_at
        )
        VALUES (1, NULL, 'applied', 'sent CV', '2026-08-03T12:00:00+00:00')
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


def test_v5_database_upgrades_with_every_row_intact(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_v5_database(connection)

        counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "jobs",
                "collection_runs",
                "job_groups",
                "job_group_members",
                "applications",
                "application_events",
                "saved_searches",
            )
        }

        initialize_schema(connection)

        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        counts_after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        }

    assert version["value"] == str(SCHEMA_VERSION)
    assert "reminders" in _table_names(connection)
    assert counts_after == counts_before


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


def test_reminder_requires_existing_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO reminders (job_id, kind, due_at, created_at, updated_at)
                VALUES (999, 'follow_up', '2026-08-10T14:00:00+00:00',
                        '2026-08-03T12:00:00+00:00', '2026-08-03T12:00:00+00:00')
                """
            )


def test_reminder_rejects_unknown_kind_via_check(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(connection, [_make_job(source="arbeitnow", source_job_id="a-1")])
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO reminders (job_id, kind, due_at, created_at, updated_at)
                VALUES (1, 'bogus', '2026-08-10T14:00:00+00:00',
                        '2026-08-03T12:00:00+00:00', '2026-08-03T12:00:00+00:00')
                """
            )


def _reminder_rows(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, job_id, kind, message, due_at, completed_at,"
                " created_at, updated_at FROM reminders ORDER BY id"
            )
        ]


def test_regroup_preserves_reminders_unchanged(tmp_path: Path) -> None:
    """Regression: regrouping never deletes, rewrites, or orphans reminders.

    Same reminder ID, job ID, kind, due date, message, and completion state
    must survive repeated regrouping.
    """
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(source="arbeitnow", source_job_id="a-1"),
                _make_job(source="remoteok", source_job_id="r-1"),
            ],
        )
        ensure_grouped(connection)
        job_ids = {
            row["source_job_id"]: row["id"]
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        add_reminder(
            connection,
            job_id=job_ids["a-1"],
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="call recruiter",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=job_ids["r-1"],
            kind="interview",
            due_at="2026-08-11T09:00:00+00:00",
            now=_EPOCH,
        )
        completed, _changed = complete_reminder(connection, reminder_id=2, now=_EPOCH)
        assert completed.completed_at is not None

    before = _reminder_rows(db_path)
    assert len(before) == 2

    for _ in range(4):
        with connect(db_path) as connection:
            ensure_grouped(connection)
        assert cli.main(["group", "regroup", "--database", str(db_path)]) == 0

    assert _reminder_rows(db_path) == before
