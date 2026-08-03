"""Saved-search workflow service.

Presentation-agnostic: performs no I/O beyond the queries it is given and never
writes to stdout or stderr. Business rules live here; raw SQL lives in
``careeros.saved_searches.database``; the CLI only parses, dispatches, and
renders.

Write semantics:

- ``create_search`` validates, rejects case-insensitive duplicate names, and
  commits one row.
- ``update_search`` merges a patch onto the stored definition (``None`` patch
  fields retain prior values), applies explicit clears, then re-validates the
  whole merged definition before writing. A no-op update succeeds without
  touching ``updated_at``. Mode is immutable after creation.
- ``rename_search`` is an atomic case-insensitive rename with duplicate-name
  protection.
- ``delete_search`` removes the named row.

Every write commits in a single transaction and rolls back on any
``sqlite3.Error``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from careeros.saved_searches.database import (
    delete_search as _delete_search,
)
from careeros.saved_searches.database import (
    get_search as _get_search,
)
from careeros.saved_searches.database import (
    insert_search as _insert_search,
)
from careeros.saved_searches.database import (
    list_searches as _list_searches,
)
from careeros.saved_searches.database import (
    rename_search as _rename_search,
)
from careeros.saved_searches.database import (
    update_search as _update_search,
)
from careeros.saved_searches.models import (
    DuplicateNameError,
    SavedSearch,
    SavedSearchFields,
    SavedSearchPatch,
    UnknownSearchError,
    ValidationError,
    normalize_name,
    validate_fields,
)

_CLEAR_TARGETS: frozenset[str] = frozenset(
    {"profile_path", "source", "remote", "location", "min_score", "status_filters"}
)

_FIELD_NAMES: tuple[str, ...] = (
    "name",
    "description",
    "query",
    "mode",
    "profile_path",
    "grouped",
    "source",
    "remote",
    "location",
    "untracked",
    "include_statuses",
    "exclude_statuses",
    "min_score",
    "result_limit",
)


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _serialize(value: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _conflict_check(
    connection: sqlite3.Connection,
    name: str,
    *,
    exclude_id: int | None = None,
) -> None:
    """Raise DuplicateNameError when ``name`` already exists."""
    existing = _get_search(connection, name)
    if existing is not None and existing.id != exclude_id:
        raise DuplicateNameError(f"A saved search named {name!r} already exists.")


def require_search(connection: sqlite3.Connection, name: str) -> SavedSearch:
    """Return a saved search by name, raising UnknownSearchError when missing."""
    search = _get_search(connection, name)
    if search is None:
        raise UnknownSearchError(f"No saved search named {name!r}.")
    return search


def create_search(
    connection: sqlite3.Connection,
    *,
    fields: SavedSearchFields,
    now: datetime | None = None,
) -> SavedSearch:
    """Create a saved search and return the stored row."""
    validate_fields(fields)
    _conflict_check(connection, fields.name)
    timestamp = _serialize(now if now is not None else _utc_now())
    try:
        _insert_search(
            connection,
            fields=fields,
            created_at=timestamp,
            updated_at=timestamp,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    stored = _get_search(connection, fields.name)
    if stored is None:
        raise RuntimeError("create_search did not persist the saved search")
    return stored


def get_search(connection: sqlite3.Connection, name: str) -> SavedSearch | None:
    """Return a saved search by name (case-insensitive), or None."""
    return _get_search(connection, name)


def list_searches(connection: sqlite3.Connection) -> list[SavedSearch]:
    """Return saved searches ordered case-insensitively by name then id."""
    return _list_searches(connection)


def update_search(
    connection: sqlite3.Connection,
    *,
    name: str,
    patch: SavedSearchPatch,
    clears: frozenset[str] = frozenset(),
    now: datetime | None = None,
) -> tuple[SavedSearch, bool]:
    """Apply a partial update and return (stored definition, changed).

    Unknown clear targets are rejected; a merged definition identical to the
    stored one is a no-op that preserves ``updated_at``.
    """
    unknown_clears = clears - _CLEAR_TARGETS
    if unknown_clears:
        raise ValidationError(
            f"Unknown clear target(s): {', '.join(sorted(unknown_clears))}."
        )

    search = require_search(connection, name)
    merged = _merge_fields(search, patch, clears)
    validate_fields(merged)

    if _fields_equal(merged, search):
        return search, False

    timestamp = _serialize(now if now is not None else _utc_now())
    try:
        _update_search(
            connection,
            search_id=search.id,
            fields=merged,
            updated_at=timestamp,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    updated = _get_search(connection, name)
    if updated is None:
        raise RuntimeError("update_search did not persist the saved search")
    return updated, True


def rename_search(
    connection: sqlite3.Connection,
    *,
    name: str,
    new_name: str,
    now: datetime | None = None,
) -> SavedSearch:
    """Rename a saved search atomically with duplicate-name protection.

    Renaming to the exact stored name (case-sensitive) is a no-op.
    """
    search = require_search(connection, name)
    normalized = normalize_name(new_name)
    _conflict_check(connection, normalized, exclude_id=search.id)

    if normalized.casefold() == search.name.casefold():
        return search

    timestamp = _serialize(now if now is not None else _utc_now())
    try:
        _rename_search(
            connection,
            search_id=search.id,
            name=normalized,
            updated_at=timestamp,
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    renamed = _get_search(connection, normalized)
    if renamed is None:
        raise RuntimeError("rename_search did not persist the saved search")
    return renamed


def delete_search(connection: sqlite3.Connection, *, name: str) -> SavedSearch:
    """Delete a saved search and return the removed definition."""
    search = require_search(connection, name)
    try:
        _delete_search(connection, search_id=search.id)
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise
    return search


def _merge_fields(
    search: SavedSearch,
    patch: SavedSearchPatch,
    clears: frozenset[str],
) -> SavedSearchFields:
    """Merge a patch and clears onto a stored definition.

    Mode is immutable: the stored mode is always retained. ``remote`` is
    normalized to ``None`` or ``True``.
    """
    untracked = patch.untracked if patch.untracked is not None else search.untracked
    include_statuses = (
        patch.include_statuses
        if patch.include_statuses is not None
        else search.include_statuses
    )
    exclude_statuses = (
        patch.exclude_statuses
        if patch.exclude_statuses is not None
        else search.exclude_statuses
    )
    if "status_filters" in clears:
        untracked = False
        include_statuses = ()
        exclude_statuses = ()

    remote = patch.remote if patch.remote is not None else search.remote
    if "remote" in clears:
        remote = None
    if remote is not True:
        remote = None

    return SavedSearchFields(
        name=search.name,
        description=(
            patch.description if patch.description is not None else search.description
        ),
        query=patch.query if patch.query is not None else search.query,
        mode=search.mode,
        profile_path=(
            None
            if "profile_path" in clears
            else patch.profile_path
            if patch.profile_path is not None
            else search.profile_path
        ),
        grouped=patch.grouped if patch.grouped is not None else search.grouped,
        source=(
            None
            if "source" in clears
            else patch.source
            if patch.source is not None
            else search.source
        ),
        remote=remote,
        location=(
            None
            if "location" in clears
            else patch.location
            if patch.location is not None
            else search.location
        ),
        untracked=untracked,
        include_statuses=include_statuses,
        exclude_statuses=exclude_statuses,
        min_score=(
            None
            if "min_score" in clears
            else patch.min_score
            if patch.min_score is not None
            else search.min_score
        ),
        result_limit=(
            patch.result_limit
            if patch.result_limit is not None
            else search.result_limit
        ),
    )


def _fields_equal(fields: SavedSearchFields, search: SavedSearch) -> bool:
    """Return True when every definition field matches the stored row."""
    return all(
        getattr(fields, field_name) == getattr(search, field_name)
        for field_name in _FIELD_NAMES
    )
