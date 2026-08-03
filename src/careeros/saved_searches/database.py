"""Saved-search persistence layer.

Stores durable search/rank definitions as normalized columns plus two JSON
columns for the genuinely repeatable application-status lists. Row-level
functions never commit on their own; the service layer owns the transaction so
a definition write is committed atomically.
"""

from __future__ import annotations

import json
import sqlite3

from careeros.saved_searches.models import SavedSearch, SavedSearchFields

_SELECT_COLUMNS = (
    "id, name, description, query, mode, profile_path, grouped, source,"
    " remote, location, untracked, include_statuses, exclude_statuses,"
    " min_score, result_limit, created_at, updated_at"
)


def _json_statuses(values: tuple[str, ...]) -> str | None:
    """Serialize status lists deterministically, or None when empty."""
    if not values:
        return None
    return json.dumps(list(values), separators=(",", ":"), sort_keys=True)


def _statuses_from_json(value: str | None) -> tuple[str, ...]:
    """Deserialize a stored status list, or an empty tuple when absent."""
    if value is None:
        return ()
    parsed = json.loads(value)
    return tuple(str(item) for item in parsed)


def _row_to_saved_search(row: sqlite3.Row) -> SavedSearch:
    """Build a SavedSearch from a saved_searches row."""
    return SavedSearch(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        query=row["query"],
        mode=row["mode"],
        profile_path=row["profile_path"],
        grouped=bool(row["grouped"]),
        source=row["source"],
        remote=bool(row["remote"]) if row["remote"] is not None else None,
        location=row["location"],
        untracked=bool(row["untracked"]),
        include_statuses=_statuses_from_json(row["include_statuses"]),
        exclude_statuses=_statuses_from_json(row["exclude_statuses"]),
        min_score=row["min_score"],
        result_limit=row["result_limit"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _remote_value(remote: bool | None) -> int | None:
    """Store remote as 1 (filter remote only) or NULL (no filter).

    ``False`` carries no meaning distinct from "no filter" in the existing
    search implementation, so it is normalized to NULL here.
    """
    return 1 if remote is True else None


def insert_search(
    connection: sqlite3.Connection,
    *,
    fields: SavedSearchFields,
    created_at: str,
    updated_at: str,
) -> int:
    """Insert a saved-search row and return its id."""
    cursor = connection.execute(
        """
        INSERT INTO saved_searches (
            name, description, query, mode, profile_path, grouped, source,
            remote, location, untracked, include_statuses, exclude_statuses,
            min_score, result_limit, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fields.name,
            fields.description,
            fields.query,
            fields.mode,
            fields.profile_path,
            int(fields.grouped),
            fields.source,
            _remote_value(fields.remote),
            fields.location,
            int(fields.untracked),
            _json_statuses(fields.include_statuses),
            _json_statuses(fields.exclude_statuses),
            fields.min_score,
            fields.result_limit,
            created_at,
            updated_at,
        ),
    )
    search_id = cursor.lastrowid
    if search_id is None:
        raise RuntimeError("insert_search did not return a row id")
    return int(search_id)


def get_search(
    connection: sqlite3.Connection,
    name: str,
) -> SavedSearch | None:
    """Return a saved search by name (case-insensitive), or None."""
    row = connection.execute(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM saved_searches
        WHERE name = ? COLLATE NOCASE
        """,
        (name,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_saved_search(row)


def list_searches(connection: sqlite3.Connection) -> list[SavedSearch]:
    """Return every saved search, ordered case-insensitively by name then id."""
    rows = connection.execute(
        f"""
        SELECT {_SELECT_COLUMNS}
        FROM saved_searches
        ORDER BY name COLLATE NOCASE, id
        """
    ).fetchall()
    return [_row_to_saved_search(row) for row in rows]


def update_search(
    connection: sqlite3.Connection,
    *,
    search_id: int,
    fields: SavedSearchFields,
    updated_at: str,
) -> None:
    """Replace a saved-search row's definition fields."""
    connection.execute(
        """
        UPDATE saved_searches
        SET name = ?, description = ?, query = ?, mode = ?, profile_path = ?,
            grouped = ?, source = ?, remote = ?, location = ?, untracked = ?,
            include_statuses = ?, exclude_statuses = ?, min_score = ?,
            result_limit = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            fields.name,
            fields.description,
            fields.query,
            fields.mode,
            fields.profile_path,
            int(fields.grouped),
            fields.source,
            _remote_value(fields.remote),
            fields.location,
            int(fields.untracked),
            _json_statuses(fields.include_statuses),
            _json_statuses(fields.exclude_statuses),
            fields.min_score,
            fields.result_limit,
            updated_at,
            search_id,
        ),
    )


def rename_search(
    connection: sqlite3.Connection,
    *,
    search_id: int,
    name: str,
    updated_at: str,
) -> None:
    """Rename a saved-search row."""
    connection.execute(
        "UPDATE saved_searches SET name = ?, updated_at = ? WHERE id = ?",
        (name, updated_at, search_id),
    )


def delete_search(
    connection: sqlite3.Connection,
    *,
    search_id: int,
) -> None:
    """Delete a saved-search row."""
    connection.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
