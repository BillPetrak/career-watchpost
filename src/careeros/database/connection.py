"""SQLite connection helpers for CareerOS."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DATABASE_PATH = Path("data/database/careeros.sqlite3")


def connect(database_path: Path = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if not database_path.exists():
        try:
            fd = os.open(database_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def connect_read_only(database_path: Path) -> sqlite3.Connection:
    """Open an existing database read-only without creating files/directories."""
    if not database_path.exists():
        raise FileNotFoundError(database_path)
    if not database_path.is_file():
        raise IsADirectoryError(database_path)
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
