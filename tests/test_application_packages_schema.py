from __future__ import annotations

# Normative SQL fixture statements remain visually comparable to the DDL.
# ruff: noqa: E501
import sqlite3

import pytest

from careeros.database.connection import connect
from careeros.database.migrations import core
from careeros.database.migrations.core import inspect_schema, migrate
from careeros.database.schema import initialize_schema

M3_TABLES = (
    "package_completeness_reasons",
    "package_answers",
    "package_material_selections",
    "application_package_requirement_snapshots",
    "application_package_versions",
    "application_packages",
)


def downgrade_to_v10(connection: sqlite3.Connection) -> None:
    for table in M3_TABLES:
        connection.execute(f"DROP TABLE {table}")
    connection.execute(
        "UPDATE schema_metadata SET value='10' WHERE key='schema_version'"
    )
    connection.commit()


def test_fresh_v11_is_exact_and_rejects_extra_named_index(tmp_path):
    connection = connect(tmp_path / "fresh.sqlite3")
    initialize_schema(connection)
    assert inspect_schema(connection) == "current"
    connection.execute(
        "CREATE INDEX unexpected_package_index ON application_packages(updated_at)"
    )
    connection.commit()
    assert inspect_schema(connection) == "malformed"


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE package_unexpected(id INTEGER)",
        "CREATE INDEX package_unexpected_index ON jobs(id)",
        "CREATE TRIGGER package_unexpected_trigger AFTER INSERT ON jobs BEGIN SELECT 1; END",
    ],
)
def test_fresh_v11_rejects_additional_m3_domain_objects(tmp_path, statement):
    connection = connect(tmp_path / "unexpected.sqlite3")
    initialize_schema(connection)
    connection.execute(statement)
    connection.commit()
    assert inspect_schema(connection) == "malformed"


def test_fresh_v11_allows_unrelated_preexisting_careeros_objects(tmp_path):
    connection = connect(tmp_path / "unrelated.sqlite3")
    initialize_schema(connection)
    connection.execute("CREATE TABLE career_notes(id INTEGER PRIMARY KEY)")
    connection.execute("CREATE INDEX idx_career_notes_id ON career_notes(id)")
    connection.execute(
        "CREATE TRIGGER trg_career_notes AFTER INSERT ON career_notes BEGIN SELECT 1; END"
    )
    connection.commit()
    assert inspect_schema(connection) == "current"


def test_v10_to_v11_is_additive_and_exact(tmp_path):
    connection = connect(tmp_path / "v10.sqlite3")
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO materials(id,name,category,revision,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        ("a" * 32, "Fictional Resume", "resume", 1, "active", "t0", "t0"),
    )
    downgrade_to_v10(connection)
    assert inspect_schema(connection) == "upgrade_required"
    assert migrate(connection) == "current"
    assert inspect_schema(connection) == "current"
    assert connection.execute("SELECT name FROM materials").fetchone()[0] == (
        "Fictional Resume"
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM application_packages").fetchone()[0]
        == 0
    )


def test_v10_with_preexisting_package_object_rolls_back(tmp_path):
    connection = connect(tmp_path / "collision.sqlite3")
    initialize_schema(connection)
    downgrade_to_v10(connection)
    connection.execute("CREATE TABLE application_packages(sentinel TEXT)")
    connection.commit()
    with pytest.raises(sqlite3.OperationalError):
        migrate(connection)
    assert (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "10"
    )
    assert (
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='application_packages'"
        ).fetchone()[0]
        == "CREATE TABLE application_packages(sentinel TEXT)"
    )


def test_v8_to_v11_failure_rolls_back_every_boundary(tmp_path, monkeypatch):
    connection = connect(tmp_path / "v8.sqlite3")
    initialize_schema(connection)
    for table in M3_TABLES:
        connection.execute(f"DROP TABLE {table}")
    for table in ("material_versions", "materials", "material_store"):
        connection.execute(f"DROP TABLE {table}")
    connection.execute("DROP TABLE application_requirement_items")
    connection.execute("DROP TABLE application_preparations")
    connection.execute("ALTER TABLE application_events DROP COLUMN note_redacted_at")
    connection.execute(
        "UPDATE schema_metadata SET value='8' WHERE key='schema_version'"
    )
    connection.commit()

    def injected_failure(_connection):
        raise RuntimeError("injected v11 failure")

    monkeypatch.setitem(core.MIGRATIONS, 10, injected_failure)
    with pytest.raises(RuntimeError, match="injected v11 failure"):
        migrate(connection)
    assert (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "8"
    )
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='application_preparations'"
        ).fetchone()
        is None
    )
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='materials'"
        ).fetchone()
        is None
    )
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='application_packages'"
        ).fetchone()
        is None
    )


def test_dispatcher_has_every_frozen_boundary():
    assert set(core.MIGRATIONS) == {8, 9, 10}


def test_target_validation_precedes_metadata_and_failure_rolls_back(
    tmp_path, monkeypatch
):
    connection = connect(tmp_path / "validation-order.sqlite3")
    initialize_schema(connection)
    downgrade_to_v10(connection)
    observed_metadata: list[str] = []
    real_valid = core._valid_structure

    def reject_target(candidate, version):
        if version == 11:
            observed_metadata.append(
                str(
                    candidate.execute(
                        "SELECT value FROM schema_metadata WHERE key='schema_version'"
                    ).fetchone()[0]
                )
            )
            return False
        return real_valid(candidate, version)

    monkeypatch.setattr(core, "_valid_structure", reject_target)
    with pytest.raises(ValueError, match="invalid v11 schema"):
        core.migrate_10_to_11(connection)
    assert observed_metadata == ["10"]
    assert (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "10"
    )
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_packages'"
        ).fetchone()
        is None
    )
