from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.materials.audit import (
    FIXED_MESSAGE,
    AuditAttempt,
    _value,
    audit,
    audit_attempt,
    audit_database,
    fingerprint,
)
from careeros.materials.service import create_material
from careeros.materials.storage import (
    finalize_stage,
    install_marker,
    prepare_root,
    stage_bytes,
)


def _database(path: Path) -> Path:
    connection = connect(path)
    initialize_schema(connection)
    connection.close()
    return path


def _bound_store(tmp_path: Path) -> tuple[Path, Path, Path]:
    database = _database(tmp_path / "db")
    connection = connect(database)
    root = tmp_path / "root"
    stage, size, digest = stage_bytes(root, [b"payload"])
    create_material(
        connection,
        root,
        name="Resume",
        category="resume",
        filename="resume.pdf",
        media_type="application/pdf",
        stage=stage,
        size=size,
        sha256=digest,
    )
    connection.close()
    return database, root, next((root / "objects").rglob("*.blob"))


def test_fingerprint_changes_on_persisted_material_column(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    before = fingerprint(c)
    c.execute(
        "insert into materials(id,name,category,created_at,updated_at) "
        "values(?,?,?,?,?)",
        ("a" * 32, "R", "resume", "t", "t"),
    )
    after = fingerprint(c)
    assert before != after


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("material_store", "singleton", 2),
        ("material_store", "store_id", "b" * 32),
        ("material_store", "created_at", "changed"),
        ("materials", "id", "b" * 32),
        ("materials", "name", "Changed"),
        ("materials", "category", "cover_letter_base"),
        ("materials", "revision", 2),
        ("materials", "status", "retired"),
        ("materials", "retired_at", "changed"),
        ("materials", "created_at", "changed"),
        ("materials", "updated_at", "changed"),
        ("material_versions", "id", "c" * 32),
        ("material_versions", "material_id", "b" * 32),
        ("material_versions", "version_number", 2),
        ("material_versions", "display_filename", "changed.pdf"),
        ("material_versions", "declared_media_type", "text/plain"),
        ("material_versions", "byte_size", 8),
        ("material_versions", "sha256", "d" * 64),
        ("material_versions", "storage_key", "e" * 64),
        ("material_versions", "created_at", "changed"),
    ],
)
def test_fingerprint_covers_every_persisted_m2_column(tmp_path, table, column, value):
    database, _, _ = _bound_store(tmp_path)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("PRAGMA ignore_check_constraints=ON")
    before = fingerprint(connection)
    connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    assert fingerprint(connection) != before
    connection.close()


def test_fingerprint_covers_schema_version(tmp_path):
    connection = connect(_database(tmp_path / "db"))
    before = fingerprint(connection)
    connection.execute(
        "UPDATE schema_metadata SET value='10' WHERE key='schema_version'"
    )
    assert fingerprint(connection) != before
    connection.close()


@pytest.mark.parametrize(
    "value", [None, 0, 1, -1, "", "a", "a,b", "unicode-\u03b1", "x" * 100]
)
def test_canonical_framing_is_binary_and_type_specific(value):
    encoded = _value(value)
    assert encoded[:1] in (b"\x00", b"\x01", b"\x02")
    if isinstance(value, str):
        assert value.encode() in encoded


@pytest.mark.parametrize("name", ["bad", "x" * 64 + ".part"])
def test_staging_names_are_not_assumed_safe(tmp_path, name):
    root = tmp_path / "root"
    from careeros.materials.storage import prepare_root

    prepare_root(root)
    (root / ".staging" / name).write_bytes(b"x")
    c = __import__("careeros.database.connection", fromlist=["connect"]).connect(
        tmp_path / "db"
    )
    __import__(
        "careeros.database.schema", fromlist=["initialize_schema"]
    ).initialize_schema(c)
    result = audit(c, root)[1]
    assert result["stale_staging"] + result["invalid"] >= 1


@pytest.mark.parametrize("root_state", ["absent", "empty", "initialized"])
def test_unbound_state_a_root_is_coherent_and_read_only(tmp_path, root_state):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    if root_state == "empty":
        root.mkdir(mode=0o700)
    elif root_state == "initialized":
        prepare_root(root)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    code, message, counts = audit_database(database, root)
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert (code, message, counts) == (
        0,
        None,
        {"missing": 0, "unexpected": 0, "invalid": 0, "stale_staging": 0},
    )
    # Audit may update only the informational collection-lock companion.
    assert set(after) - set(before) <= {"db.lock"}
    if root_state == "absent":
        assert not root.exists()


def test_unbound_marker_only_root_is_coherent(tmp_path):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    install_marker(root, "a" * 32)
    assert audit_database(database, root)[0] == 0


@pytest.mark.parametrize("unsafe", ["root_mode", "objects_mode", "staging_type"])
def test_unsafe_root_or_managed_directory_is_inconsistent(tmp_path, unsafe):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    if unsafe == "root_mode":
        root.chmod(0o755)
    elif unsafe == "objects_mode":
        (root / "objects").chmod(0o755)
    else:
        (root / ".staging").rmdir()
        (root / ".staging").write_bytes(b"not a directory")
    code, _, counts = audit_database(database, root)
    assert code == 1
    assert counts["invalid"] >= 1


def test_unbound_populated_root_is_inconsistent(tmp_path):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    stage, _, _ = stage_bytes(root, [b"orphan"])
    finalize_stage(root, stage, "a" * 64)
    code, _, counts = audit_database(database, root)
    assert code == 1
    assert counts["unexpected"] == 1
    assert counts["invalid"] >= 1


@pytest.mark.parametrize("kind", ["symlink", "malformed", "mode", "directory"])
def test_unsafe_or_malformed_marker_is_never_absent(tmp_path, kind):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    marker = root / ".careeros-store"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("careeros-material-store-v1:" + "a" * 32 + "\n")
        marker.symlink_to(target)
    elif kind == "directory":
        marker.mkdir(mode=0o700)
    else:
        marker.write_text(
            "malformed\n"
            if kind == "malformed"
            else "careeros-material-store-v1:" + "a" * 32 + "\n"
        )
        marker.chmod(0o644 if kind == "mode" else 0o600)
    code, _, counts = audit_database(database, root)
    assert code == 1
    assert counts["invalid"] >= 1


@pytest.mark.parametrize("attack", ["root_symlink", "parent_symlink"])
def test_configured_path_symlink_in_any_component_is_inconsistent(tmp_path, attack):
    database = _database(tmp_path / "db")
    target_parent = tmp_path / "target-parent"
    target_parent.mkdir()
    prepare_root(target_parent / "root")
    if attack == "root_symlink":
        root = tmp_path / "root"
        root.symlink_to(target_parent / "root", target_is_directory=True)
    else:
        parent = tmp_path / "parent"
        parent.symlink_to(target_parent, target_is_directory=True)
        root = parent / "root"
    code, _, counts = audit_database(database, root)
    assert code == 1
    assert counts["invalid"] >= 1


@pytest.mark.parametrize("replacement", ["root", "parent"])
def test_configured_path_replacement_discards_first_attempt(
    tmp_path, monkeypatch, replacement
):
    import careeros.materials.audit as material_audit

    database = _database(tmp_path / "db")
    parent = tmp_path / "configured"
    parent.mkdir(mode=0o700)
    root = parent / "root"
    prepare_root(root)
    calls = 0
    original = material_audit._capture_authority

    def capture(database_arg, root_arg):
        nonlocal calls
        calls += 1
        if calls == 2:
            if replacement == "root":
                prepare_root(parent / "new-root")
                root.rename(parent / "old-root")
                (parent / "new-root").rename(root)
            else:
                new_parent = tmp_path / "new-parent"
                new_parent.mkdir(mode=0o700)
                prepare_root(new_parent / "root")
                parent.rename(tmp_path / "old-parent")
                new_parent.rename(parent)
        return original(database_arg, root_arg)

    monkeypatch.setattr(material_audit, "_capture_authority", capture)
    assert audit_database(database, root)[0] == 0
    assert calls == 4


def test_marker_mutation_retries_once_then_certifies_stable_snapshot(
    tmp_path, monkeypatch
):
    import careeros.materials.audit as material_audit

    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    install_marker(root, "a" * 32)
    calls = 0
    original = material_audit._capture_authority

    def capture(database_arg, root_arg):
        nonlocal calls
        calls += 1
        if calls == 2:
            marker = root / ".careeros-store"
            marker.write_text("careeros-material-store-v1:" + "b" * 32 + "\n")
            marker.chmod(0o600)
        return original(database_arg, root_arg)

    monkeypatch.setattr(material_audit, "_capture_authority", capture)
    assert audit_database(database, root)[0] == 0
    assert calls == 4


def test_db_mutation_between_windows_discards_first_attempt(tmp_path, monkeypatch):
    import careeros.materials.audit as material_audit

    database = _database(tmp_path / "db")
    root = tmp_path / "absent"
    calls = 0
    original = material_audit._capture_authority

    def capture(database_arg, root_arg):
        nonlocal calls
        calls += 1
        if calls == 2:
            with sqlite3.connect(database) as writer:
                writer.execute(
                    "INSERT INTO materials(id,name,category,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    ("a" * 32, "Resume", "resume", "t", "t"),
                )
        return original(database_arg, root_arg)

    monkeypatch.setattr(material_audit, "_capture_authority", capture)
    assert audit_database(database, root)[0] == 0
    assert calls == 4


def test_repeated_schema_instability_returns_fixed_75(tmp_path, monkeypatch):
    import careeros.materials.audit as material_audit

    database = _database(tmp_path / "db")
    calls = 0
    original = material_audit._capture_authority

    def capture(database_arg, root_arg):
        nonlocal calls
        calls += 1
        version = "13" if calls in (2, 4) else "14"
        with sqlite3.connect(database) as writer:
            writer.execute(
                "UPDATE schema_metadata SET value=? WHERE key='schema_version'",
                (version,),
            )
        return original(database_arg, root_arg)

    monkeypatch.setattr(material_audit, "_capture_authority", capture)
    code, message, counts = audit_database(database, tmp_path / "absent")
    assert (code, message) == (75, FIXED_MESSAGE)
    assert not any(counts.values())


@pytest.mark.parametrize("mutation", ["add", "remove", "replace", "metadata"])
def test_object_inventory_or_metadata_mutation_is_unstable(
    tmp_path, monkeypatch, mutation
):
    import careeros.materials.audit as material_audit

    database, root, blob = _bound_store(tmp_path)
    connection = connect(database)
    original = material_audit._scan_objects

    def scan(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == "add":
            added = root / "objects" / "ff" / "ff"
            added.mkdir(parents=True, mode=0o700)
            (added / ("f" * 64 + ".blob")).write_bytes(b"new")
        elif mutation == "remove":
            blob.unlink()
        elif mutation == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(b"payload")
            replacement.chmod(0o600)
            os.replace(replacement, blob)
        else:
            blob.chmod(0o400)
        return result

    monkeypatch.setattr(material_audit, "_scan_objects", scan)
    assert audit_attempt(connection, root).unstable
    connection.close()


@pytest.mark.parametrize("mutation", ["add", "remove", "replace"])
def test_stage_inventory_mutation_is_unstable(tmp_path, monkeypatch, mutation):
    import careeros.materials.audit as material_audit

    database = _database(tmp_path / "db")
    connection = connect(database)
    root = tmp_path / "root"
    prepare_root(root)
    stage = root / ".staging" / ("a" * 64 + ".part")
    if mutation != "add":
        stage.write_bytes(b"stage")
        stage.chmod(0o600)
    original = material_audit._scan_staging
    calls = 0

    def scan(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            if mutation == "add":
                stage.write_bytes(b"stage")
                stage.chmod(0o600)
            elif mutation == "remove":
                stage.unlink()
            else:
                replacement = tmp_path / "stage-replacement"
                replacement.write_bytes(b"stage")
                replacement.chmod(0o600)
                os.replace(replacement, stage)
        return result

    monkeypatch.setattr(material_audit, "_scan_staging", scan)
    assert audit_attempt(connection, root).unstable
    connection.close()


def test_unstable_scan_still_uses_second_window_before_retry(tmp_path, monkeypatch):
    import careeros.materials.audit as material_audit

    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    captures = 0
    scans = 0
    original_capture = material_audit._capture_authority
    original_attempt = material_audit.audit_attempt

    def capture(*args, **kwargs):
        nonlocal captures
        captures += 1
        return original_capture(*args, **kwargs)

    def attempt(*args, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 1:
            return AuditAttempt(True, False, {key: 0 for key in ZERO_KEYS})
        return original_attempt(*args, **kwargs)

    ZERO_KEYS = ("missing", "unexpected", "invalid", "stale_staging")
    monkeypatch.setattr(material_audit, "_capture_authority", capture)
    monkeypatch.setattr(material_audit, "audit_attempt", attempt)
    assert audit_database(database, root)[0] == 0
    assert (captures, scans) == (4, 2)


def test_valid_young_stage_is_tolerated(tmp_path):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    stage = root / ".staging" / ("a" * 64 + ".part")
    stage.write_bytes(b"active")
    stage.chmod(0o600)
    assert audit_database(database, root)[0] == 0


def test_valid_old_stage_is_stale_inconsistency(tmp_path):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    stage = root / ".staging" / ("a" * 64 + ".part")
    stage.write_bytes(b"abandoned")
    stage.chmod(0o600)
    old = stage.stat().st_mtime - 90000
    os.utime(stage, (old, old))
    code, _, counts = audit_database(database, root)
    assert code == 1
    assert counts["stale_staging"] == 1


@pytest.mark.parametrize("unsafe", ["malformed", "mode", "symlink", "nested"])
def test_invalid_young_stage_is_immediately_inconsistent(tmp_path, unsafe):
    database = _database(tmp_path / "db")
    root = tmp_path / "root"
    prepare_root(root)
    stage = root / ".staging" / ("a" * 64 + ".part")
    if unsafe == "malformed":
        stage = root / ".staging" / "young.part"
        stage.write_bytes(b"x")
    elif unsafe == "symlink":
        stage.symlink_to(tmp_path / "missing")
    elif unsafe == "nested":
        stage.mkdir(mode=0o700)
        nested = stage / ("b" * 64 + ".part")
        nested.write_bytes(b"x")
    else:
        stage.write_bytes(b"x")
        stage.chmod(0o644)
    code, _, counts = audit_database(database, root)
    assert code == 1
    assert counts["invalid"] >= 1


def test_lock_contention_returns_75_without_findings(tmp_path):
    database = _database(tmp_path / "db")
    with collection_lock(database, command="test holder"):
        code, message, counts = audit_database(database, tmp_path / "absent")
    assert (code, message) == (75, None)
    assert not any(counts.values())


def test_object_bytes_are_never_hashed_under_collection_lock(tmp_path, monkeypatch):
    import careeros.materials.audit as material_audit

    database, root, _ = _bound_store(tmp_path)
    held = False
    real_lock = collection_lock
    real_hash = material_audit._hash_fd

    @contextmanager
    def tracking_lock(*args, **kwargs):
        nonlocal held
        with real_lock(*args, **kwargs):
            held = True
            try:
                yield
            finally:
                held = False

    def checked_hash(fd):
        assert not held
        return real_hash(fd)

    monkeypatch.setattr(material_audit, "collection_lock", tracking_lock)
    monkeypatch.setattr(material_audit, "_hash_fd", checked_hash)
    assert audit_database(database, root)[0] == 0


def test_stable_bound_store_is_coherent(tmp_path):
    database, root, blob = _bound_store(tmp_path)
    assert stat.S_IMODE(blob.stat().st_mode) == 0o600
    assert audit_database(database, root)[0] == 0
