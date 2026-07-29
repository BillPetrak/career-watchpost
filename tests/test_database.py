"""Tests for the CareerOS SQLite database layer."""

from pathlib import Path

from careeros.database.connection import connect
from careeros.database.schema import SCHEMA_VERSION, initialize_schema


def test_database_connection_creates_parent_directory(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "careeros.sqlite3"

    connection = connect(database_path)
    connection.close()

    assert database_path.exists()


def test_initialize_schema_creates_jobs_table(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        initialize_schema(connection)

        jobs_table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'jobs'
            """
        ).fetchone()

        schema_version = connection.execute(
            """
            SELECT value
            FROM schema_metadata
            WHERE key = 'schema_version'
            """
        ).fetchone()

    assert jobs_table is not None
    assert schema_version is not None
    assert schema_version["value"] == str(SCHEMA_VERSION)
