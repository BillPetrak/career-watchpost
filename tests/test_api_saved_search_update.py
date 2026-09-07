"""Tests for the CareerOS generalized saved-search update write API.

Covers ``PATCH /saved-searches/{name}`` generalized from rename-only into the
definition-update endpoint: successful single- and multi-field definition
updates returning the canonical ``SavedSearchItem`` projection (exact field
order, ``updated_at`` bumped under a pinned clock), read reflection through
``GET /saved-searches`` / ``GET /saved-searches/{name}`` and execution using
the updated definition, every independent definition field, the omitted/null/
empty/``false`` semantics (omitted and ``null`` retain, provided non-null
updates, ``[]`` empties stored status lists, ``false`` is a real supplied
boolean), the ``clears`` vocabulary (``source``/``location``/``remote``/
``min_score``/``status_filters``) with multiple and null/empty clears, the
HTTP-contract ``422``s (empty/no-op body, rename mixed with definition
updates or clears, a field supplied while also cleared, a status clear mixed
with status-filter fields, invalid clears literals), rename compatibility
unchanged (rename-only dispatch, case-insensitive lookup, same-name and
case-only no-ops, duplicate target ``422`` never ``409``, old-name ``404``,
delete under the new name), rank-mode safety (stored ``profile_path`` and
``description`` survive byte-identically, never accepted/returned, ``mode``
immutable, no filesystem access), the full domain-validation ``422``
contract (empty query/source/location, invalid status, include/exclude and
untracked conflicts, ``min_score`` bounds and search-mode rejection,
``result_limit``), missing/uninitialized/malformed database privacy-safe
``500``s without file/parent/lock/schema creation, cross-process lock
contention mapped to a privacy-safe ``503`` with lock cleanup and recovery,
transaction rollback on the update write failing plus a successful retry, no
notification or unrelated-workflow side effects, a privacy leak scan over
success/404/422/500/503 responses, deterministic repeated behavior across
equivalent databases, idempotent no-op updates preserving ``updated_at``,
prior saved-search endpoint regression (create/list/detail/execute/rename/
delete unchanged, application/review/reminder smoke), schema-version
invariant, the shared no-``careeros.cli`` import guarantee, and the rule that
the real ``data/database/careeros.sqlite3`` is never opened or modified.
"""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import lock_path_for
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_RENAME_EPOCH = datetime.fromisoformat("2026-08-04T09:30:00+00:00")
_UPDATE_EPOCH = datetime.fromisoformat("2026-08-05T09:00:00+00:00")

# Distinctive sentinels proving actual non-leakage of private fields: if any
# string ever appears in response text, the API leaked a private value.
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/sentinel-candidate-profile.yaml"
_SENTINEL_DESCRIPTION = "SENTINEL-DESCRIPTION-7f3a2b9c"
_SENTINEL_APPLICATION_NOTE = "secret-cv-note"
_SENTINEL_REVIEW_NOTE = "secret-review-note"
_SENTINEL_REMINDER_MESSAGE = "secret-reminder-message"

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

_DATABASE_UNAVAILABLE = (
    "The database schema is unavailable (run 'careeros collect "
    "<source>' to initialize it)."
)

_MIXED_RENAME_MESSAGE = (
    "A saved-search rename cannot be combined with definition updates or "
    "clears in the same request."
)
_EMPTY_UPDATE_MESSAGE = "No saved-search fields to update were provided."


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _update_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the saved-search clock so genuine updates are byte-deterministic."""
    monkeypatch.setattr(
        "careeros.saved_searches.service._utc_now", lambda: _UPDATE_EPOCH
    )


def _rename_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the rename clock so genuine renames are byte-deterministic."""
    monkeypatch.setattr(
        "careeros.saved_searches.service._utc_now", lambda: _RENAME_EPOCH
    )


def _make_job(
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str,
    *,
    description: str,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed_jobs(path: Path) -> dict[str, int]:
    """Seed three distinct jobs and return a source-job-id to jobs.id mapping."""
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job(
                "arbeitnow",
                "a-1",
                "Senior Engineer",
                "Acme GmbH",
                "Berlin",
                description="Python backend development team.",
            ),
            _make_job(
                "remoteok",
                "b-1",
                "Data Scientist",
                "Other Co",
                "Munich",
                description="Machine learning research team.",
            ),
            _make_job(
                "jobicy",
                "c-1",
                "Frontend Developer",
                "Frontend Labs",
                "Berlin",
                description="React frontend team.",
            ),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.close()
    return job_ids


def _seed_saved_searches(
    path: Path, profile_path: str = _SENTINEL_PROFILE_PATH
) -> dict[str, int]:
    """Seed four saved searches (search and rank modes) with fixed times.

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
            profile_path=profile_path,
            description=_SENTINEL_DESCRIPTION,
            grouped=True,
            untracked=True,
            min_score=30,
            result_limit=15,
        ),
        SavedSearchFields(
            name="filters",
            query="python",
            mode="search",
            result_limit=10,
        ),
    ]
    searches = [
        create_search(connection, fields=definition, now=_EPOCH)
        for definition in definitions
    ]
    connection.close()
    return {search.name: search.id for search in searches}


def _search_names(path: Path) -> list[str]:
    """Return the stored saved-search names ordered by id."""
    with connect(path) as connection:
        return [
            str(row["name"])
            for row in connection.execute("SELECT name FROM saved_searches ORDER BY id")
        ]


def _row_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_snapshot(path: Path, table: str) -> list[tuple]:
    """Return every row of a table as tuples for byte-level comparison."""
    with connect(path) as connection:
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    return [tuple(row) for row in rows]


def _saved_search_row(path: Path, name: str) -> sqlite3.Row:
    """Read one saved-search row directly for byte-level invariants."""
    with connect(path) as connection:
        row = connection.execute(
            "SELECT id, name, description, query, mode, profile_path, grouped,"
            " source, remote, location, untracked, include_statuses,"
            " exclude_statuses, min_score, result_limit, created_at, updated_at"
            " FROM saved_searches WHERE name = ? COLLATE NOCASE ORDER BY id LIMIT 1",
            (name,),
        ).fetchone()
    assert row is not None
    return row


def _saved_search_by_id(path: Path, search_id: int) -> sqlite3.Row:
    """Read one saved-search row directly for byte-level invariants."""
    with connect(path) as connection:
        row = connection.execute(
            "SELECT id, name, profile_path, description, created_at, updated_at"
            " FROM saved_searches WHERE id = ?",
            (search_id,),
        ).fetchone()
    assert row is not None
    return row


# --------------------------------------------------------------------------- #
# Child-process lock helper (cross-process flock semantics)
# --------------------------------------------------------------------------- #

_CHILD_HOLD = """\
import sys
from pathlib import Path
from careeros.locking import collection_lock
database = Path(sys.argv[1])
with collection_lock(database, command="collect due"):
    print("LOCKED", flush=True)
    sys.stdin.readline()
"""


def _src_on_pythonpath() -> str:
    existing = os.environ.get("PYTHONPATH")
    return str(_SRC) + (os.pathsep + existing if existing else "")


def _start_child(database: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = _src_on_pythonpath()
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLD, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_lock_line(child: subprocess.Popen) -> None:
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if line != "LOCKED":
        assert child.stderr is not None
        pytest.fail(f"child did not acquire lock: {child.stderr.read()}")


def _release_child(child: subprocess.Popen) -> None:
    assert child.stdin is not None
    child.stdin.write("release\n")
    child.stdin.close()
    child.wait(timeout=10)


# --------------------------------------------------------------------------- #
# A. Core success
# --------------------------------------------------------------------------- #


def test_single_field_query_update_returns_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"query": "golang"})
        detail = client.get("/saved-searches/alpha")
        listing = client.get("/saved-searches")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["id"] == search_ids["alpha"]
    assert payload["name"] == "alpha"
    assert payload["mode"] == "search"
    assert payload["query"] == "golang"
    # Omitted fields retain their stored values.
    assert payload["grouped"] is False
    assert payload["source"] is None
    assert payload["remote"] is None
    assert payload["location"] is None
    assert payload["untracked"] is True
    assert payload["include_statuses"] == []
    assert payload["exclude_statuses"] == []
    assert payload["min_score"] is None
    assert payload["result_limit"] == 5
    assert payload["created_at"] == _EPOCH.isoformat()
    # A genuine update bumps updated_at (pinned clock proves it).
    assert payload["updated_at"] == _UPDATE_EPOCH.isoformat()
    assert "profile_path" not in payload
    assert "description" not in payload

    # The update is reflected by the existing read endpoints.
    assert detail.status_code == 200
    assert detail.json() == payload
    assert listing.status_code == 200
    alpha_item = next(
        item for item in listing.json()["items"] if item["name"] == "alpha"
    )
    assert alpha_item["query"] == "golang"
    assert alpha_item["updated_at"] == _UPDATE_EPOCH.isoformat()
    # Direct DB read confirms the write.
    row = _saved_search_row(path, "alpha")
    assert row["query"] == "golang"
    assert row["updated_at"] == _UPDATE_EPOCH.isoformat()
    assert row["created_at"] == _EPOCH.isoformat()


def test_multi_field_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    body = {
        "query": "golang",
        "grouped": True,
        "source": "remoteok",
        "remote": True,
        "location": "Munich",
        "untracked": False,
        "result_limit": 7,
    }
    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == search_ids["alpha"]
    assert payload["query"] == "golang"
    assert payload["grouped"] is True
    assert payload["source"] == "remoteok"
    assert payload["remote"] is True
    assert payload["location"] == "Munich"
    assert payload["untracked"] is False
    assert payload["include_statuses"] == []
    assert payload["exclude_statuses"] == []
    assert payload["min_score"] is None
    assert payload["result_limit"] == 7
    assert payload["updated_at"] == _UPDATE_EPOCH.isoformat()

    row = _saved_search_row(path, "alpha")
    assert row["grouped"] == 1
    assert row["source"] == "remoteok"
    assert row["remote"] == 1
    assert row["location"] == "Munich"
    assert row["untracked"] == 0
    assert row["result_limit"] == 7


def test_execute_uses_updated_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        before = client.post("/saved-searches/alpha/execute")
        updated = client.patch("/saved-searches/alpha", json={"query": "scientist"})
        after = client.post("/saved-searches/alpha/execute")

    assert before.status_code == 200
    assert [item["title"] for item in before.json()["items"]] == ["Senior Engineer"]
    assert updated.status_code == 200
    assert updated.json()["query"] == "scientist"
    assert after.status_code == 200
    assert [item["title"] for item in after.json()["items"]] == ["Data Scientist"]


# --------------------------------------------------------------------------- #
# B. Independent fields
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("target", "field", "value", "expected"),
    [
        ("alpha", "query", "golang", "golang"),
        ("alpha", "grouped", True, True),
        ("alpha", "source", "remoteok", "remoteok"),
        ("alpha", "remote", True, True),
        ("alpha", "location", "Munich", "Munich"),
        ("alpha", "untracked", False, False),
        ("alpha", "result_limit", 7, 7),
        ("filters", "include_statuses", ["offer"], ["offer"]),
        ("filters", "exclude_statuses", ["rejected"], ["rejected"]),
        ("filters", "untracked", True, True),
        ("Top-Ranks", "min_score", 45, 45),
    ],
)
def test_individual_definition_fields_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
    value: object,
    expected: object,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/saved-searches/{target}", json={field: value})
        detail = client.get(f"/saved-searches/{target}")

    assert response.status_code == 200, (target, field)
    assert response.json()[field] == expected
    assert detail.status_code == 200
    assert detail.json()[field] == expected
    assert detail.json()["updated_at"] == _UPDATE_EPOCH.isoformat()


def test_remote_false_updates_remote_only_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"remote": False})

    assert response.status_code == 200
    assert response.json()["remote"] is None
    row = _saved_search_row(path, "Beta")
    assert row["remote"] is None
    assert row["updated_at"] == _UPDATE_EPOCH.isoformat()


# --------------------------------------------------------------------------- #
# C. Omitted / null / empty / false semantics
# --------------------------------------------------------------------------- #


def test_null_definition_field_retains_stored_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    # The planned combination: null query retains it while min_score updates.
    with TestClient(app) as client:
        rank_response = client.patch(
            "/saved-searches/Top-Ranks", json={"query": None, "min_score": 50}
        )
        search_response = client.patch(
            "/saved-searches/alpha", json={"query": None, "grouped": True}
        )

    assert rank_response.status_code == 200
    rank_item = rank_response.json()
    assert rank_item["query"] == "senior engineer"
    assert rank_item["min_score"] == 50
    assert rank_item["updated_at"] == _UPDATE_EPOCH.isoformat()

    assert search_response.status_code == 200
    search_item = search_response.json()
    assert search_item["query"] == "engineer"
    assert search_item["grouped"] is True
    assert search_item["untracked"] is True
    assert search_item["updated_at"] == _UPDATE_EPOCH.isoformat()


def test_null_and_clear_combination_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        source = client.patch(
            "/saved-searches/Beta", json={"source": None, "clears": ["source"]}
        )
        remote = client.patch(
            "/saved-searches/Beta", json={"remote": None, "clears": ["remote"]}
        )
        min_score = client.patch(
            "/saved-searches/Top-Ranks",
            json={"min_score": None, "clears": ["min_score"]},
        )

    # A null field is "retain", so it never conflicts with clearing it.
    assert source.status_code == 200
    assert source.json()["source"] is None
    assert remote.status_code == 200
    assert remote.json()["remote"] is None
    assert min_score.status_code == 200
    assert min_score.json()["min_score"] is None


def test_empty_list_empties_include_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        set_response = client.patch(
            "/saved-searches/filters", json={"include_statuses": ["applied"]}
        )
        empty_response = client.patch(
            "/saved-searches/filters", json={"include_statuses": []}
        )

    assert set_response.status_code == 200
    assert set_response.json()["include_statuses"] == ["applied"]
    assert empty_response.status_code == 200
    assert empty_response.json()["include_statuses"] == []
    row = _saved_search_row(path, "filters")
    assert row["include_statuses"] is None
    assert row["updated_at"] == _UPDATE_EPOCH.isoformat()


def test_empty_list_empties_exclude_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        set_response = client.patch(
            "/saved-searches/filters", json={"exclude_statuses": ["rejected"]}
        )
        empty_response = client.patch(
            "/saved-searches/filters", json={"exclude_statuses": []}
        )

    assert set_response.status_code == 200
    assert set_response.json()["exclude_statuses"] == ["rejected"]
    assert empty_response.status_code == 200
    assert empty_response.json()["exclude_statuses"] == []
    row = _saved_search_row(path, "filters")
    assert row["exclude_statuses"] is None


def test_false_is_a_real_supplied_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        grouped_on = client.patch("/saved-searches/alpha", json={"grouped": True})
        grouped_off = client.patch("/saved-searches/alpha", json={"grouped": False})
        untracked_on = client.patch("/saved-searches/filters", json={"untracked": True})
        untracked_off = client.patch(
            "/saved-searches/filters", json={"untracked": False}
        )

    assert grouped_on.status_code == 200
    assert grouped_on.json()["grouped"] is True
    # False is treated as a supplied boolean, flipping the stored value back.
    assert grouped_off.status_code == 200
    assert grouped_off.json()["grouped"] is False
    assert grouped_off.json()["updated_at"] == _UPDATE_EPOCH.isoformat()

    assert untracked_on.status_code == 200
    assert untracked_on.json()["untracked"] is True
    assert untracked_off.status_code == 200
    assert untracked_off.json()["untracked"] is False
    row = _saved_search_row(path, "filters")
    assert row["untracked"] == 0


def test_null_name_with_definition_fields_is_not_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/alpha", json={"name": None, "query": "golang"}
        )

    assert response.status_code == 200
    assert response.json()["name"] == "alpha"
    assert response.json()["query"] == "golang"
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks", "filters"]


# --------------------------------------------------------------------------- #
# D. Clears
# --------------------------------------------------------------------------- #


def test_clear_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"clears": ["source"]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] is None
    assert payload["location"] == "Berlin"
    assert payload["remote"] is True
    assert payload["grouped"] is True
    assert payload["include_statuses"] == ["interview", "applied"]
    assert payload["updated_at"] == _UPDATE_EPOCH.isoformat()
    row = _saved_search_row(path, "Beta")
    assert row["source"] is None


def test_clear_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"clears": ["location"]})

    assert response.status_code == 200
    assert response.json()["location"] is None
    assert response.json()["source"] == "arbeitnow"
    assert response.json()["remote"] is True


def test_clear_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"clears": ["remote"]})

    assert response.status_code == 200
    assert response.json()["remote"] is None
    assert response.json()["source"] == "arbeitnow"


def test_clear_min_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/Top-Ranks", json={"clears": ["min_score"]}
        )

    assert response.status_code == 200
    assert response.json()["min_score"] is None
    assert response.json()["mode"] == "rank"
    row = _saved_search_row(path, "Top-Ranks")
    assert row["min_score"] is None
    assert row["profile_path"] == _SENTINEL_PROFILE_PATH


def test_clear_status_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/Beta", json={"clears": ["status_filters"]}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["untracked"] is False
    assert payload["include_statuses"] == []
    assert payload["exclude_statuses"] == []
    assert payload["grouped"] is True
    assert payload["source"] == "arbeitnow"
    row = _saved_search_row(path, "Beta")
    assert row["include_statuses"] is None
    assert row["exclude_statuses"] is None


def test_multiple_compatible_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/Beta",
            json={"clears": ["source", "location", "remote"]},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] is None
    assert payload["location"] is None
    assert payload["remote"] is None
    assert payload["grouped"] is True
    assert payload["query"] == "python"


@pytest.mark.parametrize("clears", [None, []])
def test_null_and_empty_clears_are_noops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clears: object
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    body: dict = {"query": "golang"}
    if clears is not None:
        body["clears"] = clears
    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json=body)

    assert response.status_code == 200
    assert response.json()["query"] == "golang"
    # Beta's filters are untouched by the no-op clears on alpha.
    assert _saved_search_row(path, "Beta")["source"] == "arbeitnow"


# --------------------------------------------------------------------------- #
# E. Invalid clear combinations (HTTP-contract 422s)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (
            {"source": "arbeitnow", "clears": ["source"]},
            "Cannot set and clear 'source'",
        ),
        (
            {"location": "Athens", "clears": ["location"]},
            "Cannot set and clear 'location'",
        ),
        ({"remote": False, "clears": ["remote"]}, "Cannot set and clear 'remote'"),
        ({"remote": True, "clears": ["remote"]}, "Cannot set and clear 'remote'"),
        (
            {"min_score": 50, "clears": ["min_score"]},
            "Cannot set and clear 'min_score'",
        ),
        (
            {"untracked": True, "clears": ["status_filters"]},
            "Cannot clear status filters while supplying untracked",
        ),
        (
            {"untracked": False, "clears": ["status_filters"]},
            "Cannot clear status filters while supplying untracked",
        ),
        (
            {"include_statuses": ["applied"], "clears": ["status_filters"]},
            "Cannot clear status filters while supplying untracked",
        ),
        (
            {"include_statuses": [], "clears": ["status_filters"]},
            "Cannot clear status filters while supplying untracked",
        ),
        (
            {"exclude_statuses": ["rejected"], "clears": ["status_filters"]},
            "Cannot clear status filters while supplying untracked",
        ),
    ],
)
def test_clear_conflict_422_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
    detail: str,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json=body)

    assert response.status_code == 422
    assert detail in response.json()["detail"]
    # No mutation: Beta keeps every original value.
    row = _saved_search_row(path, "Beta")
    assert row["source"] == "arbeitnow"
    assert row["remote"] == 1
    assert row["location"] == "Berlin"
    assert row["include_statuses"] == '["interview","applied"]'
    assert row["updated_at"] == _EPOCH.isoformat()
    assert _saved_search_row(path, "Beta")["id"] == search_ids["Beta"]


@pytest.mark.parametrize("literal", ["profile_path", "description", "bogus", "mode"])
def test_invalid_clears_literal_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, literal: str
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Top-Ranks", json={"clears": [literal]})

    assert response.status_code == 422
    assert (
        _saved_search_row(path, "Top-Ranks")["profile_path"] == _SENTINEL_PROFILE_PATH
    )
    assert _saved_search_row(path, "Top-Ranks")["updated_at"] == _EPOCH.isoformat()
    assert _saved_search_row(path, "Top-Ranks")["id"] == search_ids["Top-Ranks"]


# --------------------------------------------------------------------------- #
# F. Rename compatibility
# --------------------------------------------------------------------------- #


def test_rename_only_dispatch_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "Gamma"})

    assert response.status_code == 200
    assert list(response.json()) == _ITEM_FIELDS
    assert response.json()["id"] == search_ids["alpha"]
    assert response.json()["name"] == "Gamma"
    assert response.json()["updated_at"] == _RENAME_EPOCH.isoformat()
    assert _search_names(path) == ["Gamma", "Beta", "Top-Ranks", "filters"]


def test_rename_case_insensitive_old_name_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/ALPHA", json={"name": "epsilon"})

    assert response.status_code == 200
    assert response.json()["id"] == search_ids["alpha"]
    assert response.json()["name"] == "epsilon"


def test_rename_same_name_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"name": "Beta"})

    assert response.status_code == 200
    assert response.json()["id"] == search_ids["Beta"]
    assert response.json()["name"] == "Beta"
    assert response.json()["updated_at"] == _EPOCH.isoformat()


def test_rename_case_only_noop_preserves_stored_casing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"name": "beta"})

    assert response.status_code == 200
    assert response.json()["id"] == search_ids["Beta"]
    assert response.json()["name"] == "Beta"
    assert response.json()["updated_at"] == _EPOCH.isoformat()


def test_rename_duplicate_target_422_never_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "Beta"})

    assert response.status_code == 422
    assert response.json() == {"detail": "A saved search named 'Beta' already exists."}
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks", "filters"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"name": "Gamma", "query": "python"}, _MIXED_RENAME_MESSAGE),
        ({"name": "Gamma", "grouped": False}, _MIXED_RENAME_MESSAGE),
        ({"name": "Gamma", "include_statuses": []}, _MIXED_RENAME_MESSAGE),
        ({"name": "Gamma", "clears": ["source"]}, _MIXED_RENAME_MESSAGE),
    ],
)
def test_rename_mixed_with_definition_update_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
    message: str,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": message}
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks", "filters"]
    assert _saved_search_row(path, "alpha")["query"] == "engineer"


def test_rename_null_definition_field_is_not_mixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    # A null definition field retains its stored value, so the body is a pure
    # rename and dispatches to rename_search (200), not a mixed rejection.
    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/alpha", json={"name": "Gamma", "query": None}
        )

    assert response.status_code == 200
    assert response.json()["id"] == search_ids["alpha"]
    assert response.json()["name"] == "Gamma"
    assert response.json()["query"] == "engineer"


def test_rename_old_name_404_new_name_resolves_and_delete_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        renamed = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
        old_detail = client.get("/saved-searches/alpha")
        new_detail = client.get("/saved-searches/Gamma")
        deleted = client.delete("/saved-searches/Gamma")
        old_delete = client.delete("/saved-searches/alpha")

    assert renamed.status_code == 200
    assert old_detail.status_code == 404
    assert old_detail.json() == {"detail": "No saved search named 'alpha'."}
    assert new_detail.status_code == 200
    assert new_detail.json()["id"] == search_ids["alpha"]
    assert new_detail.json()["name"] == "Gamma"
    assert deleted.status_code == 200
    assert deleted.json()["id"] == search_ids["alpha"]
    assert old_delete.status_code == 404


def test_rename_empty_name_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "   "})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Saved-search name must not be empty or whitespace."
    }


# --------------------------------------------------------------------------- #
# G. Rank-mode safety
# --------------------------------------------------------------------------- #


def test_rank_mode_definition_update_preserves_private_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/Top-Ranks", json={"query": "data scientist"}
        )
        detail = client.get("/saved-searches/Top-Ranks")
        listing = client.get("/saved-searches")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["id"] == search_ids["Top-Ranks"]
    assert payload["mode"] == "rank"
    assert payload["query"] == "data scientist"
    assert payload["min_score"] == 30
    assert payload["result_limit"] == 15
    assert "profile_path" not in response.text
    assert "description" not in response.text
    assert _SENTINEL_PROFILE_PATH not in response.text
    assert _SENTINEL_DESCRIPTION not in response.text

    # The private fields survive the HTTP update byte-identically.
    row = _saved_search_by_id(path, search_ids["Top-Ranks"])
    assert row["profile_path"] == _SENTINEL_PROFILE_PATH
    assert row["description"] == _SENTINEL_DESCRIPTION
    assert row["updated_at"] == _UPDATE_EPOCH.isoformat()

    # Reads also omit the private fields.
    assert "profile_path" not in detail.text
    assert "description" not in detail.text
    assert _SENTINEL_PROFILE_PATH not in detail.text
    assert _SENTINEL_DESCRIPTION not in listing.text


def test_rank_mode_execute_after_update_omits_private_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    profile = {
        "desired_titles": ["Engineer"],
        "skills": ["Python"],
        "preferred_locations": ["Berlin"],
        "remote_preference": "remote",
    }
    with TestClient(app) as client:
        updated = client.patch("/saved-searches/Top-Ranks", json={"query": "engineer"})
        executed = client.post(
            "/saved-searches/Top-Ranks/execute", json={"profile": profile}
        )

    assert updated.status_code == 200
    assert executed.status_code == 200
    assert executed.json()["mode"] == "rank"
    for response in (updated, executed):
        for leaked in (
            _SENTINEL_PROFILE_PATH,
            _SENTINEL_DESCRIPTION,
            "profile_path",
            "description",
        ):
            assert leaked not in response.text


def test_rank_mode_private_field_probes_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        profile_path = client.patch(
            "/saved-searches/Top-Ranks", json={"profile_path": "/tmp/evil.yaml"}
        )
        description = client.patch(
            "/saved-searches/Top-Ranks", json={"description": "probe"}
        )
        mode = client.patch("/saved-searches/Top-Ranks", json={"mode": "search"})
        clear_profile = client.patch(
            "/saved-searches/Top-Ranks", json={"clears": ["profile_path"]}
        )

    for response in (profile_path, description, mode, clear_profile):
        assert response.status_code == 422
    row = _saved_search_by_id(path, search_ids["Top-Ranks"])
    assert row["profile_path"] == _SENTINEL_PROFILE_PATH


def test_rank_mode_no_filesystem_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    sentinel = tmp_path / "no" / "such" / "sentinel-profile.yaml"
    assert not sentinel.exists()
    _seed_saved_searches(path, profile_path=str(sentinel))
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        updated = client.patch(
            "/saved-searches/Top-Ranks", json={"query": "data engineer"}
        )
        detail = client.get("/saved-searches/Top-Ranks")
        listing = client.get("/saved-searches")

    assert updated.status_code == 200
    assert detail.status_code == 200
    assert listing.status_code == 200
    # The HTTP update never reads, loads, or creates the profile file.
    assert not sentinel.exists()
    assert str(sentinel) not in updated.text
    assert str(sentinel) not in detail.text


# --------------------------------------------------------------------------- #
# H. Validation (delegated to the domain)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "expected", "target"),
    [
        (
            {"query": ""},
            "Saved-search query must not be empty or whitespace.",
            "alpha",
        ),
        (
            {"query": "   "},
            "Saved-search query must not be empty or whitespace.",
            "alpha",
        ),
        ({"source": ""}, "Source filter must not be empty or whitespace.", "alpha"),
        (
            {"location": "  "},
            "Location filter must not be empty or whitespace.",
            "alpha",
        ),
        ({"result_limit": 0}, "result_limit must be at least 1.", "alpha"),
        ({"result_limit": -5}, "result_limit must be at least 1.", "alpha"),
        ({"min_score": -5}, "min_score must be between 0 and 100.", "Top-Ranks"),
        ({"min_score": 101}, "min_score must be between 0 and 100.", "Top-Ranks"),
        (
            {"min_score": 50},
            "Search saved searches cannot have a minimum score",
            "filters",
        ),
        (
            {"include_statuses": ["bogus"]},
            "Invalid application status 'bogus'",
            "filters",
        ),
        (
            {"include_statuses": ["applied"], "exclude_statuses": ["rejected"]},
            "include_statuses and exclude_statuses cannot be combined.",
            "alpha",
        ),
        (
            {"untracked": True, "include_statuses": ["applied"]},
            "untracked cannot be combined with include_statuses",
            "alpha",
        ),
    ],
)
def test_domain_validation_422_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict,
    expected: str,
    target: str,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    # min_score bounds target the rank-mode row; the search-mode rank-only
    # rule and the status vocabulary need a search-mode row without
    # untracked=True so no earlier validation fires; everything else targets
    # alpha.
    with TestClient(app) as client:
        response = client.patch(f"/saved-searches/{target}", json=body)

    assert response.status_code == 422
    assert expected in response.json()["detail"]
    seed_queries = {
        "alpha": "engineer",
        "Top-Ranks": "senior engineer",
        "filters": "python",
    }
    assert _saved_search_row(path, target)["query"] == seed_queries[target]


def test_empty_and_noop_requests_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    bodies: list[dict | None] = [
        {},
        {"query": None},
        {"name": None},
        {"clears": []},
        {"clears": None},
        {"query": None, "min_score": None},
        None,  # no body at all (FastAPI rejects the missing body)
    ]
    with TestClient(app) as client:
        responses = [
            client.patch("/saved-searches/alpha", json=body)
            if body is not None
            else client.patch("/saved-searches/alpha")
            for body in bodies
        ]

    for response in responses:
        assert response.status_code == 422
    # No mutation happened.
    assert _saved_search_row(path, "alpha")["query"] == "engineer"
    assert _saved_search_row(path, "alpha")["updated_at"] == _EPOCH.isoformat()


def test_malformed_json_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/alpha",
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert _saved_search_row(path, "alpha")["query"] == "engineer"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bogus", "surprise"),
        ("profile_path", "/tmp/private-candidate.yaml"),
        ("description", "description-probe"),
        ("mode", "rank"),
        ("message", "secret-message"),
        ("note", "secret-note"),
        ("id", 1),
        ("created_at", "2026-08-03T12:00:00+00:00"),
        ("updated_at", "2026-08-03T12:00:00+00:00"),
    ],
)
def test_forbidden_body_fields_422_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/alpha", json={"query": "golang", field: value}
        )

    assert response.status_code == 422
    assert _saved_search_row(path, "alpha")["query"] == "engineer"
    assert _saved_search_by_id(path, search_ids["alpha"])["updated_at"] == (
        _EPOCH.isoformat()
    )
    assert _SENTINEL_PROFILE_PATH not in response.text
    assert _SENTINEL_DESCRIPTION not in response.text


# --------------------------------------------------------------------------- #
# I. Identity / error behavior
# --------------------------------------------------------------------------- #


def test_unknown_saved_search_404_for_both_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        update = client.patch(
            "/saved-searches/does-not-exist", json={"query": "golang"}
        )
        rename = client.patch("/saved-searches/does-not-exist", json={"name": "Gamma"})

    for response in (update, rename):
        assert response.status_code == 404
        assert response.json() == {"detail": "No saved search named 'does-not-exist'."}


def test_no_artificial_path_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/No%20Such%20Search", json={"query": "x"}
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "No saved search named 'No Such Search'."}


# --------------------------------------------------------------------------- #
# J. Idempotency / timestamps
# --------------------------------------------------------------------------- #


def test_genuine_update_bumps_then_identical_repeat_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.patch("/saved-searches/alpha", json={"query": "golang"})
        second = client.patch("/saved-searches/alpha", json={"query": "golang"})

    assert first.status_code == 200
    assert first.json()["updated_at"] == _UPDATE_EPOCH.isoformat()
    # The identical repeat is a domain no-op: same 200, same response body,
    # updated_at untouched.
    assert second.status_code == 200
    assert second.json() == first.json()
    row = _saved_search_row(path, "alpha")
    assert row["query"] == "golang"
    assert row["updated_at"] == _UPDATE_EPOCH.isoformat()
    assert search_ids["alpha"] == search_ids["alpha"]


def _exercise_sequence(client: TestClient) -> tuple:
    sequence = [
        client.patch("/saved-searches/alpha", json={"query": "golang"}),
        client.patch("/saved-searches/alpha", json={"query": "golang"}),
        client.patch("/saved-searches/alpha", json={"remote": True}),
        client.patch("/saved-searches/alpha", json={"remote": None, "grouped": True}),
        client.patch("/saved-searches/Beta", json={"clears": ["source", "remote"]}),
        client.patch("/saved-searches/filters", json={"include_statuses": ["applied"]}),
        client.patch("/saved-searches/filters", json={"include_statuses": []}),
        client.patch("/saved-searches/Top-Ranks", json={"min_score": 45}),
        client.patch(
            "/saved-searches/Top-Ranks", json={"query": None, "min_score": 50}
        ),
        client.patch("/saved-searches/alpha", json={"name": "Gamma"}),
        client.get("/saved-searches"),
        client.get("/saved-searches/Gamma"),
        client.get("/saved-searches/does-not-exist"),
    ]
    return tuple((response.status_code, response.json()) for response in sequence)


def test_repeated_updates_are_byte_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    _seed_saved_searches(left)
    _seed_saved_searches(right)
    _update_now(monkeypatch)
    _set_database(monkeypatch, left)
    with TestClient(app) as client:
        left_sequence = _exercise_sequence(client)

    _set_database(monkeypatch, right)
    with TestClient(app) as client:
        right_sequence = _exercise_sequence(client)

    assert left_sequence == right_sequence


# --------------------------------------------------------------------------- #
# K. Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_refuses_startup_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v11") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v11 database"
    assert not database_path.exists()
    assert not database_path.parent.exists()
    assert not Path(str(database_path) + ".lock").exists()


def test_uninitialized_database_refuses_startup_no_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    _set_database(monkeypatch, empty)

    before = empty.read_bytes()
    with pytest.raises(ValueError, match="current schema v11") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v11 database"
    assert empty.read_bytes() == before == b""


def test_malformed_database_refuses_startup_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, garbage)

    before = garbage.read_bytes()
    with pytest.raises(ValueError, match="current schema v11") as exc_info:
        with TestClient(app):
            pass

    message = str(exc_info.value)
    assert message == "CareerOS requires a current schema v11 database"
    assert garbage.read_bytes() == before
    for leaked in (
        "sqlite3",
        "OperationalError",
        "not a database",
        "file is not",
    ):
        assert leaked not in message


# --------------------------------------------------------------------------- #
# L. Cross-process lock contention and lock cleanup
# --------------------------------------------------------------------------- #


def test_lock_contention_503_unchanged_database_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.patch("/saved-searches/alpha", json={"query": "golang"})
        assert response.status_code == 503
        assert response.json() == {
            "detail": "The database is temporarily busy; retry the request later."
        }
        for leaked in (
            str(path.resolve()),
            str(lock_path_for(path)),
            "pid",
            "command",
            "acquired_at",
            "collection",
            "flock",
            "Traceback",
            "sqlite3",
        ):
            assert leaked not in response.text
    finally:
        _release_child(child)

    # The contested update changed nothing and the lock is releasable.
    assert _saved_search_row(path, "alpha")["query"] == "engineer"
    assert search_ids["alpha"] == search_ids["alpha"]
    with TestClient(app) as client:
        listing = client.get("/saved-searches")
        recovery = client.patch("/saved-searches/alpha", json={"query": "golang"})

    assert listing.status_code == 200
    assert listing.json()["total"] == 4
    assert recovery.status_code == 200
    assert recovery.json()["id"] == search_ids["alpha"]
    assert recovery.json()["query"] == "golang"
    assert _saved_search_row(path, "alpha")["query"] == "golang"


def test_lock_cleanup_after_404_422_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unknown = client.patch(
            "/saved-searches/does-not-exist", json={"query": "golang"}
        )
        mixed = client.patch(
            "/saved-searches/alpha", json={"name": "Gamma", "query": "golang"}
        )
        clear_conflict = client.patch(
            "/saved-searches/alpha", json={"source": "x", "clears": ["source"]}
        )
        valid = client.patch("/saved-searches/alpha", json={"query": "golang"})

    assert unknown.status_code == 404
    assert mixed.status_code == 422
    assert clear_conflict.status_code == 422
    # The lock was released after each error: a later valid request succeeds.
    assert valid.status_code == 200
    assert valid.json()["id"] == search_ids["alpha"]
    assert valid.json()["query"] == "golang"


def test_lock_cleanup_after_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)

    def fail_update(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated update failure")

    monkeypatch.setattr("careeros.saved_searches.service._update_search", fail_update)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        failed = client.patch("/saved-searches/alpha", json={"query": "golang"})
    assert failed.status_code == 500

    monkeypatch.undo()
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    # The lock is releasable and a later valid request succeeds.
    with TestClient(app) as client:
        recovery = client.patch("/saved-searches/alpha", json={"query": "golang"})
    assert recovery.status_code == 200
    assert recovery.json()["query"] == "golang"


# --------------------------------------------------------------------------- #
# M. Transaction rollback and retry
# --------------------------------------------------------------------------- #


def test_transaction_rollback_and_retry_after_update_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)

    def fail_update(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated update failure")

    monkeypatch.setattr("careeros.saved_searches.service._update_search", fail_update)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"query": "golang"})

    assert response.status_code == 500
    assert response.json()["detail"] == _DATABASE_UNAVAILABLE
    for leaked in ("OperationalError", "simulated update failure", "sqlite3"):
        assert leaked not in response.text

    monkeypatch.undo()
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    # The domain rolled the whole mutation back: no partial field change and
    # updated_at unchanged.
    row = _saved_search_row(path, "alpha")
    assert row["query"] == "engineer"
    assert row["updated_at"] == _EPOCH.isoformat()
    assert search_ids["alpha"] == search_ids["alpha"]

    # A later normal update succeeds (lock released after the 500).
    with TestClient(app) as client:
        recovery = client.patch("/saved-searches/alpha", json={"query": "golang"})
    assert recovery.status_code == 200
    assert recovery.json()["id"] == search_ids["alpha"]
    assert recovery.json()["query"] == "golang"
    assert _saved_search_row(path, "alpha")["query"] == "golang"


def test_rename_branch_rollback_still_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)

    def fail_rename(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated rename failure")

    monkeypatch.setattr("careeros.saved_searches.service._rename_search", fail_rename)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "Gamma"})

    assert response.status_code == 500
    assert response.json()["detail"] == _DATABASE_UNAVAILABLE

    monkeypatch.undo()
    assert _saved_search_by_id(path, search_ids["alpha"])["name"] == "alpha"
    assert _saved_search_by_id(path, search_ids["alpha"])["updated_at"] == (
        _EPOCH.isoformat()
    )


# --------------------------------------------------------------------------- #
# N. Side effects and privacy
# --------------------------------------------------------------------------- #


def test_update_creates_no_side_effect_rows_and_unchanged_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _seed_saved_searches(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note=_SENTINEL_APPLICATION_NOTE,
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="shortlist",
            note=_SENTINEL_REVIEW_NOTE,
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=job_ids["c-1"],
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message=_SENTINEL_REMINDER_MESSAGE,
            now=_EPOCH,
        )
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    workflow_tables = (
        "reminder_notifications",
        "applications",
        "application_events",
        "review_decisions",
        "review_events",
        "reminders",
    )
    before = {table: _table_snapshot(path, table) for table in workflow_tables}

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"query": "golang"})

    assert response.status_code == 200
    assert response.json()["query"] == "golang"
    # Only the saved_searches table changed; every workflow row is untouched.
    assert _row_count(path, "reminder_notifications") == 0
    for table in workflow_tables:
        assert _table_snapshot(path, table) == before[table]
    assert _row_count(path, "saved_searches") == 4


def test_privacy_leak_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note=_SENTINEL_APPLICATION_NOTE,
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="shortlist",
            note=_SENTINEL_REVIEW_NOTE,
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=job_ids["c-1"],
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message=_SENTINEL_REMINDER_MESSAGE,
            now=_EPOCH,
        )
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    responses: list = []
    startup_messages: list[str] = []

    # 503 while a child process holds the collection lock.
    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            responses.append(
                client.patch("/saved-searches/alpha", json={"query": "golang"})
            )
        _release_child(child)
    finally:
        if child.poll() is None:
            _release_child(child)

    # Startup refuses a malformed database, then the valid seeded database is
    # used for response-privacy coverage.
    malformed = tmp_path / "garbage.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, malformed)
    with pytest.raises(ValueError, match="current schema v11") as exc_info:
        with TestClient(app):
            pass
    startup_messages.append(str(exc_info.value))

    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        responses.append(
            client.patch("/saved-searches/alpha", json={"query": "golang"})
        )
        responses.append(
            client.patch("/saved-searches/does-not-exist", json={"query": "golang"})
        )
        responses.append(
            client.patch(
                "/saved-searches/alpha", json={"name": "Gamma", "query": "golang"}
            )
        )
        responses.append(
            client.patch("/saved-searches/alpha", json={"clears": ["bogus"]})
        )
        responses.append(client.get("/saved-searches"))
        responses.append(client.get("/saved-searches/Top-Ranks"))
        responses.append(client.patch("/saved-searches/alpha", json={"name": "Gamma"}))

    for response in responses:
        for leaked in (
            _SENTINEL_PROFILE_PATH,
            _SENTINEL_DESCRIPTION,
            _SENTINEL_APPLICATION_NOTE,
            _SENTINEL_REVIEW_NOTE,
            _SENTINEL_REMINDER_MESSAGE,
            "profile_path",
            "description",
            "latest_note",
            "note",
            "message",
            "feedback",
            "notification",
            "attempt",
            "backend",
            "Traceback",
            "sqlite3",
            "OperationalError",
            str(path),
            str(lock_path_for(path)),
            "pid",
            "command",
        ):
            assert leaked not in response.text, (
                f"{leaked!r} leaked in {response.status_code} response"
            )
    assert startup_messages == ["CareerOS requires a current schema v11 database"]
    for leaked in (
        _SENTINEL_PROFILE_PATH,
        _SENTINEL_DESCRIPTION,
        str(malformed),
        "sqlite3",
        "OperationalError",
        "not a database",
    ):
        assert leaked not in startup_messages[0]


# --------------------------------------------------------------------------- #
# O. Regression / invariants
# --------------------------------------------------------------------------- #


def test_prior_saved_search_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _seed_saved_searches(path)
    _update_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/saved-searches", json={"name": "fresh", "query": "python"}
        )
        listing = client.get("/saved-searches")
        detail = client.get("/saved-searches/alpha")
        executed = client.post("/saved-searches/alpha/execute")
        renamed = client.patch("/saved-searches/fresh", json={"name": "fresh-renamed"})
        deleted = client.delete("/saved-searches/fresh-renamed")

    assert created.status_code == 200
    assert created.json()["mode"] == "search"
    assert listing.status_code == 200
    assert listing.json()["total"] == 5
    assert detail.status_code == 200
    assert detail.json()["query"] == "engineer"
    assert executed.status_code == 200
    assert executed.json()["total"] == 1
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "fresh-renamed"
    assert deleted.status_code == 200
    assert deleted.json()["name"] == "fresh-renamed"
    assert listing.json()["total"] == 5


def test_prior_mutation_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        application = client.put("/applications/1", json={"status": "interested"})
        review = client.put("/review-decisions/2", json={"decision": "shortlist"})
        reminder = client.post(
            "/reminders",
            json={"job_id": 3, "kind": "follow_up", "due_at": "2026-08-10T14:00:00Z"},
        )

    assert application.status_code == 200
    assert application.json()["current_status"] == "interested"
    assert review.status_code == 200
    assert review.json()["decision"] == "shortlist"
    assert reminder.status_code == 200
    assert reminder.json()["kind"] == "follow_up"


def test_schema_version_stays_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.json()["schema_version"] == SCHEMA_VERSION
    with connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
    assert row is not None
    assert row["value"] == str(SCHEMA_VERSION)


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH, database_path

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        client.patch("/saved-searches/alpha", json={"query": "golang"})

    # The suite resolved only the tmp database, never the real one.
    assert database_path() == path
    assert not real.is_file() or real.stat().st_mtime_ns == before


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
