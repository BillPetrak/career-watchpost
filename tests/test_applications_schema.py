"""Tests for the schema-v4 application-tracking tables and migration."""

import sqlite3
from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.schema import SCHEMA_VERSION, initialize_schema

_V3_TABLES = """
CREATE TABLE schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE job_groups (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE job_group_members (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    group_id TEXT NOT NULL REFERENCES job_groups(id),
    reason TEXT NOT NULL,
    remote_sig TEXT,
    location_sig TEXT
);

CREATE TABLE collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    succeeded INTEGER,
    fetched INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    error TEXT
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
"""


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


def test_fresh_schema_has_application_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert version["value"] == str(SCHEMA_VERSION)
    assert _table_names(connection) >= {
        "applications",
        "application_events",
    }


def test_schema_version_is_five(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 5


def test_required_indexes_exist(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        indexes = _index_names(connection)

    assert "idx_applications_status" in indexes
    assert "idx_application_events_application" in indexes


def test_v3_database_upgrades_with_rows_intact(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        connection.executescript(_V3_TABLES)
        connection.execute(
            "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', '3')"
        )
        connection.execute(
            """
            INSERT INTO jobs (source, source_job_id, title, collected_at)
            VALUES ('arbeitnow', 'slug-1', 'Python Engineer',
                    '2026-08-01T08:00:00+00:00')
            """
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
        connection.commit()

        initialize_schema(connection)

        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        jobs = connection.execute(
            "SELECT id, source, source_job_id, title FROM jobs"
        ).fetchall()
        runs = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
        members = connection.execute(
            "SELECT COUNT(*) FROM job_group_members"
        ).fetchone()[0]

    assert version["value"] == str(SCHEMA_VERSION)
    assert _table_names(connection) >= {
        "applications",
        "application_events",
    }
    assert len(jobs) == 1 and jobs[0]["source_job_id"] == "slug-1"
    assert runs == 1
    assert members == 1


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


def test_one_application_record_per_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO jobs (source, source_job_id, title, collected_at)
            VALUES ('arbeitnow', 'a-1', 'Python Engineer',
                    '2026-08-01T08:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO applications (job_id, current_status, created_at, updated_at)
            VALUES (1, 'applied', '2026-08-03T12:00:00+00:00',
                    '2026-08-03T12:00:00+00:00')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO applications (
                    job_id, current_status, created_at, updated_at
                )
                VALUES (1, 'interview', '2026-08-03T13:00:00+00:00',
                        '2026-08-03T13:00:00+00:00')
                """
            )


def test_application_requires_existing_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO applications (
                    job_id, current_status, created_at, updated_at
                )
                VALUES (999, 'applied', '2026-08-03T12:00:00+00:00',
                        '2026-08-03T12:00:00+00:00')
                """
            )


def test_event_requires_existing_application(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO application_events (
                    application_id, old_status, new_status, created_at
                )
                VALUES (999, NULL, 'applied', '2026-08-03T12:00:00+00:00')
                """
            )
