from __future__ import annotations

import sqlite3

import pytest

from careeros.database.connection import connect
from careeros.database.migrations.core import V13_TABLES, inspect_schema, migrate
from careeros.database.schema import SCHEMA_VERSION, initialize_schema

PREFERENCE_TABLES = {
    "preference_state",
    "explicit_preferences",
    "preference_evidence",
    "learned_preference_suppressions",
}


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]


def _downgrade_to_v11(connection: sqlite3.Connection) -> None:
    for table in (
        "review_ranking_current",
        "review_ranking_items",
        "review_ranking_generations",
        "review_ranking_authority",
    ):
        connection.execute(f"DROP TABLE {table}")
    for table in reversed(V13_TABLES):
        connection.execute(f"DROP TABLE {table}")
    for table in (
        "learned_preference_suppressions",
        "preference_evidence",
        "explicit_preferences",
        "preference_state",
    ):
        connection.execute(f"DROP TABLE {table}")
    connection.execute(
        "UPDATE schema_metadata SET value='11' WHERE key='schema_version'"
    )
    connection.commit()


def test_fresh_v12_schema_is_exact_and_current(tmp_path) -> None:
    connection = connect(tmp_path / "fresh.sqlite3")
    initialize_schema(connection)

    assert SCHEMA_VERSION == 14
    assert inspect_schema(connection) == "current"
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert PREFERENCE_TABLES <= tables
    assert _columns(connection, "preference_state") == [
        "singleton",
        "explicit_configured",
        "explicit_updated_at",
        "explicit_ranking_revision",
        "explicit_ranking_fingerprint",
        "learning_reset_at",
        "learning_reset_review_event_id",
        "created_at",
    ]
    assert _columns(connection, "explicit_preferences") == [
        "dimension",
        "position",
        "value",
    ]
    assert _columns(connection, "preference_evidence") == [
        "job_id",
        "review_event_id",
        "dimension",
        "value",
        "normalized_value",
        "signal",
        "observed_at",
    ]
    assert _columns(connection, "learned_preference_suppressions") == [
        "preference_key",
        "dimension",
        "normalized_value",
        "suppressed_at",
    ]
    state = tuple(connection.execute("SELECT * FROM preference_state").fetchone())
    assert state[:4] == (
        1,
        0,
        None,
        0,
    )
    assert isinstance(state[4], str) and len(state[4]) == 64
    assert state[5:7] == (None, 0)
    assert isinstance(state[7], str) and state[7]
    assert (
        connection.execute("SELECT COUNT(*) FROM explicit_preferences").fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM preference_evidence").fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM learned_preference_suppressions"
        ).fetchone()[0]
        == 0
    )

    indexes = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_autoindex_%'"
        )
        if str(row[1]) in PREFERENCE_TABLES
    }
    assert indexes == {
        "idx_preference_evidence_dimension_value": "preference_evidence",
        "idx_preference_evidence_review_event": "preference_evidence",
    }
    assert [
        str(row[2])
        for row in connection.execute(
            "PRAGMA index_info(idx_preference_evidence_dimension_value)"
        )
    ] == ["dimension", "normalized_value"]
    assert [
        str(row[2])
        for row in connection.execute(
            "PRAGMA index_info(idx_preference_evidence_review_event)"
        )
    ] == ["review_event_id"]

    foreign_keys = list(
        connection.execute("PRAGMA foreign_key_list(preference_evidence)")
    )
    assert {
        (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
        for row in foreign_keys
    } == {
        ("jobs", "job_id", "id", "RESTRICT"),
        ("review_events", "review_event_id", "id", "RESTRICT"),
    }


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        ("UPDATE preference_state SET singleton=2", ()),
        ("UPDATE preference_state SET explicit_configured=2", ()),
        (
            "INSERT INTO explicit_preferences VALUES(?,?,?)",
            ("unknown", 0, "value"),
        ),
        (
            "INSERT INTO explicit_preferences VALUES(?,?,?)",
            ("skills", 100, "value"),
        ),
        (
            "INSERT INTO learned_preference_suppressions VALUES(?,?,?,?)",
            ("A" * 32, "title", "engineer", "now"),
        ),
    ],
)
def test_fresh_v12_constraints_reject_invalid_rows(
    tmp_path, statement: str, parameters: tuple[object, ...]
) -> None:
    connection = connect(tmp_path / "constraints.sqlite3")
    initialize_schema(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement, parameters)


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TABLE preference_evidence",
        "ALTER TABLE explicit_preferences ADD COLUMN unexpected TEXT",
        "DROP INDEX idx_preference_evidence_review_event",
        "CREATE INDEX unexpected_preference_index ON preference_state(created_at)",
    ],
)
def test_malformed_v12_preference_schema_fails_closed(tmp_path, mutation: str) -> None:
    connection = connect(tmp_path / "malformed.sqlite3")
    initialize_schema(connection)
    connection.execute(mutation)
    connection.commit()
    assert inspect_schema(connection) == "malformed"


def test_valid_v11_is_upgrade_required_and_current_v12_is_idempotent(tmp_path) -> None:
    connection = connect(tmp_path / "states.sqlite3")
    initialize_schema(connection)
    _downgrade_to_v11(connection)
    assert inspect_schema(connection) == "upgrade_required"
    assert migrate(connection) == "current"
    before = connection.total_changes
    assert migrate(connection) == "current"
    assert connection.total_changes == before
