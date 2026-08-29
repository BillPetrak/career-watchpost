"""Safe offline SQLite administration operations."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from careeros.database.connection import connect
from careeros.database.migrations.core import inspect_schema, migrate
from careeros.locking import collection_lock, database_maintenance_lock


def _check(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("database is absent or empty")
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as c:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("database integrity check failed")
            state = inspect_schema(c)
            if state == "current":
                required = {
                    "jobs",
                    "applications",
                    "application_events",
                    "application_preparations",
                    "application_requirement_items",
                }
                tables = {
                    row[0]
                    for row in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not required <= tables:
                    raise ValueError("database schema is structurally invalid")
            return state
    except sqlite3.DatabaseError as exc:
        raise ValueError("database is malformed") from exc


def backup(source: Path, destination: Path) -> str:
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("symlink paths are not accepted")
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or destination.exists():
        raise ValueError("backup destination already exists")
    state = _check(source)
    if state not in {"current", "upgrade_required"}:
        raise ValueError("unsupported database schema")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(fd)
    try:
        with collection_lock(source, command="database backup"):
            _backup_unlocked(source, destination)
        os.chmod(destination, 0o600)
        _check(destination)
        with open(destination, "rb") as f:
            os.fsync(f.fileno())
        dir_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return str(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _backup_unlocked(source: Path, destination: Path) -> None:
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src,
        sqlite3.connect(destination) as dst,
    ):
        src.backup(dst)


def migrate_database(
    path: Path,
    *,
    backup_path: Path | None = None,
    no_backup: bool = False,
    acknowledge: bool = False,
) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("database is absent")
    with database_maintenance_lock(path, command="database migrate"):
        state = _check(path)
        if state == "upgrade_required" and no_backup and not acknowledge:
            raise ValueError("--no-backup requires acknowledgement")
        if state == "upgrade_required" and not no_backup:
            destination = backup_path or Path(str(path) + ".pre-v9.backup.sqlite3")
            if destination.is_symlink() or destination.exists():
                raise ValueError("backup destination already exists")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            try:
                _backup_unlocked(path.resolve(), destination.resolve())
                _check(destination)
                with open(destination, "rb") as file:
                    os.fsync(file.fileno())
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        with connect(path) as c:
            migrate(c)
    return "current"


def restore(backup_path: Path, target: Path) -> None:
    if backup_path.is_symlink() or target.is_symlink():
        raise ValueError("symlink paths are not accepted")
    if backup_path.resolve() == target.resolve():
        raise ValueError("backup and target must differ")
    state = _check(backup_path)
    if state not in {"current", "upgrade_required"}:
        raise ValueError("unsupported backup schema")
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with database_maintenance_lock(target, command="database restore"):
        for suffix in ("-wal", "-shm"):
            if Path(str(target) + suffix).exists():
                raise ValueError("target has unresolved SQLite sidecar files")
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.restore-", dir=target.parent)
        os.close(fd)
        os.chmod(tmp, 0o600)
        try:
            with (
                sqlite3.connect(
                    f"file:{backup_path.resolve()}?mode=ro", uri=True
                ) as src,
                sqlite3.connect(tmp) as dst,
            ):
                src.backup(dst)
            _check(Path(tmp))
            with open(tmp, "rb") as file:
                os.fsync(file.fileno())
            os.replace(tmp, target)
            os.chmod(target, 0o600)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(tmp).unlink(missing_ok=True)
