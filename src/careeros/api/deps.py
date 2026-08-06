"""FastAPI dependencies for the CareerOS API."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from careeros.settings import database_path


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


__all__: tuple[str, ...] = ("read_only_connection",)
