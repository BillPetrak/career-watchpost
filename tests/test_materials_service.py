import pytest

from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.materials.models import ConflictError, NotFoundError, StorageError
from careeros.materials.service import add_version, create_material, preflight_binding
from careeros.materials.storage import install_marker, prepare_root, stage_bytes


def test_duplicate_version_is_idempotent(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    root = tmp_path / "root"
    s, n, h = stage_bytes(root, [b"a"])
    item = create_material(
        c,
        root,
        name="R",
        category="resume",
        filename="x",
        media_type="application/pdf",
        stage=s,
        size=n,
        sha256=h,
    )
    s, n, h = stage_bytes(root, [b"a"])
    same, status = add_version(
        c,
        root,
        item["material_id"],
        expected_revision=99,
        filename="y",
        media_type="application/pdf",
        stage=s,
        size=n,
        sha256=h,
    )
    assert status == 200 and same["revision"] == 1


def test_preflight_does_not_recreate_bound_missing_root(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    root = tmp_path / "root"
    prepare_root(root)
    install_marker(root, "a" * 32)
    c.execute(
        "INSERT INTO material_store(singleton,store_id,created_at) VALUES(1,?,?)",
        ("a" * 32, "now"),
    )
    c.commit()
    root.rename(tmp_path / "removed")
    with pytest.raises(StorageError):
        preflight_binding(c, root)
    assert not root.exists()


def test_unknown_add_version_has_no_binding_side_effect(tmp_path):
    c = connect(tmp_path / "db")
    initialize_schema(c)
    root = tmp_path / "root"
    stage, size, digest = stage_bytes(root, [b"payload"])
    with pytest.raises(NotFoundError):
        add_version(
            c,
            root,
            "f" * 32,
            expected_revision=1,
            filename="x",
            media_type="application/pdf",
            stage=stage,
            size=size,
            sha256=digest,
        )
    assert not (root / ".careeros-store").exists()
    assert not (root / "objects" / "ff").exists()


def test_first_import_root_replacement_cleans_original_marker(tmp_path, monkeypatch):
    import careeros.materials.service as service
    from careeros.materials.storage import marker_id

    c = connect(tmp_path / "db")
    initialize_schema(c)
    root = tmp_path / "root"
    stage, size, digest = stage_bytes(root, [b"payload"])
    replacement = tmp_path / "replacement"
    prepare_root(replacement)
    real_finalize = service.finalize_stage

    def replace_after_finalize(*args, **kwargs):
        result = real_finalize(*args, **kwargs)
        root.rename(tmp_path / "original")
        replacement.rename(root)
        return result

    monkeypatch.setattr(service, "finalize_stage", replace_after_finalize)
    with pytest.raises(ConflictError):
        create_material(
            c,
            root,
            name="R",
            category="resume",
            filename="x",
            media_type="application/pdf",
            stage=stage,
            size=size,
            sha256=digest,
        )
    assert c.execute("SELECT count(*) FROM materials").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM material_versions").fetchone()[0] == 0
    assert marker_id(tmp_path / "original") is None
    assert marker_id(root) is None
    assert not list((tmp_path / "original" / "objects").rglob("*.blob"))


def test_cleanup_failure_preserves_binding_failure(tmp_path, monkeypatch):
    import careeros.materials.service as service

    c = connect(tmp_path / "db")
    initialize_schema(c)
    root = tmp_path / "root"
    stage, size, digest = stage_bytes(root, [b"payload"])
    original_identity = service.root_identity
    calls = 0

    def swap_identity(path):
        nonlocal calls
        calls += 1
        identity = original_identity(path)
        return identity if calls == 1 else (identity[0], identity[1] + 1)

    monkeypatch.setattr(service, "root_identity", swap_identity)
    monkeypatch.setattr(
        service,
        "remove_object_fd",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("cleanup")),
    )
    with pytest.raises(ConflictError):
        service.create_material(
            c,
            root,
            name="R",
            category="resume",
            filename="x",
            media_type="application/pdf",
            stage=stage,
            size=size,
            sha256=digest,
        )
