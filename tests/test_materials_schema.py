from careeros.database.connection import connect
from careeros.database.migrations.core import inspect_schema
from careeros.database.schema import initialize_schema


def test_v10_material_schema(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    assert inspect_schema(c) == "current"
    assert {
        r[0] for r in c.execute("select name from sqlite_master where type='table'")
    } >= {"material_store", "materials", "material_versions"}


def test_v10_inspection_rejects_weakened_material_constraint(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    c.execute("PRAGMA writable_schema=ON")
    c.execute(
        "UPDATE sqlite_master SET sql=replace(sql, "
        "'length(storage_key) = 64', 'length(storage_key) > 0') "
        "WHERE type='table' AND name='material_versions'"
    )
    c.execute("PRAGMA writable_schema=OFF")
    c.commit()
    assert inspect_schema(c) == "malformed"


def test_v10_inspection_rejects_additive_constraint_bypass(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    c.execute("PRAGMA writable_schema=ON")
    c.execute(
        "UPDATE sqlite_master SET sql=replace(sql, "
        "'length(storage_key) = 64', 'length(storage_key) = 64 OR 1=1') "
        "WHERE type='table' AND name='material_versions'"
    )
    c.execute("PRAGMA writable_schema=OFF")
    c.commit()
    c.close()
    with connect(tmp_path / "db") as reopened:
        assert inspect_schema(reopened) == "malformed"


def test_v10_inspection_rejects_added_collation(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    c.execute("PRAGMA writable_schema=ON")
    c.execute(
        "UPDATE sqlite_master SET sql=replace(sql, "
        "'store_id = lower(store_id)', "
        "'store_id = lower(store_id) COLLATE NOCASE') "
        "WHERE type='table' AND name='material_store'"
    )
    c.execute("PRAGMA writable_schema=OFF")
    c.commit()
    c.close()
    with connect(tmp_path / "db") as reopened:
        assert inspect_schema(reopened) == "malformed"


def test_v10_inspection_rejects_unexpected_material_store_index(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    c.execute("CREATE INDEX extra_material_store_index ON material_store(created_at)")
    c.commit()
    assert inspect_schema(c) == "malformed"
