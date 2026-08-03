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


class LockConflict(Exception):
    """Base class for process-level advisory lock contention.

    Carries the lock's deterministic companion path and diagnostic metadata so
    a caller can report exactly which lock is held and by whom.
    """

    lock_name: str

    def __init__(
        self,
        database_path: Path,
        lock_path: Path,
        metadata: dict[str, str],
    ) -> None:
        super().__init__(f"{self.lock_name} lock is held for {database_path}")
        self.database_path = database_path
        self.lock_path = lock_path
        self.metadata = metadata


class CollectionLockConflict(LockConflict):
    """Raised when another process already holds the collection lock."""

    lock_name = "collection"


class NotificationLockConflict(LockConflict):
    """Raised when another process already holds the notification lock.

    The notification lock exists solely to serialize reminder-notification
    delivery workers. It must never grow into a general-purpose project lock;
    other commands must keep using their own specific locks.
    """

    lock_name = "notification"


def lock_path_for(database_path: Path, suffix: str = ".lock") -> Path:
    """Return the deterministic companion lock path for a database.

    The lock path is the resolved absolute database path plus ``suffix`` (a
    default of ``.lock`` for collection and ``.notify.lock`` for notification),
    so relative and equivalent absolute paths map to the same lock and two
    different databases never share one.
    """
    return Path(str(database_path.resolve()) + suffix)


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
    yield from _lock(
        database_path,
        command=command or "careeros",
        suffix=".lock",
        conflict_cls=CollectionLockConflict,
    )


@contextmanager
def notification_lock(
    database_path: Path,
    *,
    command: str | None = None,
) -> Iterator[None]:
    """Serialize reminder-notification delivery workers for one database.

    A non-blocking exclusive ``flock`` on the ``.notify.lock`` companion file
    is held only around the short claim/finalize transactions, never while the
    external notification subprocess runs. Raises
    :class:`NotificationLockConflict` when another notification worker already
    holds the lock.

    This lock exists solely to serialize notification workers; it must never
    evolve into a general-purpose project lock.
    """
    yield from _lock(
        database_path,
        command=command or "careeros notify",
        suffix=".notify.lock",
        conflict_cls=NotificationLockConflict,
    )


def _lock(
    database_path: Path,
    *,
    command: str,
    suffix: str,
    conflict_cls: type[LockConflict],
) -> Iterator[None]:
    """Shared advisory-lock acquisition/release used by the public locks."""
    if _fcntl is None:  # pragma: no cover - non-POSIX platforms
        raise RuntimeError("locking requires a POSIX flock platform")

    resolved = database_path.resolve()
    lock_path = lock_path_for(resolved, suffix)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise conflict_cls(
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
