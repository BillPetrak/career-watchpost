"""Tests for the schema-v8 review tables, migration, and durability."""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.migrations.core import migrate
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.review.service import set_decision

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


def test_schema_version_is_ten(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 10


def test_fresh_schema_has_review_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()

    assert version["value"] == str(SCHEMA_VERSION)
    assert "review_decisions" in _table_names(connection)
    assert "review_events" in _table_names(connection)


def test_review_indexes_exist(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        indexes = _index_names(connection)

    assert "idx_review_decisions_decision" in indexes
    assert "idx_review_events_job" in indexes


def _seed_v7_database(connection: sqlite3.Connection) -> None:
    """Populate every pre-v8 table with representative rows."""
    connection.executescript(
        """
        DROP TABLE IF EXISTS application_requirement_items;
        DROP TABLE IF EXISTS application_preparations;
        DROP INDEX IF EXISTS idx_review_decisions_decision;
        DROP INDEX IF EXISTS idx_review_events_job;
        DROP TABLE IF EXISTS review_events;
        DROP TABLE IF EXISTS review_decisions;
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value)"
        " VALUES ('schema_version', '7')"
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
    connection.execute(
        """
        INSERT INTO reminders (job_id, kind, due_at, created_at, updated_at)
        VALUES (2, 'follow_up', '2026-08-10T14:00:00+00:00',
                '2026-08-03T12:00:00+00:00', '2026-08-03T12:00:00+00:00')
        """
    )
    connection.commit()


def test_v7_database_requires_explicit_migration_without_changes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        _seed_v7_database(connection)

        tables = (
            "jobs",
            "collection_runs",
            "job_groups",
            "job_group_members",
            "applications",
            "application_events",
            "saved_searches",
            "reminders",
        )
        counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
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
            for table in tables
        }

    assert snapshot_after == snapshot_before
    assert version["value"] == "7"
    assert {
        "review_decisions",
        "review_events",
        "application_preparations",
        "application_requirement_items",
    }.isdisjoint(_table_names(connection))
    assert counts_after == counts_before


def test_explicit_v8_to_v10_migration_preserves_review_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(connection, [_make_job(source="arbeitnow", source_job_id="a-1")])
        set_decision(
            connection,
            job_id=1,
            decision="shortlist",
            now=_EPOCH,
        )
        connection.executescript(
            """
            DROP TABLE material_versions;
            DROP TABLE materials;
            DROP TABLE material_store;
            DROP TABLE application_requirement_items;
            DROP TABLE application_preparations;
            ALTER TABLE application_events DROP COLUMN note_redacted_at;
            UPDATE schema_metadata SET value = '8' WHERE key = 'schema_version';
            """
        )
        connection.commit()
        counts_before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("review_decisions", "review_events")
        }

        with pytest.raises(ValueError, match="explicit migration"):
            initialize_schema(connection)
        assert migrate(connection) == "current"
        initialize_schema(connection)
        counts_after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("review_decisions", "review_events")
        }
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        event_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(application_events)")
        }

    assert version["value"] == str(SCHEMA_VERSION)
    assert "note_redacted_at" in event_columns
    assert {
        "application_preparations",
        "application_requirement_items",
    } <= _table_names(connection)
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


def test_decision_requires_existing_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO review_decisions (
                    job_id, decision, created_at, updated_at
                )
                VALUES (999, 'shortlist', '2026-08-03T12:00:00+00:00',
                        '2026-08-03T12:00:00+00:00')
                """
            )


def test_event_requires_existing_job(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO review_events (job_id, created_at)
                VALUES (999, '2026-08-03T12:00:00+00:00')
                """
            )


def test_decision_rejects_unknown_value_via_check(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(connection, [_make_job(source="arbeitnow", source_job_id="a-1")])
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO review_decisions (
                    job_id, decision, created_at, updated_at
                )
                VALUES (1, 'bogus', '2026-08-03T12:00:00+00:00',
                        '2026-08-03T12:00:00+00:00')
                """
            )


def test_decision_job_id_is_unique(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(connection, [_make_job(source="arbeitnow", source_job_id="a-1")])
        connection.execute(
            """
            INSERT INTO review_decisions (
                job_id, decision, created_at, updated_at
            )
            VALUES (1, 'shortlist', '2026-08-03T12:00:00+00:00',
                    '2026-08-03T12:00:00+00:00')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO review_decisions (
                    job_id, decision, created_at, updated_at
                )
                VALUES (1, 'dismiss', '2026-08-03T12:00:00+00:00',
                        '2026-08-03T12:00:00+00:00')
                """
            )


def test_deleting_job_is_restricted_by_decision(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(connection, [_make_job(source="arbeitnow", source_job_id="a-1")])
        set_decision(connection, job_id=1, decision="shortlist", now=_EPOCH)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM jobs WHERE id = 1")


def _decision_rows(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, job_id, decision, created_at, updated_at "
                "FROM review_decisions ORDER BY id"
            )
        ]


def _event_rows(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, job_id, old_decision, new_decision, note, created_at "
                "FROM review_events ORDER BY id"
            )
        ]


def test_regroup_preserves_review_rows_unchanged(tmp_path: Path) -> None:
    """Regression: regrouping never deletes, rewrites, or orphans reviews.

    Same decision ID, job ID, decision value, timestamps, and event rows must
    survive repeated regrouping.
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
        set_decision(
            connection,
            job_id=1,
            decision="shortlist",
            note="top pick",
            now=_EPOCH,
        )
        set_decision(connection, job_id=2, decision="dismiss", now=_EPOCH)

    before_decisions = _decision_rows(db_path)
    before_events = _event_rows(db_path)
    assert len(before_decisions) == 2
    assert len(before_events) == 2

    for _ in range(4):
        with connect(db_path) as connection:
            ensure_grouped(connection)
        assert cli.main(["group", "regroup", "--database", str(db_path)]) == 0

    assert _decision_rows(db_path) == before_decisions
    assert _event_rows(db_path) == before_events
