from careeros.database.connection import connect
from careeros.database.migrations.core import inspect_schema, migrate
from careeros.database.schema import initialize_schema


def test_v9_to_v10(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    for t in ("material_versions", "materials", "material_store"):
        c.execute("drop table " + t)
    c.execute("update schema_metadata set value='9'")
    c.commit()
    assert migrate(c) == "current"
    assert inspect_schema(c) == "current"


def test_v10_with_missing_material_table_is_malformed(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    c.execute("DROP TABLE material_versions")
    c.execute("UPDATE schema_metadata SET value='10'")
    c.commit()
    assert inspect_schema(c) == "malformed"


def test_v8_to_v10_failure_rolls_back_intermediate_step(tmp_path, monkeypatch):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    c.executescript(
        """
        DROP TABLE application_requirement_items;
        DROP TABLE application_preparations;
        ALTER TABLE application_events DROP COLUMN note_redacted_at;
        UPDATE schema_metadata SET value='8';
        """
    )
    c.commit()

    from careeros.database.migrations import core

    def fail_after_v9(_connection):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(core, "_apply_9_to_10", fail_after_v9)
    try:
        migrate(c)
    except RuntimeError:
        pass
    else:
        raise AssertionError("migration unexpectedly succeeded")
    assert (
        c.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
        == "8"
    )
    assert (
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='application_preparations'"
        ).fetchone()
        is None
    )
