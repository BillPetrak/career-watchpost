import os
import stat

import pytest

from careeros.materials.models import StorageError
from careeros.materials.storage import (
    ensure_binding,
    finalize_stage,
    install_marker,
    marker_id,
    open_bound_object,
    prepare_root,
    stage_bytes,
    stage_source,
)


def test_private_stage_and_marker(tmp_path):
    root = tmp_path / "materials"
    prepare_root(root)
    stage, size, digest = stage_bytes(root, [b"abc", b"def"])
    assert size == 6 and stage.stat().st_mode & 0o777 == 0o600
    install_marker(root, "a" * 32)
    assert marker_id(root) == "a" * 32


def test_open_bound_object_uses_validated_root_and_rejects_replacement(
    tmp_path, monkeypatch
):
    import shutil

    import careeros.materials.storage as storage

    root = tmp_path / "materials"
    stage, _, _ = stage_bytes(root, [b"trusted"])
    storage.finalize_stage(root, stage, "a" * 64)
    install_marker(root, "b" * 32)
    original = storage.ensure_binding_fd
    replacement = tmp_path / "replacement"
    original_root = tmp_path / "original"

    def validate_then_replace(rfd, configured, store_id):
        result = original(rfd, configured, store_id)
        shutil.copytree(root, replacement)
        root.rename(original_root)
        replacement.rename(root)
        blob = next(root.glob("objects/*/*/*.blob"))
        blob.write_bytes(b"malicious")
        return result

    monkeypatch.setattr(storage, "ensure_binding_fd", validate_then_replace)
    with pytest.raises(StorageError):
        open_bound_object(root, "b" * 32, "a" * 64)


def test_bound_missing_root_fails_closed(tmp_path):
    root = tmp_path / "materials"
    prepare_root(root)
    install_marker(root, "a" * 32)
    root.rename(tmp_path / "removed")
    with pytest.raises(StorageError):
        ensure_binding(root, "a" * 32, allow_create=False)
    assert not root.exists()


def test_unbound_absent_root_is_read_only_admissible(tmp_path):
    root = tmp_path / "absent"
    assert ensure_binding(root, None, allow_create=False) is None
    assert not root.exists()


def test_unbound_absent_root_beneath_symlinked_parent_fails_closed(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(target, target_is_directory=True)
    root = linked_parent / "absent"

    with pytest.raises(StorageError):
        ensure_binding(root, None, allow_create=False)

    assert not (target / "absent").exists()


def test_bound_absent_root_fails_without_creation(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(StorageError):
        ensure_binding(root, "a" * 32, allow_create=False)
    assert not root.exists()


@pytest.mark.parametrize("store_id", [None, "a" * 32])
def test_empty_root_classification_is_read_only(tmp_path, store_id):
    root = tmp_path / "empty"
    root.mkdir(mode=0o700)
    before = list(root.iterdir())
    if store_id is None:
        assert ensure_binding(root, None, allow_create=False) is None
    else:
        with pytest.raises(StorageError):
            ensure_binding(root, store_id, allow_create=False)
    assert list(root.iterdir()) == before


@pytest.mark.parametrize("unsafe", ["symlink", "file", "mode"])
def test_binding_rejects_structurally_unsafe_staging(tmp_path, unsafe):
    root = tmp_path / "materials"
    prepare_root(root)
    staging = root / ".staging"
    if unsafe == "symlink":
        staging.rmdir()
        target = tmp_path / "external-staging"
        target.mkdir(mode=0o700)
        staging.symlink_to(target, target_is_directory=True)
    elif unsafe == "file":
        staging.rmdir()
        staging.write_text("not a directory")
        staging.chmod(0o700)
    else:
        staging.chmod(0o755)

    with pytest.raises(StorageError):
        ensure_binding(root, None, allow_create=False)


def test_missing_careeros_parent_created_safely_under_umask(tmp_path):
    share = tmp_path / "share"
    share.mkdir(mode=0o755)
    root = share / "careeros" / "materials"
    old_umask = os.umask(0o077)
    try:
        stage, _, _ = stage_bytes(root, [b"x"])
    finally:
        os.umask(old_umask)
    assert stage.exists()
    assert stat.S_IMODE((share / "careeros").stat().st_mode) == 0o700
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_symlink_careeros_parent_rejected_without_mutation(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (share / "careeros").symlink_to(target, target_is_directory=True)
    root = share / "careeros" / "materials"
    with pytest.raises(StorageError):
        stage_bytes(root, [b"x"])
    assert not (target / "materials").exists()


def test_hardlinked_source_is_copied_to_independent_inode(tmp_path):
    root = tmp_path / "materials"
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    alias = tmp_path / "alias"
    alias.hardlink_to(source)
    stage, size, digest = stage_source(root, alias)
    assert size == 7 and digest
    assert stage.stat().st_ino != source.stat().st_ino


def test_source_mutation_is_rejected(tmp_path, monkeypatch):
    import careeros.materials.storage as storage

    root = tmp_path / "materials"
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    original = storage._stage_fd

    def mutate(root_arg, fd):
        result = original(root_arg, fd)
        source.write_bytes(b"changed")
        return result

    monkeypatch.setattr(storage, "_stage_fd", mutate)
    with pytest.raises(StorageError):
        stage_source(root, source)
    assert not list((root / ".staging").iterdir())


def test_shards_and_final_mode_ignore_umask(tmp_path):
    root = tmp_path / "materials"
    old = os.umask(0o077)
    try:
        stage, _, _ = stage_bytes(root, [b"x"])
        final = finalize_stage(root, stage, "a" * 64)
    finally:
        os.umask(old)
    assert stat.S_IMODE((root / "objects" / "aa").stat().st_mode) == 0o700
    assert stat.S_IMODE(final.stat().st_mode) == 0o600


def test_finalize_rejects_stage_replacement_between_check_and_link(
    tmp_path, monkeypatch
):
    import careeros.materials.storage as storage

    root = tmp_path / "materials"
    stage, size, digest = stage_bytes(root, [b"original"])
    identity = storage.stage_identity(stage)
    real_link = storage.os.link

    def replace_then_link(src, dst, *, src_dir_fd, dst_dir_fd, follow_symlinks=True):
        os.unlink(src, dir_fd=src_dir_fd)
        fd = os.open(
            src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=src_dir_fd
        )
        os.write(fd, b"replacement")
        os.close(fd)
        return real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(storage.os, "link", replace_then_link)
    with pytest.raises(StorageError):
        storage.finalize_stage(
            root,
            stage,
            "b" * 64,
            expected_size=size,
            expected_identity=identity,
        )
    assert not list((root / "objects").rglob("*.blob"))
