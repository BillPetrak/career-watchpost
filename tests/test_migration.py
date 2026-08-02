"""Tests for non-destructive schema migration and idempotency."""

from pathlib import Path

from careeros.database.connection import connect
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
    }


def test_v1_database_upgrades_with_jobs_intact(tmp_path: Path) -> None:
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

        initialize_schema(connection)

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

    assert version["value"] == str(SCHEMA_VERSION)
    assert len(jobs) == 1
    assert jobs[0]["source_job_id"] == "slug-1"
    assert runs_table is not None


def test_collection_runs_index_exists(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        index = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_collection_runs_source_started'"
        ).fetchone()
    assert index is not None
