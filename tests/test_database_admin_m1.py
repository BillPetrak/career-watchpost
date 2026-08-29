from pathlib import Path

import pytest

from careeros.database.admin import _check, backup, migrate_database, restore
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema


def fresh(path: Path):
    with connect(path) as connection:
        initialize_schema(connection)


def test_inspection_states(tmp_path: Path):
    missing = tmp_path / "missing"
    assert not missing.exists()
    empty = tmp_path / "empty"
    empty.touch()
    with pytest.raises(ValueError):
        _check(empty)
    current = tmp_path / "current"
    fresh(current)
    assert _check(current) == "current"
    future = tmp_path / "future"
    with connect(future) as c:
        c.execute(
            "create table schema_metadata(key text primary key,value text not null)"
        )
        c.execute("insert into schema_metadata values ('schema_version','99')")
        c.commit()
    assert _check(future) == "future"


def test_backup_is_exclusive_private_and_restore_roundtrip(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    target = tmp_path / "target.sqlite3"
    fresh(source)
    backup(source, destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        backup(source, destination)
    restore(destination, target)
    assert _check(target) == "current"
    with pytest.raises(ValueError):
        backup(destination, destination)


def test_migrate_missing_refuses_without_creating(tmp_path: Path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError):
        migrate_database(path)
    assert not path.exists()


def test_restore_invalid_does_not_touch_target(tmp_path: Path):
    target = tmp_path / "target.sqlite3"
    fresh(target)
    before = target.read_bytes()
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(ValueError):
        restore(bad, target)
    assert target.read_bytes() == before


def test_restore_rejects_alias_symlink_and_sidecars(tmp_path: Path):
    source = tmp_path / "source.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    target = tmp_path / "target.sqlite3"
    fresh(source)
    backup(source, backup_path)
    target.write_bytes(b"target")
    (tmp_path / "alias.sqlite3").symlink_to(backup_path)
    with pytest.raises(ValueError):
        restore(tmp_path / "alias.sqlite3", target)
    (Path(str(target) + "-wal")).write_bytes(b"pending")
    with pytest.raises(ValueError):
        restore(backup_path, target)
