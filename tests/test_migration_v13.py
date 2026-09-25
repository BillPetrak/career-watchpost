from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careeros.database.admin import migrate_database
from careeros.database.connection import connect
from careeros.database.migrations import core
from careeros.database.migrations.core import (
    V13_TABLES,
    inspect_database,
    inspect_schema,
    migrate,
    migrate_12_to_13,
)
from careeros.database.schema import SCHEMA_VERSION, initialize_schema


def _downgrade_to_v12(connection: sqlite3.Connection) -> None:
    for table in reversed(V13_TABLES):
        connection.execute(f"DROP TABLE {table}")
    connection.execute(
        "UPDATE schema_metadata SET value='12' WHERE key='schema_version'"
    )
    connection.commit()


def _seed_v12_rows(connection: sqlite3.Connection) -> None:
    job_id = int(
        connection.execute(
            """
            INSERT INTO jobs(source, source_job_id, title, company, collected_at)
            VALUES ('test', 'legacy-1', 'Legacy Engineer', 'Legacy Co',
                    '2026-01-01T00:00:00+00:00')
            """
        ).lastrowid
    )
    event_id = int(
        connection.execute(
            """
            INSERT INTO review_events(
                job_id, old_decision, new_decision, note, created_at
            ) VALUES (?, NULL, 'shortlist', 'legacy private note',
                      '2026-01-02T00:00:00+00:00')
            """,
            (job_id,),
        ).lastrowid
    )
    connection.execute(
        """
        INSERT INTO review_decisions(job_id, decision, created_at, updated_at)
        VALUES (?, 'shortlist', '2026-01-02T00:00:00+00:00',
                '2026-01-02T00:00:00+00:00')
        """,
        (job_id,),
    )
    connection.execute(
        """
        INSERT INTO explicit_preferences(dimension, position, value)
        VALUES ('desired_titles', 0, 'Platform Engineer')
        """
    )
    connection.execute(
        """
        UPDATE preference_state
        SET explicit_configured=1,
            explicit_updated_at='2026-01-03T00:00:00+00:00',
            learning_reset_review_event_id=?
        WHERE singleton=1
        """,
        (event_id,),
    )
    connection.commit()


def _snapshot_rows(
    connection: sqlite3.Connection,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'schema_metadata' ORDER BY name"
        )
    ]
    return {
        table: tuple(
            tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')
        )
        for table in tables
    }


def _schema_signature(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str | None], ...]:
    return tuple(
        (str(row[0]), str(row[1]), str(row[2]), row[3])
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


def test_fresh_v13_has_exact_tables_and_empty_profile_state(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 13
    with connect(tmp_path / "fresh.sqlite3") as connection:
        initialize_schema(connection)
        assert inspect_schema(connection) == "current"
        present = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(V13_TABLES) <= present
        assert len(set(V13_TABLES)) == 14
        state = tuple(
            connection.execute("SELECT * FROM career_profile_state").fetchone()
        )
        assert state == (1, None, 0, 1)


def test_v12_to_v13_preserves_every_existing_row_and_matches_fresh(
    tmp_path: Path,
) -> None:
    migrated = connect(tmp_path / "migrated.sqlite3")
    initialize_schema(migrated)
    _downgrade_to_v12(migrated)
    _seed_v12_rows(migrated)
    before = _snapshot_rows(migrated)

    assert inspect_schema(migrated) == "upgrade_required"
    assert migrate(migrated) == "current"
    after = _snapshot_rows(migrated)
    assert {table: after[table] for table in before} == before
    assert tuple(migrated.execute("SELECT * FROM career_profile_state").fetchone()) == (
        1,
        None,
        0,
        1,
    )

    fresh = sqlite3.connect(":memory:")
    fresh.row_factory = sqlite3.Row
    initialize_schema(fresh)
    assert _schema_signature(migrated) == _schema_signature(fresh)
    fresh.close()
    migrated.close()


def test_forced_v13_failure_rolls_back_objects_rows_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect(tmp_path / "rollback.sqlite3")
    initialize_schema(connection)
    _downgrade_to_v12(connection)
    _seed_v12_rows(connection)
    before = _snapshot_rows(connection)
    real_apply = core._apply_12_to_13

    def fail_after_ddl(candidate: sqlite3.Connection) -> None:
        real_apply(candidate)
        raise RuntimeError("injected v13 failure")

    monkeypatch.setitem(core.MIGRATIONS, 12, fail_after_ddl)
    with pytest.raises(RuntimeError, match="injected v13 failure"):
        migrate(connection)

    version = connection.execute(
        "SELECT value FROM schema_metadata WHERE key='schema_version'"
    ).fetchone()[0]
    assert version == "12"
    assert _snapshot_rows(connection) == before
    for table in V13_TABLES:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is None
        )
    assert inspect_schema(connection) == "upgrade_required"


def test_v13_target_validation_precedes_metadata_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = connect(tmp_path / "validation-order.sqlite3")
    initialize_schema(connection)
    _downgrade_to_v12(connection)
    observed: list[str] = []
    real_valid = core._valid_structure

    def reject_v13(candidate: sqlite3.Connection, version: int) -> bool:
        if version == 13:
            observed.append(
                str(
                    candidate.execute(
                        "SELECT value FROM schema_metadata WHERE key='schema_version'"
                    ).fetchone()[0]
                )
            )
            return False
        return real_valid(candidate, version)

    monkeypatch.setattr(core, "_valid_structure", reject_v13)
    with pytest.raises(ValueError, match="invalid v13 schema"):
        migrate_12_to_13(connection)
    assert observed == ["12"]
    assert (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "12"
    )
    assert inspect_schema(connection) == "upgrade_required"


def test_partial_extra_and_malformed_v12_fail_closed(tmp_path: Path) -> None:
    partial = connect(tmp_path / "partial.sqlite3")
    initialize_schema(partial)
    _downgrade_to_v12(partial)
    partial.execute("CREATE TABLE resume_partial(id INTEGER PRIMARY KEY)")
    partial.commit()
    assert inspect_schema(partial) == "malformed"
    with pytest.raises(ValueError, match="malformed"):
        migrate(partial)

    malformed = connect(tmp_path / "malformed.sqlite3")
    initialize_schema(malformed)
    _downgrade_to_v12(malformed)
    malformed.execute("DROP TABLE preference_state")
    malformed.commit()
    assert inspect_schema(malformed) == "malformed"

    extra = connect(tmp_path / "extra.sqlite3")
    initialize_schema(extra)
    extra.execute("CREATE TABLE career_unexpected(id INTEGER PRIMARY KEY)")
    extra.commit()
    assert inspect_schema(extra) == "malformed"


def test_default_migration_backup_is_private_pre_v13(tmp_path: Path) -> None:
    database = tmp_path / "admin.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        _downgrade_to_v12(connection)
        _seed_v12_rows(connection)

    assert migrate_database(database) == "current"
    backup = tmp_path / "admin.sqlite3.pre-v13.backup.sqlite3"
    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert inspect_database(backup) == "upgrade_required"
    assert inspect_database(database) == "current"


def test_migration_dispatcher_includes_v13_boundary() -> None:
    assert set(core.MIGRATIONS) == {8, 9, 10, 11, 12}
