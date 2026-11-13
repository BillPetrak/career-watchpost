"""Safe output handling for deterministic exports."""

from __future__ import annotations

import os
import stat
import sys
import uuid
from pathlib import Path
from typing import TextIO

from careeros.export.models import ExportError

_DEFAULT_ENCODING = "utf-8"


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_parent(path: Path) -> tuple[int, str]:
    absolute = Path(os.path.abspath(path))
    fd = os.open(os.sep, _directory_flags() | getattr(os, "O_CLOEXEC", 0))
    try:
        for component in absolute.parts[1:-1]:
            info = os.stat(component, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ExportError("output parent must contain directories")
            next_fd = os.open(
                component, _directory_flags() | getattr(os, "O_CLOEXEC", 0), dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
        return fd, absolute.name
    except Exception:
        os.close(fd)
        raise


def _validate_parent(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ExportError("output parent must be an owned directory")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise ExportError("output parent must not be group- or world-writable")


def _entry(fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_target(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ExportError("output target must be a regular file")
    if info.st_uid != os.geteuid():
        raise ExportError("output target must be owned by the effective user")
    if info.st_nlink != 1:
        raise ExportError("output target must have exactly one link")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ExportError("output target must have mode 0600")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _write_temp(parent_fd: int, name: str, text: str) -> str:
    temporary = f".careeros-export-{uuid.uuid4().hex}.tmp"
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
        with os.fdopen(fd, "w", encoding=_DEFAULT_ENCODING, newline="") as stream:
            fd = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_target(os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    return temporary


def write_file_export(path: Path, text: str, *, overwrite: bool) -> int:
    """Write complete text to a private same-directory temporary, then publish."""
    parent_fd: int | None = None
    temporary: str | None = None
    try:
        try:
            parent_fd, name = _open_parent(path)
        except FileNotFoundError as error:
            raise ExportError(
                f"Output directory does not exist: {path.parent}."
            ) from error
        _validate_parent(parent_fd)
        existing = _entry(parent_fd, name)
        if existing is not None:
            _validate_target(existing)
            if not overwrite:
                raise ExportError(
                    f"Output file already exists: {path} (use --overwrite to "
                    "replace it)."
                )
        temporary = _write_temp(parent_fd, name, text)
        current = _entry(parent_fd, name)
        # Make the parent directory durable before publication.  Any failure
        # here leaves the old destination untouched and the temp is cleaned.
        os.fsync(parent_fd)
        if existing is None:
            if current is not None:
                raise ExportError("output target appeared during creation")
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise ExportError("output target appeared during creation") from error
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError as error:
                linked = _entry(parent_fd, name)
                temp_info = _entry(parent_fd, temporary)
                if (
                    temp_info is not None
                    and linked is not None
                    and _identity(linked) == _identity(temp_info)
                ):
                    # The unlink did not take effect: roll back the newly
                    # linked destination rather than leaking a hardlink.
                    try:
                        os.unlink(name, dir_fd=parent_fd)
                    except OSError:
                        pass
                    raise error
                if linked is None or temp_info is not None:
                    raise error
            temporary = None
        else:
            if current is None or _identity(existing) != _identity(current):
                raise ExportError("output target changed during replacement")
            temp_info = _entry(parent_fd, temporary)
            try:
                os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            except OSError as error:
                current_after = _entry(parent_fd, name)
                temp_after = _entry(parent_fd, temporary)
                published = False
                if (
                    temp_info is not None
                    and temp_after is None
                    and current_after is not None
                ):
                    try:
                        _validate_target(current_after)
                    except ExportError:
                        pass
                    else:
                        published = _same_inode(temp_info, current_after)
                if not published:
                    raise error
            temporary = None
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        return 0
    except ExportError:
        raise
    except OSError as error:
        raise ExportError(f"Failed to write output file {path}: {error}") from error
    finally:
        if parent_fd is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except OSError:
                    pass
            os.close(parent_fd)


def write_export(text: str, stream: TextIO) -> int:
    """Write serialized export text to a stream and return the exit code.

    A broken pipe returns 0 (not an error); a real write failure raises
    :class:`ExportError`.
    """
    try:
        stream.write(text)
        stream.flush()
    except BrokenPipeError:
        return 0
    except OSError as error:
        raise ExportError(f"Failed to write export: {error}") from error
    return 0


def stdout_stream() -> TextIO:
    """Return the stdout stream (helper keeps broken-pipe policy in one place)."""
    return sys.stdout


__all__: tuple[str, ...] = ("stdout_stream", "write_export", "write_file_export")
