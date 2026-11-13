"""Descriptor-backed storage for the private file-backed matching profile."""

from __future__ import annotations

import errno
import os
import stat
import uuid
from pathlib import Path


class UnsafeProfileFileError(ValueError):
    """The profile or its destination is not safe for private use."""


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _validate_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeProfileFileError("profile must be a regular file")
    if info.st_uid != os.geteuid():
        raise UnsafeProfileFileError("profile must be owned by the effective user")
    if info.st_nlink != 1:
        raise UnsafeProfileFileError("profile must have exactly one link")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise UnsafeProfileFileError("profile must have mode 0600")


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_parent(path: Path, *, create: bool) -> tuple[int, str]:
    """Walk an absolute path from a stable root, never following components."""
    absolute = Path(os.path.abspath(path))
    fd = os.open(os.sep, _directory_flags() | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in absolute.parts[1:-1]:
            try:
                info = os.stat(component, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    info = os.stat(component, dir_fd=fd, follow_symlinks=False)
                else:
                    next_fd = os.open(
                        component,
                        _directory_flags() | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=fd,
                    )
                    os.fchmod(next_fd, 0o700)
                    os.close(fd)
                    fd = next_fd
                    continue
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeProfileFileError("profile parent must contain directories")
            next_fd = os.open(
                component, _directory_flags() | getattr(os, "O_CLOEXEC", 0), dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return fd, absolute.name
    except Exception:
        os.close(fd)
        raise


def _validate_write_parent(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise UnsafeProfileFileError("profile parent must be an owned directory")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise UnsafeProfileFileError(
            "profile parent must not be group- or world-writable"
        )


def _entry(fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_validated(path: Path) -> tuple[int, int, os.stat_result]:
    parent_fd, name = _open_parent(path, create=False)
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENXIO):
                raise UnsafeProfileFileError(
                    "profile must not be a symbolic link"
                ) from None
            raise UnsafeProfileFileError("profile cannot be opened safely") from error
        try:
            before = os.fstat(fd)
            _validate_file(before)
            return parent_fd, fd, before
        except Exception:
            os.close(fd)
            raise
    except Exception:
        os.close(parent_fd)
        raise


def read_profile_text(path: Path) -> str:
    """Read a profile only through validated descriptors."""
    parent_fd, fd, before = _open_validated(path)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        if _identity(before) != _identity(os.fstat(fd)):
            raise UnsafeProfileFileError("profile changed while being read")
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("profile is not valid UTF-8") from error
    finally:
        os.close(fd)
        os.close(parent_fd)


def _make_temp(parent_fd: int, name: str, text: str) -> str:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o600)
        payload = text.encode("utf-8")
        position = 0
        while position < len(payload):
            position += os.write(fd, payload[position:])
        os.fsync(fd)
        _validate_file(os.fstat(fd))
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    return temporary


def write_profile_text(path: Path, text: str, *, force: bool = False) -> None:
    """Create or safely replace a private profile atomically."""
    parent_fd, name = _open_parent(path, create=True)
    try:
        _validate_write_parent(parent_fd)
        existing = _entry(parent_fd, name)
        if existing is not None:
            _validate_file(existing)
            if not force:
                raise FileExistsError
        temporary = _make_temp(parent_fd, name, text)
        try:
            current = _entry(parent_fd, name)
            if existing is not None and (
                current is None or _identity(existing) != _identity(current)
            ):
                raise UnsafeProfileFileError(
                    "profile target changed during replacement"
                )
            # All fallible durability work happens before publication.  After
            # replace/link, only best-effort cleanup remains.
            os.fsync(parent_fd)
            if existing is None:
                if current is not None:
                    raise UnsafeProfileFileError(
                        "profile target appeared during creation"
                    )
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise UnsafeProfileFileError(
                        "profile target appeared during creation"
                    ) from None
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except OSError:
                    # Roll back the newly linked destination when cleanup
                    # fails, but only if it is still our inode.
                    linked = _entry(parent_fd, name)
                    temporary_info = _entry(parent_fd, temporary)
                    if (
                        linked is not None
                        and temporary_info is not None
                        and _identity(linked) == _identity(temporary_info)
                    ):
                        os.unlink(name, dir_fd=parent_fd)
                    raise
            else:
                os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            # Publication is already committed.  Preserve success semantics if
            # directory durability is unavailable, while making a best-effort
            # attempt to persist the directory entry.
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
    finally:
        os.close(parent_fd)
