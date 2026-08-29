"""Ordered, fail-closed schema inspection and migrations."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from careeros.database.schema import SCHEMA_VERSION

MIGRATIONS: dict[int, object] = {}


def inspect_schema(connection: sqlite3.Connection) -> str:
    """Classify a database without creating or changing anything."""
    row = (
        connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()
        if _table_exists(connection, "schema_metadata")
        else None
    )
    if row is None:
        tables = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        return "empty" if not tables else "malformed"
    try:
        version = int(row[0])
    except (TypeError, ValueError):
        return "malformed"
    if version == SCHEMA_VERSION:
        return "current" if _valid_structure(connection, 9) else "malformed"
    if version < 8:
        return "unsupported"
    if version > SCHEMA_VERSION:
        return "future"
    if version == 8:
        if not _valid_structure(connection, 8):
            return "malformed"
    return "upgrade_required"


def inspect_database(path: Path) -> str:
    """Inspect a database path without creating it or mutating its schema."""
    if not path.exists():
        return "absent"
    if path.stat().st_size == 0:
        return "empty"
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as connection:
            return inspect_schema(connection)
    except sqlite3.DatabaseError:
        return "malformed"


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _valid_structure(connection: sqlite3.Connection, version: int) -> bool:
    required: dict[str, set[str]] = {
        "jobs": {"id", "source", "title", "collected_at"},
        "applications": {"id", "job_id", "current_status", "created_at", "updated_at"},
        "application_events": {
            "id",
            "application_id",
            "old_status",
            "new_status",
            "created_at",
        },
        "reminders": {"id", "job_id", "kind", "due_at"},
        "review_decisions": {"id", "job_id", "decision", "created_at", "updated_at"},
    }
    if version == 9:
        required["application_events"].add("note_redacted_at")
        required["application_preparations"] = {
            "application_id",
            "revision",
            "requirements_reviewed_revision",
            "requirements_reviewed_at",
            "ready_confirmed_revision",
            "ready_confirmed_at",
            "created_at",
            "updated_at",
        }
        required["application_requirement_items"] = {
            "id",
            "application_id",
            "kind",
            "label",
            "detail",
            "required",
            "fulfillment_state",
            "deadline_at",
            "sort_order",
            "origin",
            "capture_method",
            "confirmed",
            "created_at",
            "updated_at",
        }
    for table, columns in required.items():
        if not _table_exists(connection, table):
            return False
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns <= actual:
            return False
    if version == 9:
        if not _valid_v9_columns(connection) or not _valid_v9_sql(connection):
            return False
        if not _valid_v9_foreign_keys(connection):
            return False
        if not _valid_v9_indexes(connection):
            return False
    return True


def _valid_v9_columns(connection: sqlite3.Connection) -> bool:
    """Validate column affinity and constraint flags for the preparation tables."""
    expected = {
        "application_preparations": {
            "application_id": ("INTEGER", 0, 1),
            "revision": ("INTEGER", 1, 0),
            "requirements_reviewed_revision": ("INTEGER", 0, 0),
            "requirements_reviewed_at": ("TEXT", 0, 0),
            "ready_confirmed_revision": ("INTEGER", 0, 0),
            "ready_confirmed_at": ("TEXT", 0, 0),
            "created_at": ("TEXT", 1, 0),
            "updated_at": ("TEXT", 1, 0),
        },
        "application_requirement_items": {
            "id": ("TEXT", 0, 1),
            "application_id": ("INTEGER", 1, 0),
            "kind": ("TEXT", 1, 0),
            "label": ("TEXT", 1, 0),
            "detail": ("TEXT", 0, 0),
            "required": ("INTEGER", 1, 0),
            "fulfillment_state": ("TEXT", 1, 0),
            "deadline_at": ("TEXT", 0, 0),
            "sort_order": ("INTEGER", 1, 0),
            "origin": ("TEXT", 1, 0),
            "capture_method": ("TEXT", 1, 0),
            "confirmed": ("INTEGER", 1, 0),
            "created_at": ("TEXT", 1, 0),
            "updated_at": ("TEXT", 1, 0),
        },
    }
    for table, table_columns in expected.items():
        actual = {
            row[1]: (str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if any(
            actual.get(column) != properties
            for column, properties in table_columns.items()
        ):
            return False

    event_column = next(
        (
            row
            for row in connection.execute("PRAGMA table_info(application_events)")
            if row[1] == "note_redacted_at"
        ),
        None,
    )
    return (
        event_column is not None
        and str(event_column[2]).upper() == "TEXT"
        and not event_column[3]
        and not event_column[5]
    )


def _normalized_sql(
    connection: sqlite3.Connection, object_type: str, name: str
) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    normalized = re.sub(r"\s+", " ", str(row[0]).strip()).lower()
    normalized = re.sub(r"\s*,\s*", ",", normalized)
    return re.sub(r"\s*=\s*", "=", normalized)


def _valid_v9_sql(connection: sqlite3.Connection) -> bool:
    preparation_sql = _normalized_sql(connection, "table", "application_preparations")
    items_sql = _normalized_sql(connection, "table", "application_requirement_items")
    if preparation_sql is None or items_sql is None:
        return False
    preparation_fragments = (
        "application_id integer primary key references applications(id) "
        "on delete cascade",
        "revision integer not null default 0 check (revision >=0)",
    )
    item_fragments = (
        "application_id integer not null references applications(id) on delete cascade",  # noqa: E501
        "kind text not null check (kind in "
        "('material','link','question','task','deadline','other'))",
        "label text not null check (length(label) between 1 and 160)",
        "detail text check (detail is null or length(detail) between 1 and 1000)",
        "required integer not null check (required in (0,1))",
        "fulfillment_state text not null check (fulfillment_state in "
        "('open','satisfied','not_applicable'))",
        "sort_order integer not null check (sort_order >=0)",
        "origin text not null check (origin='manual')",
        "capture_method text not null check (capture_method='manual')",
        "confirmed integer not null check (confirmed in (0,1))",
        "check (kind !='deadline' or fulfillment_state !='satisfied')",
        "unique(application_id,sort_order)",
    )
    return all(
        fragment in preparation_sql for fragment in preparation_fragments
    ) and all(fragment in items_sql for fragment in item_fragments)


def _valid_v9_foreign_keys(connection: sqlite3.Connection) -> bool:
    for table, source_column in (
        ("application_preparations", "application_id"),
        ("application_requirement_items", "application_id"),
    ):
        matches = [
            row
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            if row[2] == "applications" and row[3] == source_column and row[4] == "id"
        ]
        if len(matches) != 1 or matches[0][6].upper() != "CASCADE":
            return False
    return True


def _valid_v9_indexes(connection: sqlite3.Connection) -> bool:
    indexes = {
        row[1]: row
        for row in connection.execute(
            "PRAGMA index_list(application_requirement_items)"
        )
    }
    index = indexes.get("idx_requirement_items_application")
    if index is None or int(index[2]) != 0 or index[3] != "c" or int(index[4]) != 0:
        return False
    columns = [
        row[2]
        for row in connection.execute(
            "PRAGMA index_info(idx_requirement_items_application)"
        )
    ]
    if columns != ["application_id", "sort_order"]:
        return False
    index_sql = _normalized_sql(
        connection, "index", "idx_requirement_items_application"
    )
    if index_sql != (
        "create index idx_requirement_items_application on "
        "application_requirement_items(application_id,sort_order)"
    ):
        return False

    unique_indexes = []
    for row in connection.execute("PRAGMA index_list(application_requirement_items)"):
        if int(row[2]) == 1:
            unique_indexes.append(
                [info[2] for info in connection.execute(f"PRAGMA index_info({row[1]})")]
            )
    return ["application_id", "sort_order"] in unique_indexes


def migrate_8_to_9(connection: sqlite3.Connection) -> None:
    """Upgrade a structurally valid v8 database in one transaction."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(application_events)")
        }
        if "note_redacted_at" not in columns:
            connection.execute(
                "ALTER TABLE application_events ADD COLUMN note_redacted_at TEXT"
            )
        statements = [
            """CREATE TABLE application_preparations (
                application_id INTEGER PRIMARY KEY REFERENCES applications(id)
                    ON DELETE CASCADE,
                revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
                requirements_reviewed_revision INTEGER,
                requirements_reviewed_at TEXT,
                ready_confirmed_revision INTEGER,
                ready_confirmed_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE application_requirement_items (
                id TEXT PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id)
                    ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK (kind IN
                    ('material','link','question','task','deadline','other')),
                label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 160),
                detail TEXT CHECK (detail IS NULL OR length(detail) BETWEEN 1 AND 1000),
                required INTEGER NOT NULL CHECK (required IN (0,1)),
                fulfillment_state TEXT NOT NULL CHECK (fulfillment_state IN
                    ('open','satisfied','not_applicable')),
                deadline_at TEXT, sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
                origin TEXT NOT NULL CHECK (origin='manual'), capture_method TEXT
                    NOT NULL
                    CHECK (capture_method='manual'),
                confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                CHECK (kind != 'deadline' OR fulfillment_state != 'satisfied'),
                UNIQUE(application_id, sort_order)
            )""",
            "CREATE INDEX idx_requirement_items_application ON "
            "application_requirement_items(application_id, sort_order)",
        ]
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "UPDATE schema_metadata SET value='9' WHERE key='schema_version'"
        )
        if inspect_schema(connection) != "current":
            raise ValueError("migration produced invalid schema")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


MIGRATIONS[8] = migrate_8_to_9


def migrate(connection: sqlite3.Connection) -> str:
    state = inspect_schema(connection)
    if state == "upgrade_required":
        migrate_8_to_9(connection)
        return "current"
    if state in {"future", "malformed", "unsupported"}:
        raise ValueError(f"unsupported database schema ({state})")
    return state
