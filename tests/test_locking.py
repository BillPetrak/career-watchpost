"""Tests for process-level collection locking mechanics."""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from careeros.locking import (
    CollectionLockConflict,
    UnsafeLockPathError,
    collection_lock,
    database_maintenance_lock,
    lock_path_for,
    notification_lock,
)


def test_lock_path_derives_deterministically(tmp_path: Path) -> None:
    lock_path = lock_path_for(tmp_path / "careeros.sqlite3")
    assert lock_path == tmp_path / "careeros.sqlite3.lock"
    assert lock_path.is_absolute()


def test_lock_path_maps_relative_and_absolute_equivalents(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    assert lock_path_for(database) == lock_path_for(database.resolve())


def test_lock_path_distinct_for_different_databases(tmp_path: Path) -> None:
    first = lock_path_for(tmp_path / "a.sqlite3")
    second = lock_path_for(tmp_path / "b.sqlite3")
    assert first != second


def test_same_database_conflicts(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    with collection_lock(database):
        with pytest.raises(CollectionLockConflict):
            with collection_lock(database):
                pass


def test_relative_and_equivalent_absolute_paths_conflict(tmp_path: Path) -> None:
    absolute = tmp_path / "careeros.sqlite3"
    relative = Path(os.path.relpath(absolute, Path.cwd()))
    with collection_lock(absolute):
        with pytest.raises(CollectionLockConflict):
            with collection_lock(relative):
                pass


def test_different_databases_do_not_conflict(tmp_path: Path) -> None:
    with collection_lock(tmp_path / "a.sqlite3"):
        with collection_lock(tmp_path / "b.sqlite3"):
            pass


def test_lock_releases_after_normal_exit(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    with collection_lock(database):
        pass
    with collection_lock(database):
        pass


def test_lock_releases_after_exception(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    with pytest.raises(RuntimeError):
        with collection_lock(database):
            raise RuntimeError("boom")
    with collection_lock(database):
        pass


def test_existing_unlocked_companion_file_does_not_block(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    lock_path = lock_path_for(database)
    lock_path.write_text("leftover garbage from a dead process\n")
    lock_path.chmod(0o600)
    with collection_lock(database):
        pass


def test_malformed_metadata_does_not_crash_conflict_reporting(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    lock_path = lock_path_for(database)
    lock_path.write_text("not json at all")
    lock_path.chmod(0o600)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(CollectionLockConflict) as exc_info:
            with collection_lock(database):
                pass
        assert exc_info.value.metadata == {}
    finally:
        os.close(fd)


def test_metadata_written_when_lock_acquired(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    with collection_lock(database, command="collect due"):
        payload = json.loads(lock_path_for(database).read_text())
    assert payload["pid"] == str(os.getpid())
    assert payload["command"] == "collect due"
    assert "acquired_at" in payload


def test_conflict_reports_owner_metadata(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    with collection_lock(database, command="collect all"):
        with pytest.raises(CollectionLockConflict) as exc_info:
            with collection_lock(database):
                pass
        metadata = exc_info.value.metadata
        assert metadata["pid"] == str(os.getpid())
        assert metadata["command"] == "collect all"


def test_lock_path_open_failure_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "careeros.sqlite3"

    def fail_open(*args: object, **kwargs: object) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr("careeros.locking.os.open", fail_open)
    with pytest.raises(PermissionError):
        with collection_lock(database):
            pass


_CHILD_HOLD = """\
import sys
from pathlib import Path
from careeros.locking import collection_lock
database = Path(sys.argv[1])
with collection_lock(database, command="collect due"):
    print("LOCKED", flush=True)
    sys.stdin.readline()
"""

_CHILD_EXIT = """\
import os
import sys
from pathlib import Path
from careeros.locking import collection_lock
database = Path(sys.argv[1])
with collection_lock(database, command="collect due"):
    print("LOCKED", flush=True)
os._exit(0)
"""


def _src_on_pythonpath() -> str:
    src = Path(__file__).resolve().parents[1] / "src"
    existing = os.environ.get("PYTHONPATH")
    return str(src) + (os.pathsep + existing if existing else "")


def _start_child(code: str, database: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = _src_on_pythonpath()
    return subprocess.Popen(
        [sys.executable, "-c", code, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_lock_line(child: subprocess.Popen) -> None:
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if line != "LOCKED":
        assert child.stderr is not None
        pytest.fail(f"child did not acquire lock: {child.stderr.read()}")


def test_cross_process_contention(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    child = _start_child(_CHILD_HOLD, database)
    try:
        _wait_for_lock_line(child)
        with pytest.raises(CollectionLockConflict):
            with collection_lock(database):
                pass
    finally:
        assert child.stdin is not None
        child.stdin.write("release\n")
        child.stdin.close()
        child.wait(timeout=10)
    with collection_lock(database):
        pass


def test_lock_released_when_owning_process_dies(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    child = _start_child(_CHILD_EXIT, database)
    try:
        _wait_for_lock_line(child)
    finally:
        child.wait(timeout=10)
    assert lock_path_for(database).exists()
    with collection_lock(database):
        pass


@pytest.mark.parametrize(
    ("lock", "suffix"),
    ((collection_lock, ".lock"), (notification_lock, ".notify.lock")),
)
def test_lock_rejects_symlink_without_modifying_target(
    tmp_path: Path,
    lock,
    suffix: str,
) -> None:
    database = tmp_path / "careeros.sqlite3"
    target = tmp_path / "valuable.txt"
    original = b"must remain byte-for-byte unchanged\n"
    target.write_bytes(original)
    target.chmod(0o600)
    Path(f"{database.resolve()}{suffix}").symlink_to(target)

    with pytest.raises(UnsafeLockPathError, match="symbolic links"):
        with lock(database):
            pass

    assert target.read_bytes() == original


@pytest.mark.parametrize("kind", ("directory", "hardlink", "permissive"))
def test_collection_lock_rejects_unsafe_existing_companion_unchanged(
    tmp_path: Path,
    kind: str,
) -> None:
    database = tmp_path / "careeros.sqlite3"
    lock_path = lock_path_for(database)
    original = b"do not replace or truncate\n"

    if kind == "directory":
        lock_path.mkdir()
    else:
        target = tmp_path / "target"
        target.write_bytes(original)
        target.chmod(0o600)
        if kind == "hardlink":
            os.link(target, lock_path)
        else:
            lock_path.write_bytes(original)
            lock_path.chmod(0o640)

    with pytest.raises(UnsafeLockPathError):
        with collection_lock(database):
            pass

    if kind == "directory":
        assert lock_path.is_dir()
    else:
        assert lock_path.read_bytes() == original
        assert target.read_bytes() == original


def test_collection_lock_rejects_companion_not_owned_by_effective_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "careeros.sqlite3"
    lock_path = lock_path_for(database)
    original = b"owned elsewhere\n"
    lock_path.write_bytes(original)
    lock_path.chmod(0o600)
    actual_owner = lock_path.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: actual_owner + 1)

    with pytest.raises(UnsafeLockPathError, match="not owned"):
        with collection_lock(database):
            pass

    assert lock_path.read_bytes() == original


def test_database_maintenance_rejects_unsafe_notification_companion(
    tmp_path: Path,
) -> None:
    database = tmp_path / "careeros.sqlite3"
    target = tmp_path / "notification-target"
    target.write_bytes(b"unchanged")
    target.chmod(0o600)
    lock_path_for(database, ".notify.lock").symlink_to(target)

    with pytest.raises(UnsafeLockPathError):
        with database_maintenance_lock(database):
            pass

    assert target.read_bytes() == b"unchanged"
