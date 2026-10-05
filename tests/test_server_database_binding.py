"""Regression tests for CLI-selected API database binding."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from careeros.api.main import create_app
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.materials.service import create_material
from careeros.materials.storage import (
    finalize_stage,
    install_marker,
    prepare_root,
    stage_bytes,
)
from careeros.server import serve, validate_startup_database
from tests.support import TestClient


def _seed(path: Path) -> None:
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            CollectedJob(
                source="test",
                source_job_id="job-1",
                title="Backend Engineer",
                company="Example",
                location=None,
                description="Build useful things.",
                url=None,
                employment_type=None,
                remote_status="remote",
                published_at=None,
                collected_at="2026-01-01T00:00:00+00:00",
            )
        ],
    )
    connection.commit()
    connection.close()


def _bind(path: Path, root: Path, store_id: str = "a" * 32) -> None:
    prepare_root(root)
    install_marker(root, store_id)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO material_store(singleton,store_id,created_at) VALUES(1,?,?)",
            (store_id, "now"),
        )


def test_custom_app_binds_read_and_write_paths(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.sqlite3"
    settings_path = tmp_path / "settings.sqlite3"
    _seed(selected)
    monkeypatch.setenv("CAREEROS_DATABASE", str(settings_path))

    with TestClient(create_app(selected)) as client:
        assert client.get("/health").json()["database"] == "present"
        response = client.put("/review-decisions/1", json={"decision": "shortlist"})

    assert response.status_code == 200
    connection = connect(selected)
    assert (
        connection.execute(
            "SELECT decision FROM review_decisions WHERE job_id = 1"
        ).fetchone()[0]
        == "shortlist"
    )
    connection.close()
    assert not settings_path.exists()


def test_default_app_remains_settings_driven_and_does_not_migrate(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "settings.sqlite3"
    selected = tmp_path / "other.sqlite3"
    _seed(settings_path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(settings_path))
    monkeypatch.setenv("CAREEROS_MATERIALS_ROOT", str(tmp_path / "materials"))

    with TestClient(create_app()) as client:
        assert client.get("/health").json()["schema_version"] == 14

    assert not selected.exists()


@pytest.mark.parametrize("state", ["absent", "empty", "initialized", "marker_only"])
def test_unbound_startup_accepts_only_safe_unpopulated_states(tmp_path, state):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    if state == "empty":
        root.mkdir(mode=0o700)
    elif state in {"initialized", "marker_only"}:
        prepare_root(root)
        if state == "marker_only":
            install_marker(root, "a" * 32)
    validate_startup_database(database, root)
    if state == "absent":
        assert not root.exists()


def test_unbound_populated_root_fails_closed(tmp_path):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    stage, _, _ = stage_bytes(root, [b"orphan"])
    finalize_stage(root, stage, "b" * 64)
    with pytest.raises(ValueError, match="matching materials store"):
        validate_startup_database(database, root)


def test_unbound_startup_rejects_absent_root_beneath_symlinked_parent(tmp_path):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    target = tmp_path / "target"
    target.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target, target_is_directory=True)
    root = linked_parent / "materials"

    with pytest.raises(ValueError, match="matching materials store"):
        validate_startup_database(database, root)

    assert not (target / "materials").exists()


def test_unbound_startup_rejects_symlinked_staging(tmp_path):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    prepare_root(root)
    (root / ".staging").rmdir()
    target = tmp_path / "external-staging"
    target.mkdir(mode=0o700)
    (root / ".staging").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="matching materials store"):
        validate_startup_database(database, root)

    assert (root / ".staging").is_symlink()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("state", ["absent", "mismatch", "marker_symlink"])
def test_bound_startup_fails_closed_for_unavailable_or_wrong_root(tmp_path, state):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    _bind(database, root)
    if state == "absent":
        root.rename(tmp_path / "removed")
    elif state == "mismatch":
        marker = root / ".careeros-store"
        marker.write_text("careeros-material-store-v1:" + "b" * 32 + "\n")
        marker.chmod(0o600)
    else:
        marker = root / ".careeros-store"
        marker.unlink()
        marker.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="matching materials store"):
        validate_startup_database(database, root)


@pytest.mark.parametrize(
    "unsafe", ["root_symlink", "parent_symlink", "root_mode", "root_owner"]
)
def test_unbound_unsafe_selected_root_fails_closed(tmp_path, monkeypatch, unsafe):
    import careeros.materials.storage as storage

    database = tmp_path / "db.sqlite3"
    _seed(database)
    if unsafe == "parent_symlink":
        target = tmp_path / "target"
        target.mkdir()
        prepare_root(target / "materials")
        parent = tmp_path / "linked-parent"
        parent.symlink_to(target, target_is_directory=True)
        root = parent / "materials"
    else:
        root = tmp_path / "materials"
        if unsafe == "root_symlink":
            target = tmp_path / "target"
            prepare_root(target)
            root.symlink_to(target, target_is_directory=True)
        else:
            prepare_root(root)
            if unsafe == "root_mode":
                root.chmod(0o755)
            else:
                uid = os.getuid()
                monkeypatch.setattr(storage.os, "getuid", lambda: uid + 1)
    with pytest.raises(ValueError, match="matching materials store"):
        validate_startup_database(database, root)


def test_explicit_materials_root_is_the_root_validated(tmp_path, monkeypatch):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    configured = tmp_path / "configured"
    stage, _, _ = stage_bytes(configured, [b"orphan"])
    finalize_stage(configured, stage, "c" * 64)
    selected = tmp_path / "selected"
    selected.mkdir(mode=0o700)
    monkeypatch.setenv("CAREEROS_MATERIALS_ROOT", str(configured))
    validate_startup_database(database, selected)
    with pytest.raises(ValueError, match="matching materials store"):
        validate_startup_database(database, configured)


def test_startup_validation_is_read_only_and_mutation_free(tmp_path):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    prepare_root(root)
    before_db = (hashlib.sha256(database.read_bytes()).digest(), database.stat())
    before_tree = sorted(
        (str(path.relative_to(root)), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in root.rglob("*")
    )
    validate_startup_database(database, root)
    after_db = (hashlib.sha256(database.read_bytes()).digest(), database.stat())
    after_tree = sorted(
        (str(path.relative_to(root)), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in root.rglob("*")
    )
    assert before_db[0] == after_db[0]
    assert before_db[1].st_mtime_ns == after_db[1].st_mtime_ns
    assert before_tree == after_tree


def test_startup_does_not_perform_full_object_hash_audit(tmp_path):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    connection = connect(database)
    stage, size, digest = stage_bytes(root, [b"original"])
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
    blob = next((root / "objects").rglob("*.blob"))
    blob.write_bytes(b"tampered")
    blob.chmod(0o600)
    # Startup validates schema/root/marker binding only; material audit owns
    # the O(total bytes) content-integrity check.
    validate_startup_database(database, root)


def test_direct_create_app_lifespan_enforces_unbound_root_state(tmp_path):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    stage, _, _ = stage_bytes(root, [b"orphan"])
    finalize_stage(root, stage, "d" * 64)
    with pytest.raises(ValueError, match="matching materials store"):
        with TestClient(create_app(database, root)):
            pass


@pytest.mark.parametrize("unsafe", ["absent_under_symlink", "symlinked_staging"])
def test_direct_create_app_lifespan_rejects_unsafe_materials_root(tmp_path, unsafe):
    database = tmp_path / "db.sqlite3"
    _seed(database)
    if unsafe == "absent_under_symlink":
        target = tmp_path / "target"
        target.mkdir()
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(target, target_is_directory=True)
        root = linked_parent / "materials"
    else:
        root = tmp_path / "materials"
        prepare_root(root)
        (root / ".staging").rmdir()
        target = tmp_path / "external-staging"
        target.mkdir(mode=0o700)
        (root / ".staging").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="matching materials store"):
        with TestClient(create_app(database, root)):
            pass


@pytest.mark.parametrize(("ipv6", "host"), [(False, "127.0.0.1"), (True, "::1")])
def test_official_serve_remains_loopback_only_with_proxy_trust_disabled(
    tmp_path, monkeypatch, ipv6, host
):
    import uvicorn

    database = tmp_path / "db.sqlite3"
    _seed(database)
    root = tmp_path / "materials"
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    serve(database=database, materials_root=root, ipv6=ipv6, port=8765)
    assert calls == [
        {
            "host": host,
            "port": 8765,
            "proxy_headers": False,
            "forwarded_allow_ips": "",
            "reload": False,
            "access_log": False,
            "workers": 1,
        }
    ]
