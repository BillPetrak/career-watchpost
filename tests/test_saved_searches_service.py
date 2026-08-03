"""Tests for the saved-search workflow service."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.saved_searches.models import (
    DuplicateNameError,
    SavedSearchFields,
    SavedSearchPatch,
    UnknownSearchError,
    ValidationError,
)
from careeros.saved_searches.service import (
    create_search,
    delete_search,
    get_search,
    list_searches,
    rename_search,
    update_search,
)

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _connect(tmp_path: Path):
    connection = connect(tmp_path / "careeros.sqlite3")
    initialize_schema(connection)
    return connection


def _fields(**overrides) -> SavedSearchFields:
    defaults = dict(
        name="engineer",
        query="Engineer",
        mode="search",
        result_limit=10,
    )
    defaults.update(overrides)
    return SavedSearchFields(**defaults)


def test_create_and_get_roundtrip(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        created = create_search(connection, fields=_fields())
        assert created.id >= 1
        assert created.name == "engineer"
        assert created.query == "Engineer"
        assert created.mode == "search"
        assert created.created_at.endswith("+00:00")

        fetched = get_search(connection, "ENGINEER")
        assert fetched == created


def test_create_duplicate_case_insensitive_rejected(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields(name="Engineer"))
        with pytest.raises(DuplicateNameError):
            create_search(connection, fields=_fields(name="engineer"))


def test_create_invalid_fields_rejected(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        with pytest.raises(ValidationError):
            create_search(connection, fields=_fields(query="   "))
        with pytest.raises(ValidationError):
            create_search(connection, fields=_fields(mode="wat"))
        with pytest.raises(ValidationError):
            create_search(connection, fields=_fields(result_limit=0))
        with pytest.raises(ValidationError):
            create_search(connection, fields=_fields(name="  "))


def test_rank_requires_profile_search_rejects_profile(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        with pytest.raises(ValidationError):
            create_search(connection, fields=_fields(mode="rank"))
        with pytest.raises(ValidationError):
            create_search(
                connection,
                fields=_fields(mode="search", profile_path="/x/profile.json"),
            )
        with pytest.raises(ValidationError):
            create_search(
                connection,
                fields=_fields(mode="search", min_score=50),
            )


def test_list_is_case_insensitive_name_ordered(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields(name="Banana", mode="search"))
        create_search(
            connection,
            fields=_fields(name="apple", mode="rank", profile_path="/p"),
        )
        create_search(connection, fields=_fields(name="Cherry", mode="search"))

        names = [search.name for search in list_searches(connection)]
        assert names == ["apple", "Banana", "Cherry"]


def test_get_missing_returns_none(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        assert get_search(connection, "missing") is None


def test_update_partial_merge_retains_unset(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(
            connection,
            fields=_fields(location="Berlin", grouped=False, min_score=None),
        )
        updated, changed = update_search(
            connection,
            name="engineer",
            patch=SavedSearchPatch(grouped=True, location="Hamburg"),
        )
        assert changed is True
        assert updated.grouped is True
        assert updated.location == "Hamburg"
        assert updated.query == "Engineer"


def test_update_noop_preserves_updated_at(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields(), now=NOW)
        stored = get_search(connection, "engineer")
        assert stored is not None
        frozen_updated_at = stored.updated_at

        updated, changed = update_search(
            connection,
            name="engineer",
            patch=SavedSearchPatch(),
            now=NOW,
        )
        assert changed is False
        assert updated.updated_at == frozen_updated_at


def test_update_mode_immutable(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields())
        patch = SavedSearchPatch()
        assert not hasattr(patch, "mode")  # mode cannot be patched
        updated, _ = update_search(
            connection,
            name="engineer",
            patch=patch,
        )
        assert updated.mode == "search"


def test_update_clears_search_fields(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(
            connection,
            fields=_fields(
                source="arbeitnow",
                location="Berlin",
                remote=True,
                include_statuses=("applied",),
            ),
        )
        updated, changed = update_search(
            connection,
            name="engineer",
            patch=SavedSearchPatch(),
            clears=frozenset({"source", "location", "remote", "status_filters"}),
        )
        assert changed is True
        assert updated.source is None
        assert updated.location is None
        assert updated.remote is None
        assert updated.include_statuses == ()


def test_update_clears_rank_fields(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(
            connection,
            fields=_fields(
                mode="rank",
                profile_path="/p/profile.json",
                min_score=70,
                location="Berlin",
            ),
        )
        updated, changed = update_search(
            connection,
            name="engineer",
            patch=SavedSearchPatch(),
            clears=frozenset({"min_score", "location"}),
        )
        assert changed is True
        assert updated.min_score is None
        assert updated.location is None
        assert updated.profile_path == "/p/profile.json"


def test_update_cannot_clear_rank_profile(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(
            connection,
            fields=_fields(mode="rank", profile_path="/p/profile.json"),
        )
        with pytest.raises(ValidationError):
            update_search(
                connection,
                name="engineer",
                patch=SavedSearchPatch(),
                clears=frozenset({"profile_path"}),
            )


def test_update_unknown_clear_rejected(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields())
        with pytest.raises(ValidationError):
            update_search(
                connection,
                name="engineer",
                patch=SavedSearchPatch(),
                clears=frozenset({"not_a_target"}),
            )


def test_update_missing_raises(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        with pytest.raises(UnknownSearchError):
            update_search(connection, name="missing", patch=SavedSearchPatch())


def test_rename_case_insensitive_noop(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields(name="Engineer"))
        renamed = rename_search(connection, name="Engineer", new_name="engineer")
        assert renamed.name == "Engineer"


def test_rename_duplicate_rejected(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields(name="alpha"))
        create_search(connection, fields=_fields(name="beta"))
        with pytest.raises(DuplicateNameError):
            rename_search(connection, name="alpha", new_name="BETA")


def test_rename_and_delete_roundtrip(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        create_search(connection, fields=_fields())
        renamed = rename_search(connection, name="engineer", new_name="dev")
        assert renamed.name == "dev"

        removed = delete_search(connection, name="dev")
        assert removed.name == "dev"
        assert get_search(connection, "dev") is None


def test_delete_missing_raises(tmp_path: Path) -> None:
    with _connect(tmp_path) as connection:
        with pytest.raises(UnknownSearchError):
            delete_search(connection, name="missing")
