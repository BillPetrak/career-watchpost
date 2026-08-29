"""Tests for non-destructive schema migration and idempotency."""

from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.migrations.core import inspect_schema
from careeros.database.schema import SCHEMA_VERSION, initialize_schema


def test_initialize_schema_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    assert version["value"] == str(SCHEMA_VERSION)
    assert {row["name"] for row in tables} >= {
        "schema_metadata",
        "jobs",
        "collection_runs",
        "job_groups",
        "job_group_members",
        "applications",
        "application_events",
        "application_preparations",
        "application_requirement_items",
        "saved_searches",
        "reminders",
        "reminder_notifications",
        "review_decisions",
        "review_events",
    }


def test_v1_database_requires_explicit_migration_without_changes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_job_id TEXT,
                title TEXT NOT NULL,
                company TEXT,
                location TEXT,
                description TEXT,
                url TEXT,
                employment_type TEXT,
                remote_status TEXT,
                published_at TEXT,
                collected_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, source_job_id)
            );
            INSERT INTO schema_metadata(key, value) VALUES ('schema_version', '1');
            INSERT INTO jobs (source, source_job_id, title, collected_at)
            VALUES ('arbeitnow', 'slug-1', 'Python Engineer',
                    '2026-08-01T08:00:00+00:00');
            """
        )
        connection.commit()
        snapshot_before = tuple(connection.iterdump())

        with pytest.raises(ValueError, match="explicit migration"):
            initialize_schema(connection)
        snapshot_after = tuple(connection.iterdump())

        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        jobs = connection.execute(
            "SELECT source, source_job_id, title FROM jobs"
        ).fetchall()
        runs_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'collection_runs'"
        ).fetchone()

    assert snapshot_after == snapshot_before
    assert version["value"] == "1"
    assert len(jobs) == 1
    assert jobs[0]["source_job_id"] == "slug-1"
    assert runs_table is None


def test_v7_database_requires_explicit_migration_without_changes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_job_id TEXT,
                title TEXT NOT NULL,
                company TEXT,
                location TEXT,
                description TEXT,
                url TEXT,
                employment_type TEXT,
                remote_status TEXT,
                published_at TEXT,
                collected_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, source_job_id)
            );
            INSERT INTO schema_metadata(key, value) VALUES ('schema_version', '7');
            INSERT INTO jobs (source, source_job_id, title, collected_at)
            VALUES ('arbeitnow', 'slug-1', 'Python Engineer',
                    '2026-08-01T08:00:00+00:00');
            """
        )
        connection.commit()
        snapshot_before = tuple(connection.iterdump())

        with pytest.raises(ValueError, match="explicit migration"):
            initialize_schema(connection)
        snapshot_after = tuple(connection.iterdump())

        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        review_decisions = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'review_decisions'"
        ).fetchone()
        review_events = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name = 'review_events'"
        ).fetchone()
        job = connection.execute(
            "SELECT source_job_id, title FROM jobs WHERE source_job_id = 'slug-1'"
        ).fetchone()

    assert snapshot_after == snapshot_before
    assert version["value"] == "7"
    assert review_decisions is None
    assert review_events is None
    assert job["title"] == "Python Engineer"


def test_collection_runs_index_exists(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        index = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_collection_runs_source_started'"
        ).fetchone()
    assert index is not None


@pytest.mark.parametrize(
    ("table", "needle", "replacement"),
    [
        (
            "application_preparations",
            "ON DELETE CASCADE",
            "ON DELETE RESTRICT",
        ),
        (
            "application_requirement_items",
            "required INTEGER NOT NULL",
            "required INTEGER",
        ),
        (
            "application_requirement_items",
            "UNIQUE(application_id, sort_order)",
            "UNIQUE(id, sort_order)",
        ),
        (
            "application_requirement_items",
            "CHECK (kind != 'deadline' OR fulfillment_state != 'satisfied')",
            "CHECK (kind != 'deadline' OR fulfillment_state = 'satisfied')",
        ),
        (
            "application_events",
            "note_redacted_at TEXT,",
            "",
        ),
    ],
)
def test_v9_inspection_rejects_weakened_contract(
    tmp_path: Path, table: str, needle: str, replacement: str
) -> None:
    """Structural inspection must fail closed for weakened v9 definitions."""
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        assert inspect_schema(connection) == "current"
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        assert needle in sql
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
            (sql.replace(needle, replacement), table),
        )
        connection.execute("PRAGMA writable_schema=OFF")
        connection.commit()

    # Reopening forces SQLite to reparse the mutated sqlite_master definitions.
    with connect(database_path) as connection:
        assert inspect_schema(connection) == "malformed"
