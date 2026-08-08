"""FastAPI dependencies for the CareerOS API."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from careeros.locking import CollectionLockConflict, collection_lock
from careeros.settings import database_path

DATABASE_UNAVAILABLE_MESSAGE = (
    "The database schema is unavailable (run 'careeros collect <source>' to "
    "initialize it)."
)

LOCK_CONTENTION_MESSAGE = "The database is temporarily busy; retry the request later."


def read_only_connection() -> Iterator[sqlite3.Connection | None]:
    """Yield a strictly read-only SQLite connection, or None when absent.

    A missing database file is treated as an empty database: no file, parent
    directory, or schema is ever created. Existing files are opened with
    SQLite's ``mode=ro`` URI so even a misbehaving query cannot write.
    """
    path = database_path()
    if not path.is_file():
        yield None
        return
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _collection_lock_or_503(path: Path) -> Iterator[None]:
    """Acquire the per-database collection lock, mapping contention to 503.

    The lock is the same cross-process ``flock`` used by CLI/collector writers
    and is non-blocking. Contention surfaces as a fixed privacy-safe message
    that never exposes the lock path, database path, PID, or holder metadata.
    """
    try:
        with collection_lock(path, command="api write"):
            yield
    except CollectionLockConflict:
        raise HTTPException(status_code=503, detail=LOCK_CONTENTION_MESSAGE) from None


def writable_connection() -> Iterator[sqlite3.Connection]:
    """Yield a writable SQLite connection under the collection lock.

    The configured database file must already exist: a missing database is a
    privacy-safe ``500`` and nothing (database file, parent directory, schema,
    or companion lock file) is ever created. The per-database ``collection_lock``
    is acquired before the connection is opened and held until the connection is
    closed; lock contention maps to a privacy-safe ``503``. The connection is
    opened with SQLite's ``mode=rw`` URI (never created, never initialized, no
    schema DDL) and never commits or rolls back: the shared domain service owns
    the transaction.
    """
    path = database_path()
    if not path.is_file():
        raise HTTPException(status_code=500, detail=DATABASE_UNAVAILABLE_MESSAGE)
    with _collection_lock_or_503(path):
        uri = f"{path.resolve().as_uri()}?mode=rw"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()


__all__: tuple[str, ...] = (
    "DATABASE_UNAVAILABLE_MESSAGE",
    "LOCK_CONTENTION_MESSAGE",
    "read_only_connection",
    "writable_connection",
)
