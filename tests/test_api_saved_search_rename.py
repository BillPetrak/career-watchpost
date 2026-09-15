"""Tests for the CareerOS saved-search rename write API.

Covers ``PATCH /saved-searches/{name}``: successful Alpha -> Beta rename
returning the canonical ``SavedSearchItem`` projection (exact field order, new
casing stored as given, ``updated_at`` bumped on a genuine rename), read
reflection through ``GET /saved-searches`` and ``GET /saved-searches/{name}``
(old name ``404``, new name resolves), case-insensitive source lookup with a
genuinely different target, same-name and case-only renames as idempotent
no-ops that preserve the stored casing and ``updated_at``, duplicate-target
mapping to ``422`` with the canonical domain detail (never ``409``) and no
mutation, unknown-source ``404``, the rename-only body contract (empty/
whitespace names and the forbidden ``profile_path``/``description``/``query``/
``mode``/``message``/``note``/unknown fields all ``422``, malformed JSON /
missing body / empty object ``422``), privacy shape (``profile_path`` and
``description`` structurally absent, private fields preserved byte-identically
in the database), missing/uninitialized/malformed database privacy-safe
``500``s without file/parent/lock/schema creation, cross-process lock
contention mapped to a privacy-safe ``503`` with lock cleanup and recovery,
transaction rollback on the rename write failing, no notification or side-effect
rows, a privacy leak scan over success/404/422/500/503 responses,
deterministic repeated behavior across equivalent databases, read/delete/
execute regression under the new name, prior-endpoint regression,
schema-version invariant, the shared no-``careeros.cli`` import guarantee, and
the rule that the real ``data/database/careeros.sqlite3`` is never opened or
modified.
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


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


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
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build things.",
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
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("remoteok", "b-1", "Data Scientist", "Other Co", "Munich"),
            _make_job("jobicy", "c-1", "Frontend Developer", "Frontend Labs", "Berlin"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.close()
    return job_ids


def _seed_saved_searches(path: Path) -> dict[str, int]:
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
# Successful rename and reflection
# --------------------------------------------------------------------------- #


def test_successful_rename_returns_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
        list_after = client.get("/saved-searches")
        old_detail = client.get("/saved-searches/alpha")
        new_detail = client.get("/saved-searches/Gamma")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["id"] == search_ids["alpha"]
    assert payload["name"] == "Gamma"
    assert payload["mode"] == "search"
    assert payload["query"] == "engineer"
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
    # A genuine rename updates updated_at (pinned clock proves it).
    assert payload["updated_at"] == _RENAME_EPOCH.isoformat()
    assert "profile_path" not in payload
    assert "description" not in payload

    # The rename is reflected by the existing read endpoints.
    assert old_detail.status_code == 404
    assert old_detail.json() == {"detail": "No saved search named 'alpha'."}
    assert new_detail.status_code == 200
    assert new_detail.json()["id"] == search_ids["alpha"]
    assert new_detail.json()["name"] == "Gamma"
    assert list_after.status_code == 200
    names = [item["name"] for item in list_after.json()["items"]]
    assert names == ["Beta", "Gamma", "Top-Ranks"]
    # Direct DB read keeps the renamed row at its original id position.
    assert _search_names(path) == ["Gamma", "Beta", "Top-Ranks"]


def test_rank_mode_rename_preserves_private_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/Top-Ranks", json={"name": "Ranked-Special"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["id"] == search_ids["Top-Ranks"]
    assert payload["name"] == "Ranked-Special"
    assert payload["mode"] == "rank"
    assert payload["query"] == "senior engineer"
    assert payload["min_score"] == 30
    assert payload["result_limit"] == 15
    assert "profile_path" not in response.text
    assert "description" not in response.text
    assert _SENTINEL_PROFILE_PATH not in response.text
    assert _SENTINEL_DESCRIPTION not in response.text

    # The private fields survive the rename byte-identically in the database.
    row = _saved_search_by_id(path, search_ids["Top-Ranks"])
    assert row["name"] == "Ranked-Special"
    assert row["profile_path"] == _SENTINEL_PROFILE_PATH
    assert row["description"] == _SENTINEL_DESCRIPTION
    assert row["updated_at"] == _RENAME_EPOCH.isoformat()


# --------------------------------------------------------------------------- #
# Case-insensitive identity
# --------------------------------------------------------------------------- #


def test_case_insensitive_source_lookup_renames_same_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    # Stored source name is "alpha"; the genuinely different target renames
    # the same row through the case-insensitive lookup.
    with TestClient(app) as client:
        response = client.patch("/saved-searches/ALPHA", json={"name": "epsilon"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == search_ids["alpha"]
    assert payload["name"] == "epsilon"
    assert _search_names(path) == ["epsilon", "Beta", "Top-Ranks"]


def test_genuine_rename_stores_given_casing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"name": "GammaSearch"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == search_ids["Beta"]
    assert payload["name"] == "GammaSearch"
    row = _saved_search_by_id(path, search_ids["Beta"])
    assert row["name"] == "GammaSearch"
    assert row["updated_at"] == _RENAME_EPOCH.isoformat()


# --------------------------------------------------------------------------- #
# Idempotent no-op renames
# --------------------------------------------------------------------------- #


def test_same_name_rename_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"name": "Beta"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == search_ids["Beta"]
    assert payload["name"] == "Beta"
    # A no-op preserves updated_at (creation time, not the pinned rename time).
    assert payload["updated_at"] == _EPOCH.isoformat()
    row = _saved_search_by_id(path, search_ids["Beta"])
    assert row["name"] == "Beta"
    assert row["updated_at"] == _EPOCH.isoformat()


def test_case_only_rename_noop_preserves_casing_and_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/Beta", json={"name": "beta"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == search_ids["Beta"]
    # Stored casing is preserved on a case-only no-op.
    assert payload["name"] == "Beta"
    assert payload["updated_at"] == _EPOCH.isoformat()
    row = _saved_search_by_id(path, search_ids["Beta"])
    assert row["name"] == "Beta"
    assert row["updated_at"] == _EPOCH.isoformat()


# --------------------------------------------------------------------------- #
# Duplicate target
# --------------------------------------------------------------------------- #


def test_duplicate_target_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "Beta"})

    assert response.status_code == 422
    assert response.json() == {"detail": "A saved search named 'Beta' already exists."}
    # Neither row changed.
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    assert _saved_search_by_id(path, search_ids["alpha"])["name"] == "alpha"
    assert _saved_search_by_id(path, search_ids["alpha"])["updated_at"] == (
        _EPOCH.isoformat()
    )
    assert _saved_search_by_id(path, search_ids["Beta"])["updated_at"] == (
        _EPOCH.isoformat()
    )
    assert _row_count(path, "saved_searches") == 3


# --------------------------------------------------------------------------- #
# Unknown source
# --------------------------------------------------------------------------- #


def test_unknown_source_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/does-not-exist", json={"name": "Gamma"}
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "No saved search named 'does-not-exist'."}
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]


# --------------------------------------------------------------------------- #
# Rename-only body validation
# --------------------------------------------------------------------------- #


def test_empty_new_name_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": ""})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Saved-search name must not be empty or whitespace."
    }
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]


def test_whitespace_only_name_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "   "})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Saved-search name must not be empty or whitespace."
    }
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]


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
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]


def test_missing_body_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha")

    assert response.status_code == 422
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]


def test_empty_object_body_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={})

    assert response.status_code == 422
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bogus", "surprise"),
        ("profile_path", "/tmp/private-candidate.yaml"),
        ("description", "description-probe"),
        ("query", "engineer"),
        ("mode", "rank"),
        ("message", "secret-message"),
        ("note", "secret-note"),
    ],
)
def test_forbidden_body_fields_422_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            "/saved-searches/alpha", json={"name": "Gamma", field: value}
        )

    assert response.status_code == 422
    # No mutation: the source row keeps its name and creation timestamp, and
    # no stored private value ever appears in the response (FastAPI's own 422
    # shape echoes the submitted input, which is never a stored private field).
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    assert _saved_search_by_id(path, search_ids["alpha"])["name"] == "alpha"
    assert _saved_search_by_id(path, search_ids["alpha"])["updated_at"] == (
        _EPOCH.isoformat()
    )
    assert _SENTINEL_PROFILE_PATH not in response.text
    assert _SENTINEL_DESCRIPTION not in response.text


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_refuses_startup_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v12") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v12 database"
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
    with pytest.raises(ValueError, match="current schema v12") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v12 database"
    assert empty.read_bytes() == before == b""


def test_malformed_database_refuses_startup_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, garbage)

    before = garbage.read_bytes()
    with pytest.raises(ValueError, match="current schema v12") as exc_info:
        with TestClient(app):
            pass

    message = str(exc_info.value)
    assert message == "CareerOS requires a current schema v12 database"
    assert garbage.read_bytes() == before
    for leaked in (
        "sqlite3",
        "OperationalError",
        "not a database",
        "file is not",
    ):
        assert leaked not in message


# --------------------------------------------------------------------------- #
# Cross-process lock contention and lock cleanup
# --------------------------------------------------------------------------- #


def test_lock_contention_503_unchanged_database_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
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

    # The contested rename changed nothing and the lock is releasable.
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    assert _row_count(path, "saved_searches") == 3
    with TestClient(app) as client:
        listing = client.get("/saved-searches")
        recovery = client.patch("/saved-searches/alpha", json={"name": "Gamma"})

    assert listing.status_code == 200
    assert listing.json()["total"] == 3
    assert recovery.status_code == 200
    assert recovery.json()["id"] == search_ids["alpha"]
    assert recovery.json()["name"] == "Gamma"
    assert _search_names(path) == ["Gamma", "Beta", "Top-Ranks"]


def test_lock_cleanup_after_404_and_422s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unknown = client.patch("/saved-searches/does-not-exist", json={"name": "Gamma"})
        duplicate = client.patch("/saved-searches/alpha", json={"name": "Beta"})
        invalid = client.patch("/saved-searches/alpha", json={"name": "  "})
        valid = client.patch("/saved-searches/alpha", json={"name": "Gamma"})

    assert unknown.status_code == 404
    assert duplicate.status_code == 422
    assert invalid.status_code == 422
    # The lock was released after each error: a later valid request succeeds.
    assert valid.status_code == 200
    assert valid.json()["id"] == search_ids["alpha"]
    assert valid.json()["name"] == "Gamma"


# --------------------------------------------------------------------------- #
# Transaction rollback
# --------------------------------------------------------------------------- #


def test_transaction_rollback_on_rename_failure(
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
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )
    for leaked in ("OperationalError", "simulated rename failure", "sqlite3"):
        assert leaked not in response.text

    monkeypatch.undo()
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    # The domain rolled the whole mutation back: no partial rename survives.
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    row = _saved_search_by_id(path, search_ids["alpha"])
    assert row["name"] == "alpha"
    assert row["updated_at"] == _EPOCH.isoformat()

    # A later normal rename succeeds (lock was released after the 500).
    with TestClient(app) as client:
        recovery = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
    assert recovery.status_code == 200
    assert recovery.json()["id"] == search_ids["alpha"]
    assert recovery.json()["name"] == "Gamma"
    assert _search_names(path) == ["Gamma", "Beta", "Top-Ranks"]


# --------------------------------------------------------------------------- #
# Side-effect separation
# --------------------------------------------------------------------------- #


def test_rename_creates_no_side_effect_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/saved-searches/alpha", json={"name": "Gamma"})

    assert response.status_code == 200
    assert response.json()["name"] == "Gamma"
    # Renaming touches only the saved_searches table: no workflow or
    # notification rows are created anywhere.
    for table in (
        "reminder_notifications",
        "applications",
        "application_events",
        "review_decisions",
        "review_events",
        "reminders",
    ):
        assert _row_count(path, table) == 0
    assert _row_count(path, "saved_searches") == 3


# --------------------------------------------------------------------------- #
# Privacy leak scan
# --------------------------------------------------------------------------- #


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
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    responses: list = []
    startup_messages: list[str] = []

    # 503 while a child process holds the collection lock.
    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            responses.append(
                client.patch("/saved-searches/alpha", json={"name": "Gamma"})
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
    with pytest.raises(ValueError, match="current schema v12") as exc_info:
        with TestClient(app):
            pass
    startup_messages.append(str(exc_info.value))

    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        responses.append(client.patch("/saved-searches/alpha", json={"name": "Gamma"}))
        responses.append(
            client.patch("/saved-searches/does-not-exist", json={"name": "Zed"})
        )
        responses.append(client.patch("/saved-searches/Gamma", json={"name": "Beta"}))
        responses.append(client.patch("/saved-searches/Gamma", json={"name": "  "}))
        responses.append(client.get("/saved-searches"))
        responses.append(client.get("/saved-searches/Gamma"))
        responses.append(client.post("/saved-searches/Gamma/execute"))

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
    assert startup_messages == ["CareerOS requires a current schema v12 database"]
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
# Determinism
# --------------------------------------------------------------------------- #


def _exercise_sequence(client: TestClient) -> tuple:
    rename = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
    old_detail = client.get("/saved-searches/alpha")
    new_detail = client.get("/saved-searches/Gamma")
    listing = client.get("/saved-searches")
    duplicate = client.patch("/saved-searches/Gamma", json={"name": "Beta"})
    case_only = client.patch("/saved-searches/Gamma", json={"name": "gamma"})
    unknown = client.patch("/saved-searches/missing", json={"name": "Zed"})
    return (
        rename.status_code,
        rename.json(),
        old_detail.status_code,
        old_detail.json(),
        new_detail.status_code,
        new_detail.json(),
        listing.status_code,
        listing.json(),
        duplicate.status_code,
        duplicate.json(),
        case_only.status_code,
        case_only.json(),
        unknown.status_code,
        unknown.json(),
    )


def test_repeated_renames_and_reads_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    _seed_saved_searches(left)
    _seed_saved_searches(right)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, left)
    with TestClient(app) as client:
        left_sequence = _exercise_sequence(client)

    _set_database(monkeypatch, right)
    with TestClient(app) as client:
        right_sequence = _exercise_sequence(client)

    assert left_sequence == right_sequence


# --------------------------------------------------------------------------- #
# Read / delete / execute regression under the new name
# --------------------------------------------------------------------------- #


def test_read_endpoints_still_work_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        renamed = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
        listing = client.get("/saved-searches")
        new_detail = client.get("/saved-searches/Gamma")
        old_detail = client.get("/saved-searches/alpha")

    assert renamed.status_code == 200
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["items"]] == [
        "Beta",
        "Gamma",
        "Top-Ranks",
    ]
    assert new_detail.status_code == 200
    assert new_detail.json()["id"] == search_ids["alpha"]
    assert new_detail.json()["name"] == "Gamma"
    assert old_detail.status_code == 404


def test_delete_works_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        renamed = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
        deleted = client.delete("/saved-searches/Gamma")
        old_delete = client.delete("/saved-searches/alpha")

    assert renamed.status_code == 200
    assert deleted.status_code == 200
    assert deleted.json()["id"] == search_ids["alpha"]
    assert deleted.json()["name"] == "Gamma"
    assert old_delete.status_code == 404
    assert _search_names(path) == ["Beta", "Top-Ranks"]


def test_execute_works_under_new_name_old_name_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [_make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin")],
    )
    create_search(
        connection,
        fields=SavedSearchFields(
            name="engineer-src",
            query="engineer",
            mode="search",
            result_limit=10,
        ),
        now=_EPOCH,
    )
    connection.close()
    _rename_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        renamed = client.patch(
            "/saved-searches/engineer-src", json={"name": "engineer-renamed"}
        )
        new_execute = client.post("/saved-searches/engineer-renamed/execute")
        old_execute = client.post("/saved-searches/engineer-src/execute")

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "engineer-renamed"
    assert new_execute.status_code == 200
    body = new_execute.json()
    assert body["mode"] == "search"
    assert body["grouped"] is False
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Senior Engineer"
    assert old_execute.status_code == 404
    assert old_execute.json() == {"detail": "No saved search named 'engineer-src'."}


# --------------------------------------------------------------------------- #
# Prior-endpoint regression and schema invariant
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        reminders = client.get("/reminders")
        searches = client.get("/saved-searches")
        execute = client.post("/saved-searches/alpha/execute")
        review = client.put("/review-decisions/1", json={"decision": "shortlist"})
        application = client.put("/applications/1", json={"status": "interested"})

    assert health.status_code == 200
    assert health.json()["schema_version"] == SCHEMA_VERSION
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 3
    assert reminders.status_code == 200
    assert reminders.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert searches.status_code == 200
    assert searches.json()["total"] == 3
    assert execute.status_code == 200
    assert execute.json()["total"] == 1
    assert review.status_code == 200
    assert review.json()["decision"] == "shortlist"
    assert application.status_code == 200
    assert application.json()["current_status"] == "interested"


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


# --------------------------------------------------------------------------- #
# Real database safety
# --------------------------------------------------------------------------- #


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
        client.patch("/saved-searches/alpha", json={"name": "Gamma"})

    # The suite resolved only the tmp database, never the real one.
    assert database_path() == path
    assert not real.is_file() or real.stat().st_mtime_ns == before


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
