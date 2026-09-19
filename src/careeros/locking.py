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
import stat
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


class UnsafeLockPathError(OSError):
    """Raised when a lock companion is not a private, owned regular file."""

    def __init__(self, lock_path: Path, reason: str) -> None:
        super().__init__(f"unsafe lock companion {lock_path}: {reason}")
        self.lock_path = lock_path
        self.reason = reason


def lock_path_for(database_path: Path, suffix: str = ".lock") -> Path:
    """Return the deterministic companion lock path for a database.

    The lock path is the resolved absolute database path plus ``suffix`` (a
    default of ``.lock`` for collection and ``.notify.lock`` for notification),
    so relative and equivalent absolute paths map to the same lock and two
    different databases never share one.
    """
    return Path(str(database_path.resolve()) + suffix)


def _read_metadata(fd: int) -> dict[str, str]:
    """Read diagnostic metadata from a lock file, tolerating any damage.

    Malformed or missing metadata yields an empty mapping; conflict reporting
    must never crash on unreadable companion files. Read through the already
    validated descriptor so a pathname swap cannot redirect even this
    diagnostic read through a symlink.
    """
    try:
        if hasattr(os, "pread"):
            payload = os.pread(fd, 64 * 1024, 0)
        else:  # pragma: no cover - supported POSIX platforms expose pread
            position = os.lseek(fd, 0, os.SEEK_CUR)
            os.lseek(fd, 0, os.SEEK_SET)
            payload = os.read(fd, 64 * 1024)
            os.lseek(fd, position, os.SEEK_SET)
        parsed = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
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


@contextmanager
def database_maintenance_lock(
    database_path: Path, *, command: str = "careeros maintenance"
) -> Iterator[None]:
    """Acquire collection then notification locks atomically, without blocking."""
    with collection_lock(database_path, command=command):
        try:
            with notification_lock(database_path, command=command):
                yield
        except NotificationLockConflict:
            raise


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

    base_flags = os.O_RDWR
    base_flags |= getattr(os, "O_CLOEXEC", 0)
    base_flags |= getattr(os, "O_NOFOLLOW", 0)

    created = False
    try:
        fd = os.open(lock_path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            fd = os.open(lock_path, base_flags)
        except OSError as error:
            raise UnsafeLockPathError(lock_path, _unsafe_open_reason(error)) from error

    try:
        if created:
            # ``mode`` is filtered through the caller's umask. Tightening a file
            # created by this call is safe; pre-existing files are never repaired.
            os.fchmod(fd, 0o600)
        _validate_lock_descriptor(fd, lock_path)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise conflict_cls(resolved, lock_path, _read_metadata(fd)) from error
            raise
        _write_metadata(fd, command=command, database_path=resolved)
        try:
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _unsafe_open_reason(error: OSError) -> str:
    """Return a stable explanation for a fail-closed companion open."""
    if error.errno == errno.ELOOP:
        return "symbolic links are not allowed"
    if error.errno in (errno.EISDIR, errno.ENXIO):
        return "path is not a regular file"
    return f"cannot open safely ({error.strerror or type(error).__name__})"


def _validate_lock_descriptor(fd: int, lock_path: Path) -> None:
    """Validate the opened object itself before locking or writing metadata."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeLockPathError(lock_path, "path is not a regular file")
    if info.st_nlink != 1:
        raise UnsafeLockPathError(lock_path, "hardlinked files are not allowed")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise UnsafeLockPathError(lock_path, "file is not owned by the current user")
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o600:
        raise UnsafeLockPathError(
            lock_path,
            f"mode must be 0600 (found {mode:04o})",
        )
