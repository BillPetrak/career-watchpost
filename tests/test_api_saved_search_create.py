"""Tests for the CareerOS saved-search creation write API.

Covers ``POST /saved-searches``: full and minimal valid creation returning the
canonical ``SavedSearchItem`` projection (exact field order, ``mode`` defaulting
to ``"search"``, every filter/limit field persisting, ``created_at ==
updated_at`` under a pinned clock), read reflection through
``GET /saved-searches`` and ``GET /saved-searches/{name}``, execution/rename/
delete interoperability on the newly created definition, ``mode="rank"`` and
any other mode rejected with ``422`` and zero mutation, exact and
case-insensitive duplicate names rejected with ``422`` (never ``409``) creating
no second row, the full validation ``422`` contract (empty/whitespace
name/query/source/location, invalid status, include/exclude and untracked
conflicts, zero/negative ``result_limit``), malformed JSON / missing body /
empty body ``422``, forbidden body fields (``profile_path``, ``description``,
``min_score``, ``message``, ``note``, ``id``, ``created_at``, ``updated_at``,
and arbitrary unknowns) rejected with ``422`` and zero mutation, persistence
invariants (``mode`` ``"search"``, ``profile_path``/``description``/
``min_score`` NULL, ``remote=False`` normalized to no-filter), missing/
uninitialized/malformed database privacy-safe ``500``s without file/parent/
lock/schema creation, cross-process lock contention mapped to a privacy-safe
``503`` with lock cleanup and recovery, transaction rollback on the create
write failing plus a successful retry, no notification or side-effect rows, a
privacy leak scan over 200/422/500/503 responses, deterministic repeated
behavior across equivalent databases, prior saved-search and non-saved-search
endpoint regression, schema-version invariant, the shared no-``careeros.cli``
import guarantee, and the rule that the real ``data/database/careeros.sqlite3``
is never opened or modified.
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


def _create_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the saved-search clock so creation is byte-deterministic."""
    monkeypatch.setattr("careeros.saved_searches.service._utc_now", lambda: _EPOCH)


def _initialize(path: Path) -> None:
    """Create and initialize an empty database with the current schema."""
    connection = connect(path)
    initialize_schema(connection)
    connection.close()


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


def _saved_search_row(path: Path, name: str) -> sqlite3.Row:
    """Read one saved-search row directly for byte-level invariants."""
    with connect(path) as connection:
        row = connection.execute(
            "SELECT id, name, mode, query, profile_path, description, grouped,"
            " source, remote, location, untracked, include_statuses,"
            " exclude_statuses, min_score, result_limit, created_at, updated_at"
            " FROM saved_searches WHERE name = ? COLLATE NOCASE ORDER BY id LIMIT 1",
            (name,),
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
# Successful creation, defaults, and response shape
# --------------------------------------------------------------------------- #


def test_full_valid_creation_returns_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    payload = {
        "name": "engineer-src",
        "query": "senior engineer",
        "grouped": True,
        "source": "arbeitnow",
        "remote": True,
        "location": "Berlin",
        "untracked": False,
        "include_statuses": ["interview", "applied"],
        "result_limit": 10,
    }
    with TestClient(app) as client:
        response = client.post("/saved-searches", json=payload)

    assert response.status_code == 200
    item = response.json()
    assert list(item) == _ITEM_FIELDS
    assert item["id"] == 1
    assert item["name"] == "engineer-src"
    assert item["mode"] == "search"
    assert item["query"] == "senior engineer"
    assert item["grouped"] is True
    assert item["source"] == "arbeitnow"
    assert item["remote"] is True
    assert item["location"] == "Berlin"
    assert item["untracked"] is False
    assert item["include_statuses"] == ["interview", "applied"]
    assert item["exclude_statuses"] == []
    assert item["min_score"] is None
    assert item["result_limit"] == 10
    assert item["created_at"] == _EPOCH.isoformat()
    assert item["updated_at"] == _EPOCH.isoformat()
    assert "profile_path" not in item
    assert "description" not in item


def test_minimal_body_defaults_to_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/saved-searches", json={"name": "x", "query": "python"})

    assert response.status_code == 200
    item = response.json()
    assert list(item) == _ITEM_FIELDS
    # mode defaults to "search"; every optional filter stays unset.
    assert item["mode"] == "search"
    assert item["grouped"] is False
    assert item["source"] is None
    assert item["remote"] is None
    assert item["location"] is None
    assert item["untracked"] is False
    assert item["include_statuses"] == []
    assert item["exclude_statuses"] == []
    assert item["min_score"] is None
    assert item["result_limit"] == 20
    assert item["created_at"] == item["updated_at"] == _EPOCH.isoformat()
    assert _search_names(path) == ["x"]


def test_explicit_mode_search_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches", json={"name": "x", "query": "python", "mode": "search"}
        )

    assert response.status_code == 200
    assert response.json()["mode"] == "search"


# --------------------------------------------------------------------------- #
# Mode restriction: rank-mode authoring is impossible over HTTP
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", ["rank", "mode-is-forbidden", "foo"])
def test_non_search_mode_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches", json={"name": "x", "query": "python", "mode": mode}
        )

    assert response.status_code == 422
    # The schema rejects the mode before create_search is ever called.
    assert _row_count(path, "saved_searches") == 0


# --------------------------------------------------------------------------- #
# Persistence invariants (mode, private columns, filter fields)
# --------------------------------------------------------------------------- #


def test_persisted_row_has_search_mode_and_null_private_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={"name": "engineer-src", "query": "senior engineer"},
        )
    assert response.status_code == 200

    row = _saved_search_row(path, "engineer-src")
    assert row["mode"] == "search"
    assert row["profile_path"] is None
    assert row["description"] is None
    assert row["min_score"] is None
    assert row["created_at"] == row["updated_at"] == _EPOCH.isoformat()
    assert row["grouped"] == 0
    assert row["source"] is None
    assert row["remote"] is None
    assert row["location"] is None
    assert row["untracked"] == 0
    assert row["include_statuses"] is None
    assert row["exclude_statuses"] is None
    assert row["result_limit"] == 20


def test_filter_fields_persist_to_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={
                "name": "engineer-src",
                "query": "senior engineer",
                "grouped": True,
                "source": "arbeitnow",
                "remote": True,
                "location": "Berlin",
                "untracked": True,
                "result_limit": 7,
            },
        )
    assert response.status_code == 200
    item = response.json()
    assert item["grouped"] is True
    assert item["source"] == "arbeitnow"
    assert item["remote"] is True
    assert item["location"] == "Berlin"
    assert item["untracked"] is True
    assert item["include_statuses"] == []
    assert item["exclude_statuses"] == []
    assert item["result_limit"] == 7

    row = _saved_search_row(path, "engineer-src")
    assert row["grouped"] == 1
    assert row["source"] == "arbeitnow"
    assert row["remote"] == 1
    assert row["location"] == "Berlin"
    assert row["untracked"] == 1
    assert row["result_limit"] == 7
    assert row["mode"] == "search"
    assert row["profile_path"] is None
    assert row["description"] is None
    assert row["min_score"] is None


def test_exclude_statuses_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={
                "name": "x",
                "query": "python",
                "exclude_statuses": ["rejected", "withdrawn"],
            },
        )

    assert response.status_code == 200
    item = response.json()
    assert item["untracked"] is False
    assert item["include_statuses"] == []
    assert item["exclude_statuses"] == ["rejected", "withdrawn"]
    row = _saved_search_row(path, "x")
    assert row["untracked"] == 0
    assert row["include_statuses"] is None
    assert row["exclude_statuses"] == '["rejected","withdrawn"]'


@pytest.mark.parametrize("remote", [False, None])
def test_remote_false_and_none_normalized_to_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: bool | None
) -> None:
    """``remote=False`` and ``remote=None`` share the domain's no-filter meaning.

    ``False`` carries no distinct meaning in the existing search
    implementation (``remote`` is None = no filter or True = remote only), so
    both persist as NULL and read back as ``None``.
    """
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    body: dict = {"name": "x", "query": "python"}
    if remote is not None:
        body["remote"] = remote
    with TestClient(app) as client:
        response = client.post("/saved-searches", json=body)

    assert response.status_code == 200
    assert response.json()["remote"] is None
    assert _saved_search_row(path, "x")["remote"] is None


# --------------------------------------------------------------------------- #
# Read reflection
# --------------------------------------------------------------------------- #


def test_list_and_detail_reflect_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/saved-searches", json={"name": "alpha", "query": "engineer"}
        )
        listing = client.get("/saved-searches")
        detail = client.get("/saved-searches/alpha")

    assert created.status_code == 200
    created_id = created.json()["id"]
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert [item["name"] for item in listing.json()["items"]] == ["alpha"]
    assert detail.status_code == 200
    assert detail.json() == created.json()
    assert detail.json()["id"] == created_id
    assert _search_names(path) == ["alpha"]


# --------------------------------------------------------------------------- #
# Execution / rename / delete interoperability
# --------------------------------------------------------------------------- #


def test_execute_created_search_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/saved-searches",
            json={"name": "engineer-src", "query": "engineer", "result_limit": 10},
        )
        executed = client.post("/saved-searches/engineer-src/execute")

    assert created.status_code == 200
    assert created.json()["mode"] == "search"
    assert executed.status_code == 200
    body = executed.json()
    assert body["mode"] == "search"
    assert body["grouped"] is False
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Senior Engineer"


def test_rename_works_on_created_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/saved-searches", json={"name": "alpha", "query": "engineer"}
        )
        renamed = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
        old_detail = client.get("/saved-searches/alpha")
        new_detail = client.get("/saved-searches/Gamma")

    assert created.status_code == 200
    created_id = created.json()["id"]
    assert renamed.status_code == 200
    assert renamed.json()["id"] == created_id
    assert renamed.json()["name"] == "Gamma"
    assert old_detail.status_code == 404
    assert new_detail.status_code == 200
    assert new_detail.json()["id"] == created_id


def test_delete_works_on_created_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/saved-searches", json={"name": "alpha", "query": "engineer"}
        )
        deleted = client.delete("/saved-searches/alpha")
        detail = client.get("/saved-searches/alpha")

    assert created.status_code == 200
    created_id = created.json()["id"]
    assert deleted.status_code == 200
    assert deleted.json()["id"] == created_id
    assert deleted.json()["name"] == "alpha"
    assert detail.status_code == 404
    assert _row_count(path, "saved_searches") == 0


# --------------------------------------------------------------------------- #
# Duplicate names: 422, never 409, never a second row
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("duplicate_name", ["alpha", "ALPHA"])
def test_duplicate_name_422_no_second_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate_name: str
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches", json={"name": duplicate_name, "query": "python"}
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": f"A saved search named {duplicate_name!r} already exists."
    }
    # The duplicate attempt created no second row.
    assert _row_count(path, "saved_searches") == 3
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    # The pre-existing row is untouched: it keeps its original definition and
    # creation timestamp (case-insensitive lookup resolves both variants).
    row = _saved_search_row(path, duplicate_name)
    assert row["query"] == "engineer"
    assert row["created_at"] == _EPOCH.isoformat()


# --------------------------------------------------------------------------- #
# Domain validation 422 (never duplicated in the request schema)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"name": ""}, "Saved-search name must not be empty or whitespace."),
        ({"name": "   "}, "Saved-search name must not be empty or whitespace."),
        ({"query": ""}, "Saved-search query must not be empty or whitespace."),
        ({"query": "   "}, "Saved-search query must not be empty or whitespace."),
        ({"source": ""}, "Source filter must not be empty or whitespace."),
        ({"location": "  "}, "Location filter must not be empty or whitespace."),
    ],
)
def test_blank_definition_fields_422_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict,
    expected: str,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    body: dict = {"name": "x", "query": "python"}
    body.update(overrides)
    with TestClient(app) as client:
        response = client.post("/saved-searches", json=body)

    assert response.status_code == 422
    assert response.json() == {"detail": expected}
    assert _row_count(path, "saved_searches") == 0


def test_invalid_status_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={
                "name": "x",
                "query": "python",
                "include_statuses": ["bogus"],
            },
        )

    assert response.status_code == 422
    assert "Invalid application status 'bogus'" in response.json()["detail"]
    assert _row_count(path, "saved_searches") == 0


def test_include_exclude_conflict_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={
                "name": "x",
                "query": "python",
                "include_statuses": ["applied"],
                "exclude_statuses": ["rejected"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "include_statuses and exclude_statuses cannot be combined."
    }
    assert _row_count(path, "saved_searches") == 0


def test_untracked_status_conflict_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={
                "name": "x",
                "query": "python",
                "untracked": True,
                "include_statuses": ["applied"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "untracked cannot be combined with include_statuses or "
        "exclude_statuses."
    }
    assert _row_count(path, "saved_searches") == 0


@pytest.mark.parametrize("result_limit", [0, -5])
def test_invalid_result_limit_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_limit: int
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={"name": "x", "query": "python", "result_limit": result_limit},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "result_limit must be at least 1."}
    assert _row_count(path, "saved_searches") == 0


# --------------------------------------------------------------------------- #
# Malformed request bodies
# --------------------------------------------------------------------------- #


def test_malformed_json_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    assert _row_count(path, "saved_searches") == 0


def test_missing_body_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/saved-searches")

    assert response.status_code == 422
    assert _row_count(path, "saved_searches") == 0


def test_empty_body_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/saved-searches", json={})

    assert response.status_code == 422
    assert _row_count(path, "saved_searches") == 0


# --------------------------------------------------------------------------- #
# Forbidden body fields: 422, never persisted, never echoed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_path", "/tmp/private-candidate.yaml"),
        ("description", "description-probe"),
        ("min_score", 10),
        ("message", "secret-message"),
        ("note", "secret-note"),
        ("id", 1),
        ("created_at", "2026-08-03T12:00:00+00:00"),
        ("updated_at", "2026-08-03T12:00:00+00:00"),
        ("bogus", "surprise"),
    ],
)
def test_forbidden_body_fields_422_no_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/saved-searches",
            json={"name": "x", "query": "python", field: value},
        )

    assert response.status_code == 422
    # No mutation and no private value is ever persisted (FastAPI's own 422
    # shape echoes the submitted input, which is never a stored private field).
    assert _row_count(path, "saved_searches") == 0
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
    _initialize(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.post(
                "/saved-searches", json={"name": "x", "query": "python"}
            )
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

    # The contested creation changed nothing and the lock is releasable.
    assert _row_count(path, "saved_searches") == 0
    with TestClient(app) as client:
        recovery = client.post("/saved-searches", json={"name": "x", "query": "python"})
    assert recovery.status_code == 200
    assert recovery.json()["name"] == "x"
    assert _search_names(path) == ["x"]


def test_lock_cleanup_after_validation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        duplicate = client.post(
            "/saved-searches", json={"name": "alpha", "query": "python"}
        )
        blank = client.post("/saved-searches", json={"name": "  ", "query": "python"})
        forbidden = client.post(
            "/saved-searches",
            json={"name": "x", "query": "python", "min_score": 10},
        )
        valid = client.post("/saved-searches", json={"name": "x", "query": "python"})

    assert duplicate.status_code == 422
    assert blank.status_code == 422
    assert forbidden.status_code == 422
    # The lock was released after each error: a later valid request succeeds.
    assert valid.status_code == 200
    assert valid.json()["name"] == "x"
    assert _row_count(path, "saved_searches") == 4


def test_lock_cleanup_after_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)

    def fail_insert(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated insert failure")

    monkeypatch.setattr("careeros.saved_searches.service._insert_search", fail_insert)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        failed = client.post("/saved-searches", json={"name": "x", "query": "python"})
    assert failed.status_code == 500

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The lock is releasable and a later valid request succeeds.
    with TestClient(app) as client:
        recovery = client.post("/saved-searches", json={"name": "x", "query": "python"})
    assert recovery.status_code == 200
    assert recovery.json()["name"] == "x"
    assert _search_names(path) == ["x"]


# --------------------------------------------------------------------------- #
# Transaction rollback and retry
# --------------------------------------------------------------------------- #


def test_transaction_rollback_and_retry_after_create_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)

    def fail_insert(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated insert failure")

    monkeypatch.setattr("careeros.saved_searches.service._insert_search", fail_insert)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.post("/saved-searches", json={"name": "x", "query": "python"})

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )
    for leaked in ("OperationalError", "simulated insert failure", "sqlite3"):
        assert leaked not in response.text

    monkeypatch.undo()
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    # The domain rolled the whole mutation back: no partial row survives.
    assert _row_count(path, "saved_searches") == 0

    # A later normal creation succeeds (lock released after the 500).
    with TestClient(app) as client:
        recovery = client.post("/saved-searches", json={"name": "x", "query": "python"})
    assert recovery.status_code == 200
    assert recovery.json()["name"] == "x"
    assert _search_names(path) == ["x"]


# --------------------------------------------------------------------------- #
# Side-effect separation
# --------------------------------------------------------------------------- #


def test_create_creates_no_side_effect_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/saved-searches", json={"name": "x", "query": "python"})

    assert response.status_code == 200
    # Creation touches only the saved_searches table: no workflow or
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
    assert _row_count(path, "saved_searches") == 1


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
    _create_now(monkeypatch)
    _set_database(monkeypatch, path)

    responses: list = []
    startup_messages: list[str] = []

    # 503 while a child process holds the collection lock.
    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            responses.append(
                client.post("/saved-searches", json={"name": "new-one", "query": "go"})
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
        responses.append(
            client.post("/saved-searches", json={"name": "fresh", "query": "python"})
        )
        responses.append(
            client.post("/saved-searches", json={"name": "alpha", "query": "python"})
        )
        responses.append(
            client.post(
                "/saved-searches",
                json={"name": "fresh", "query": "python", "mode": "rank"},
            )
        )
        responses.append(client.get("/saved-searches"))
        responses.append(client.get("/saved-searches/Top-Ranks"))
        responses.append(client.post("/saved-searches/Top-Ranks/execute"))

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
    created = client.post(
        "/saved-searches",
        json={
            "name": "alpha",
            "query": "engineer",
            "grouped": True,
            "source": "arbeitnow",
            "result_limit": 7,
        },
    )
    duplicate = client.post(
        "/saved-searches", json={"name": "ALPHA", "query": "python"}
    )
    listing = client.get("/saved-searches")
    detail = client.get("/saved-searches/alpha")
    blank = client.post("/saved-searches", json={"name": "  ", "query": "python"})
    renamed = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
    deleted = client.delete("/saved-searches/Gamma")
    return (
        created.status_code,
        created.json(),
        duplicate.status_code,
        duplicate.json(),
        listing.status_code,
        listing.json(),
        detail.status_code,
        detail.json(),
        blank.status_code,
        blank.json(),
        renamed.status_code,
        renamed.json(),
        deleted.status_code,
        deleted.json(),
    )


def test_repeated_creates_and_reads_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    _initialize(left)
    _initialize(right)
    _create_now(monkeypatch)
    _set_database(monkeypatch, left)
    with TestClient(app) as client:
        left_sequence = _exercise_sequence(client)

    _set_database(monkeypatch, right)
    with TestClient(app) as client:
        right_sequence = _exercise_sequence(client)

    assert left_sequence == right_sequence


# --------------------------------------------------------------------------- #
# Prior saved-search endpoint regression
# --------------------------------------------------------------------------- #


def test_prior_saved_search_reads_and_execute_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        listing = client.get("/saved-searches")
        detail = client.get("/saved-searches/alpha")
        executed = client.post("/saved-searches/alpha/execute")

    assert listing.status_code == 200
    assert listing.json()["total"] == 3
    assert [item["name"] for item in listing.json()["items"]] == [
        "alpha",
        "Beta",
        "Top-Ranks",
    ]
    assert detail.status_code == 200
    assert detail.json()["name"] == "alpha"
    assert executed.status_code == 200
    assert executed.json()["total"] == 1
    assert executed.json()["items"][0]["title"] == "Senior Engineer"


def test_prior_saved_search_rename_and_delete_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        renamed = client.patch("/saved-searches/alpha", json={"name": "Gamma"})
        deleted = client.delete("/saved-searches/Gamma")
        listing = client.get("/saved-searches")

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Gamma"
    assert deleted.status_code == 200
    assert deleted.json()["name"] == "Gamma"
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["items"]] == [
        "Beta",
        "Top-Ranks",
    ]


def test_prior_non_saved_search_endpoints_unchanged(
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
        review = client.put("/review-decisions/1", json={"decision": "shortlist"})
        application = client.put("/applications/1", json={"status": "interested"})

    assert health.status_code == 200
    assert health.json()["schema_version"] == SCHEMA_VERSION
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 3
    assert reminders.status_code == 200
    assert reminders.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert review.status_code == 200
    assert review.json()["decision"] == "shortlist"
    assert application.status_code == 200
    assert application.json()["current_status"] == "interested"


# --------------------------------------------------------------------------- #
# Schema invariant
# --------------------------------------------------------------------------- #


def test_schema_version_stays_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _initialize(path)
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
    _initialize(path)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        client.post("/saved-searches", json={"name": "x", "query": "python"})

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
