"""Tests for the schema-v5 saved_searches table and migration."""

import sqlite3
from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.schema import SCHEMA_VERSION, initialize_schema


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


def test_schema_version_is_ten_for_m1() -> None:
    assert SCHEMA_VERSION == 10


def test_fresh_schema_has_saved_searches_table(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert version["value"] == str(SCHEMA_VERSION)
    assert "saved_searches" in _table_names(connection)
    assert "idx_saved_searches_name" in _index_names(connection)


def test_saved_searches_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        columns = {
            row["name"]: row["type"]
            for row in connection.execute("PRAGMA table_info(saved_searches)")
        }

    assert set(columns) >= {
        "id",
        "name",
        "description",
        "query",
        "mode",
        "profile_path",
        "grouped",
        "source",
        "remote",
        "location",
        "untracked",
        "include_statuses",
        "exclude_statuses",
        "min_score",
        "result_limit",
        "created_at",
        "updated_at",
    }


def test_saved_searches_name_is_case_insensitive_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO saved_searches
                (name, query, mode, created_at, updated_at)
            VALUES ('Engineer', 'engineer', 'search', 't', 't')
            """
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO saved_searches
                    (name, query, mode, created_at, updated_at)
                VALUES ('engineer', 'engineer', 'search', 't', 't')
                """
            )


def test_saved_searches_mode_and_min_score_checks(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO saved_searches
                    (name, query, mode, created_at, updated_at)
                VALUES ('bad-mode', 'engineer', 'wat', 't', 't')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO saved_searches
                    (name, query, mode, min_score, created_at, updated_at)
                VALUES ('bad-score', 'engineer', 'rank', 150, 't', 't')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO saved_searches
                    (name, query, mode, result_limit, created_at, updated_at)
                VALUES ('bad-limit', 'engineer', 'search', 0, 't', 't')
                """
            )


def test_v4_database_requires_explicit_migration_without_changes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        _create_v4_tables(connection)
        connection.execute(
            "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', '4')"
        )
        connection.execute(
            """
            INSERT INTO jobs (source, source_job_id, title, collected_at)
            VALUES ('arbeitnow', 'slug-1', 'Python Engineer',
                    '2026-08-01T08:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO applications (job_id, current_status, created_at, updated_at)"
            " VALUES (1, 'applied', '2026-08-01T08:00:00+00:00',"
            " '2026-08-01T08:00:00+00:00')"
        )
        connection.commit()
        snapshot_before = tuple(connection.iterdump())

        with pytest.raises(ValueError, match="explicit migration"):
            initialize_schema(connection)
        snapshot_after = tuple(connection.iterdump())

        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        apps = connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0]

    assert snapshot_after == snapshot_before
    assert version["value"] == "4"
    assert apps == 1
    assert "saved_searches" not in _table_names(connection)


def test_repeated_initialization_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert version["value"] == str(SCHEMA_VERSION)


def _create_v4_tables(connection: sqlite3.Connection) -> None:
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

        CREATE TABLE applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
            current_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE application_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL
                REFERENCES applications(id) ON DELETE RESTRICT,
            old_status TEXT,
            new_status TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
