from __future__ import annotations

import sqlite3

import pytest

from careeros.database.admin import migrate_database
from careeros.database.connection import connect
from careeros.database.migrations import core
from careeros.database.migrations.core import inspect_database, inspect_schema, migrate
from careeros.database.schema import initialize_schema

PREFERENCE_TABLES = (
    "preference_state",
    "explicit_preferences",
    "preference_evidence",
    "learned_preference_suppressions",
)


def _insert_pre_r4_history(connection: sqlite3.Connection) -> tuple[int, int]:
    job_id = int(
        connection.execute(
            """
            INSERT INTO jobs(source, source_job_id, title, collected_at)
            VALUES ('test', 'legacy', 'Legacy Engineer', '2026-01-01T00:00:00+00:00')
            """
        ).lastrowid
    )
    event_id = int(
        connection.execute(
            """
            INSERT INTO review_events(
                job_id, old_decision, new_decision, note, created_at
            )
            VALUES (
                ?, NULL, 'shortlist', 'private legacy note',
                '2026-01-02T00:00:00+00:00'
            )
            """,
            (job_id,),
        ).lastrowid
    )
    connection.execute(
        """
        INSERT INTO review_decisions(job_id, decision, created_at, updated_at)
        VALUES (
            ?, 'shortlist', '2026-01-02T00:00:00+00:00',
            '2026-01-02T00:00:00+00:00'
        )
        """,
        (job_id,),
    )
    return job_id, event_id


def _downgrade_to_v11(connection: sqlite3.Connection) -> None:
    for table in reversed(PREFERENCE_TABLES):
        connection.execute(f"DROP TABLE {table}")
    connection.execute(
        "UPDATE schema_metadata SET value='11' WHERE key='schema_version'"
    )
    connection.commit()


def test_v11_to_v12_preserves_state_and_does_not_seed_history(tmp_path) -> None:
    connection = connect(tmp_path / "v11.sqlite3")
    initialize_schema(connection)
    job_id, event_id = _insert_pre_r4_history(connection)
    _downgrade_to_v11(connection)

    assert inspect_schema(connection) == "upgrade_required"
    assert migrate(connection) == "current"
    assert (
        connection.execute("SELECT title FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        == "Legacy Engineer"
    )
    assert (
        connection.execute(
            "SELECT decision FROM review_decisions WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        == "shortlist"
    )
    assert (
        connection.execute(
            "SELECT note FROM review_events WHERE id=?", (event_id,)
        ).fetchone()[0]
        == "private legacy note"
    )
    state = connection.execute("SELECT * FROM preference_state").fetchone()
    assert state["explicit_configured"] == 0
    assert state["learning_reset_review_event_id"] == event_id
    for table in PREFERENCE_TABLES[1:]:
        assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_v11_to_v12_failure_rolls_back_every_preference_object(
    tmp_path, monkeypatch
) -> None:
    connection = connect(tmp_path / "rollback.sqlite3")
    initialize_schema(connection)
    _downgrade_to_v11(connection)

    real_apply = core._apply_11_to_12

    def fail_after_ddl(candidate: sqlite3.Connection) -> None:
        real_apply(candidate)
        raise RuntimeError("injected v12 failure")

    monkeypatch.setitem(core.MIGRATIONS, 11, fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected v12 failure"):
        migrate(connection)

    assert (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "11"
    )
    for table in PREFERENCE_TABLES:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        )
    assert inspect_schema(connection) == "upgrade_required"


def test_partial_preference_objects_are_not_accepted_as_v11(tmp_path) -> None:
    connection = connect(tmp_path / "partial.sqlite3")
    initialize_schema(connection)
    _downgrade_to_v11(connection)
    connection.execute("CREATE TABLE preference_state(singleton INTEGER PRIMARY KEY)")
    connection.commit()
    assert inspect_schema(connection) == "malformed"
    with pytest.raises(ValueError, match="malformed"):
        migrate(connection)


def test_migration_dispatcher_includes_v12_boundary() -> None:
    assert set(core.MIGRATIONS) == {8, 9, 10, 11}


def test_default_migration_backup_is_pre_v12_and_recoverable(tmp_path) -> None:
    database = tmp_path / "admin.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        _insert_pre_r4_history(connection)
        _downgrade_to_v11(connection)

    assert migrate_database(database) == "current"
    backup = tmp_path / "admin.sqlite3.pre-v12.backup.sqlite3"
    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert inspect_database(backup) == "upgrade_required"
    assert inspect_database(database) == "current"


def test_target_validation_precedes_v12_metadata_update(tmp_path, monkeypatch) -> None:
    connection = connect(tmp_path / "target-validation.sqlite3")
    initialize_schema(connection)
    _downgrade_to_v11(connection)
    observed_metadata: list[str] = []
    real_valid = core._valid_structure

    def reject_target(candidate: sqlite3.Connection, version: int) -> bool:
        if version == 12:
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
    with pytest.raises(ValueError, match="invalid v12 schema"):
        core.migrate_11_to_12(connection)
    assert observed_metadata == ["11"]
    assert (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "11"
    )
    for table in PREFERENCE_TABLES:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        )
