from careeros.database.connection import connect
from careeros.database.migrations.core import V13_TABLES, inspect_schema, migrate
from careeros.database.schema import initialize_schema

PREFERENCE_TABLES = (
    "learned_preference_suppressions",
    "preference_evidence",
    "explicit_preferences",
    "preference_state",
)


def test_v9_to_v10(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    for table in reversed(V13_TABLES):
        c.execute("DROP TABLE " + table)
    for table in PREFERENCE_TABLES:
        c.execute("DROP TABLE " + table)
    for table in (
        "package_completeness_reasons",
        "package_answers",
        "package_material_selections",
        "application_package_requirement_snapshots",
        "application_package_versions",
        "application_packages",
    ):
        c.execute("DROP TABLE " + table)
    for t in ("material_versions", "materials", "material_store"):
        c.execute("drop table " + t)
    c.execute("update schema_metadata set value='9'")
    c.commit()
    assert migrate(c) == "current"
    assert inspect_schema(c) == "current"


def test_v10_with_missing_material_table_is_malformed(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    for table in reversed(V13_TABLES):
        c.execute("DROP TABLE " + table)
    c.execute("DROP TABLE material_versions")
    c.execute("UPDATE schema_metadata SET value='10'")
    c.commit()
    assert inspect_schema(c) == "malformed"


def test_v8_to_v10_failure_rolls_back_intermediate_step(tmp_path, monkeypatch):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    for table in PREFERENCE_TABLES:
        c.execute("DROP TABLE " + table)
    c.executescript(
        """
        DROP TABLE package_completeness_reasons;
        DROP TABLE package_answers;
        DROP TABLE package_material_selections;
        DROP TABLE application_package_requirement_snapshots;
        DROP TABLE application_package_versions;
        DROP TABLE application_packages;
        DROP TABLE material_versions;
        DROP TABLE materials;
        DROP TABLE material_store;
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

    monkeypatch.setitem(core.MIGRATIONS, 9, fail_after_v9)
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
