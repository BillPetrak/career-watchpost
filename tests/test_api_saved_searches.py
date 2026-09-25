"""Tests for the CareerOS saved-search endpoints (eleventh slice).

Covers ``GET /saved-searches`` and ``GET /saved-searches/{name}``: envelope
and item field order, deterministic case-insensitive name ordering,
pagination, database-availability behavior, and the privacy guarantee that
``profile_path`` (a local filesystem path) and ``description`` (unused free
text) never appear in any HTTP response. Execution of saved searches is out of
scope for these endpoints and is never exercised here.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.database.connection import connect
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search, list_searches
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

# Distinctive sentinels proving actual non-leakage of private fields: if either
# string ever appears in response text, the API leaked a private field value.
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/sentinel-candidate-profile.yaml"
_SENTINEL_DESCRIPTION = "SENTINEL-DESCRIPTION-7f3a2b9c"

_ITEM_FIELDS = [
    "id",
    "name",
    "mode",
    "query",
    "grouped",
    "source",
    "remote",
    "location",
    "untracked",
    "include_statuses",
    "exclude_statuses",
    "min_score",
    "result_limit",
    "created_at",
    "updated_at",
]


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _seed(path: Path) -> dict[str, int]:
    """Seed three saved searches (search and rank modes) with fixed times.

    The rank-mode definition carries a distinctive ``profile_path`` and
    ``description`` so the privacy tests can prove they never leak. Returns a
    mapping of stored name to saved-search id.
    """
    connection = connect(path)
    initialize_schema(connection)
    definitions = [
        SavedSearchFields(
            name="alpha",
            query="engineer",
            mode="search",
            untracked=True,
            result_limit=5,
        ),
        SavedSearchFields(
            name="Beta",
            query="python",
            mode="search",
            grouped=True,
            source="arbeitnow",
            remote=True,
            location="Berlin",
            include_statuses=("interview", "applied"),
            result_limit=10,
        ),
        SavedSearchFields(
            name="Top-Ranks",
            query="senior engineer",
            mode="rank",
            profile_path=_SENTINEL_PROFILE_PATH,
            description=_SENTINEL_DESCRIPTION,
            grouped=True,
            untracked=True,
            min_score=30,
            result_limit=15,
        ),
    ]
    searches = [
        create_search(connection, fields=definition, now=_EPOCH)
        for definition in definitions
    ]
    connection.close()
    return {search.name: search.id for search in searches}


# --------------------------------------------------------------------------- #
# List: shape and ordering
# --------------------------------------------------------------------------- #


def test_list_returns_all_saved_searches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert [item["id"] for item in payload["items"]] == [
        search_ids["alpha"],
        search_ids["Beta"],
        search_ids["Top-Ranks"],
    ]


def test_list_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload.keys()) == ["items", "total", "offset", "limit"]
    assert list(payload["items"][0].keys()) == _ITEM_FIELDS


def test_list_deterministic_case_insensitive_name_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches")

    assert response.status_code == 200
    names = [item["name"] for item in response.json()["items"]]
    assert names == ["alpha", "Beta", "Top-Ranks"]

    connection = connect(path)
    initialize_schema(connection)
    expected = [search.name for search in list_searches(connection)]
    connection.close()
    assert names == expected


def test_list_pagination_and_total_before_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.get("/saved-searches", params={"limit": 2})
        second = client.get("/saved-searches", params={"offset": 1, "limit": 2})
        beyond = client.get("/saved-searches", params={"offset": 10})

    assert first.status_code == 200
    assert first.json()["total"] == 3
    assert [item["name"] for item in first.json()["items"]] == ["alpha", "Beta"]

    assert second.status_code == 200
    assert second.json()["total"] == 3
    assert [item["name"] for item in second.json()["items"]] == ["Beta", "Top-Ranks"]

    assert beyond.status_code == 200
    assert beyond.json()["total"] == 3
    assert beyond.json()["items"] == []


def test_list_invalid_pagination_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        negative_offset = client.get("/saved-searches", params={"offset": -1})
        zero_limit = client.get("/saved-searches", params={"limit": 0})
        over_limit = client.get("/saved-searches", params={"limit": 101})

    assert negative_offset.status_code == 422
    assert zero_limit.status_code == 422
    assert over_limit.status_code == 422


def test_list_empty_initialized_database_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #


def test_detail_returns_one_saved_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches/alpha")

    assert response.status_code == 200
    item = response.json()
    assert list(item.keys()) == _ITEM_FIELDS
    assert item["name"] == "alpha"
    assert item["mode"] == "search"
    assert item["query"] == "engineer"
    assert item["grouped"] is False
    assert item["source"] is None
    assert item["remote"] is None
    assert item["location"] is None
    assert item["untracked"] is True
    assert item["include_statuses"] == []
    assert item["exclude_statuses"] == []
    assert item["min_score"] is None
    assert item["result_limit"] == 5
    assert item["created_at"] == "2026-08-03T12:00:00+00:00"
    assert item["updated_at"] == "2026-08-03T12:00:00+00:00"


def test_detail_case_insensitive_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        mixed_case = client.get("/saved-searches/BETA")
        upper = client.get("/saved-searches/TOP-RANKS")

    assert mixed_case.status_code == 200
    assert mixed_case.json()["name"] == "Beta"
    assert upper.status_code == 200
    assert upper.json()["name"] == "Top-Ranks"


def test_detail_unknown_name_is_404_with_canonical_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "No saved search named 'does-not-exist'."}


# --------------------------------------------------------------------------- #
# Field semantics
# --------------------------------------------------------------------------- #


def test_field_semantics_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches")

    assert response.status_code == 200
    by_name = {item["name"]: item for item in response.json()["items"]}

    alpha = by_name["alpha"]
    assert alpha["mode"] == "search"
    assert alpha["grouped"] is False
    assert alpha["source"] is None
    assert alpha["remote"] is None
    assert alpha["location"] is None
    assert alpha["untracked"] is True
    assert alpha["include_statuses"] == []
    assert alpha["exclude_statuses"] == []
    assert alpha["min_score"] is None
    assert alpha["result_limit"] == 5

    beta = by_name["Beta"]
    assert beta["mode"] == "search"
    assert beta["grouped"] is True
    assert beta["source"] == "arbeitnow"
    assert beta["remote"] is True
    assert beta["location"] == "Berlin"
    assert beta["untracked"] is False
    assert beta["include_statuses"] == ["interview", "applied"]
    assert beta["exclude_statuses"] == []
    assert beta["min_score"] is None
    assert beta["result_limit"] == 10

    ranks = by_name["Top-Ranks"]
    assert ranks["mode"] == "rank"
    assert ranks["grouped"] is True
    assert ranks["source"] is None
    assert ranks["remote"] is None
    assert ranks["location"] is None
    assert ranks["untracked"] is True
    assert ranks["include_statuses"] == []
    assert ranks["exclude_statuses"] == []
    assert ranks["min_score"] == 30
    assert ranks["result_limit"] == 15


# --------------------------------------------------------------------------- #
# Privacy: profile_path and description never leak
# --------------------------------------------------------------------------- #


def test_profile_path_absent_from_list_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches")

    assert response.status_code == 200
    assert "profile_path" not in response.text
    assert _SENTINEL_PROFILE_PATH not in response.text
    for item in response.json()["items"]:
        assert "profile_path" not in item


def test_profile_path_absent_from_detail_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches/Top-Ranks")

    assert response.status_code == 200
    assert "profile_path" not in response.text
    assert _SENTINEL_PROFILE_PATH not in response.text
    assert "profile_path" not in response.json()


def test_description_absent_from_list_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches")

    assert response.status_code == 200
    assert "description" not in response.text
    assert _SENTINEL_DESCRIPTION not in response.text
    for item in response.json()["items"]:
        assert "description" not in item


def test_description_absent_from_detail_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/saved-searches/Top-Ranks")

    assert response.status_code == 200
    assert "description" not in response.text
    assert _SENTINEL_DESCRIPTION not in response.text
    assert "description" not in response.json()


def test_private_sentinel_strings_never_appear_in_any_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        list_response = client.get("/saved-searches")
        detail_response = client.get("/saved-searches/Top-Ranks")

    for response in (list_response, detail_response):
        assert response.status_code == 200
        assert _SENTINEL_PROFILE_PATH not in response.text
        assert _SENTINEL_DESCRIPTION not in response.text


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_list_missing_database_refuses_startup_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v13") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v13 database"
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_detail_missing_database_refuses_startup_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v13") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v13 database"
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_list_uninitialized_database_refuses_startup_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v13") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v13 database"
    assert database_path.read_bytes() == before == b""


def test_detail_uninitialized_database_refuses_startup_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v13") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v13 database"
    assert database_path.read_bytes() == before == b""


def test_list_malformed_database_refuses_startup_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v13") as exc_info:
        with TestClient(app):
            pass

    message = str(exc_info.value)
    assert message == "CareerOS requires a current schema v13 database"
    assert database_path.read_bytes() == before
    assert str(database_path) not in message
    assert "not a database" not in message


def test_detail_malformed_database_refuses_startup_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v13") as exc_info:
        with TestClient(app):
            pass

    message = str(exc_info.value)
    assert message == "CareerOS requires a current schema v13 database"
    assert database_path.read_bytes() == before
    assert str(database_path) not in message
    assert "not a database" not in message


# --------------------------------------------------------------------------- #
# Prior endpoint regression
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        reminders = client.get("/reminders")

    assert health.status_code == 200
    assert health.json()["schema_version"] == SCHEMA_VERSION
    assert jobs.status_code == 200
    assert jobs.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert reminders.status_code == 200
    assert reminders.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# No CLI import
# --------------------------------------------------------------------------- #


def test_api_does_not_import_careeros_cli() -> None:
    api_root = _SRC / "careeros" / "api"
    violations: list[str] = []
    for path in sorted(api_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "careeros.cli" or name.startswith("careeros.cli."):
                    violations.append(f"{path.relative_to(_SRC)}: {name}")
    assert violations == []
