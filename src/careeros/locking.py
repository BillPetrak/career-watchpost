"""Process-level advisory locking for collection commands.

Every fetching collection command holds one OS-level exclusive lock on a
companion file next to the resolved database path for the whole duration of the
command. Ownership is managed by the operating system (``flock``): the lock is
released automatically when the owning process exits, so a companion file left
behind by a crashed process never blocks a later run. File contents are
informational diagnostics only; the OS lock, never file contents, determines
ownership.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    _fcntl = None  # type: ignore[assignment]


class CollectionLockConflict(Exception):
    """Raised when another process already holds the collection lock."""

    def __init__(
        self,
        database_path: Path,
        lock_path: Path,
        metadata: dict[str, str],
    ) -> None:
        super().__init__(f"collection lock is held for {database_path}")
        self.database_path = database_path
        self.lock_path = lock_path
        self.metadata = metadata


def lock_path_for(database_path: Path) -> Path:
    """Return the deterministic companion lock path for a database.

    The lock path is the resolved absolute database path plus a ``.lock``
    suffix, so relative and equivalent absolute paths map to the same lock and
    two different databases never share one.
    """
    return Path(str(database_path.resolve()) + ".lock")


def _read_metadata(lock_path: Path) -> dict[str, str]:
    """Read diagnostic metadata from a lock file, tolerating any damage.

    Malformed or missing metadata yields an empty mapping; conflict reporting
    must never crash on unreadable companion files.
    """
    try:
        with open(lock_path, encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in parsed.items()
        if isinstance(value, (str, int, float))
    }


def _write_metadata(
    fd: int,
    *,
    command: str,
    database_path: Path,
) -> None:
    """Write informational ownership metadata into an acquired lock file."""
    metadata = {
        "pid": str(os.getpid()),
        "command": command,
        "acquired_at": datetime.now(UTC).isoformat(),
        "database_path": str(database_path),
    }
    payload = json.dumps(metadata, indent=2) + "\n"
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload.encode("utf-8"))


@contextmanager
def collection_lock(
    database_path: Path,
    *,
    command: str | None = None,
) -> Iterator[None]:
    """Hold an exclusive advisory lock for the duration of a collection.

    Acquires a non-blocking exclusive ``flock`` on the companion lock file of
    the resolved database path before yielding, and releases it on normal exit,
    on any exception, and on KeyboardInterrupt. Raises
    :class:`CollectionLockConflict` when another process already holds the
    lock. The companion file is never removed; a stale file is harmless because
    ownership is OS-managed.
    """
    if _fcntl is None:  # pragma: no cover - non-POSIX platforms
        raise RuntimeError("collection locking requires a POSIX flock platform")

    resolved = database_path.resolve()
    lock_path = lock_path_for(resolved)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if command is None:
        command = "careeros"

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise CollectionLockConflict(
                    resolved, lock_path, _read_metadata(lock_path)
                ) from error
            raise
        _write_metadata(fd, command=command, database_path=resolved)
        try:
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)
