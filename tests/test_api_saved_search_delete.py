"""Tests for the CareerOS saved-search deletion write API.

Covers ``DELETE /saved-searches/{name}``: successful deletion returning the
canonical ``SavedSearchItem`` projection of the deleted definition (exact
field order, stored/original name casing preserved), privacy shape
(``profile_path`` and ``description`` structurally absent), read reflection
through ``GET /saved-searches`` and ``GET /saved-searches/{name}``,
case-insensitive lookup with original-casing responses, repeated-DELETE
canonical ``404`` semantics, unknown-name ``404`` with the canonical message, a
sent request body ignored and never stored/echoed, missing/uninitialized/
malformed database privacy-safe ``500``s without file/parent/lock/schema
creation, cross-process lock contention mapped to a privacy-safe ``503`` with
lock cleanup and recovery, transaction rollback on the delete write failing,
deterministic repeated behavior across equivalent databases, saved-search read
ordering and execute regression, prior-endpoint regression, schema-version
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

# Distinctive sentinels proving actual non-leakage of private fields: if any
# string ever appears in response text, the API leaked a private value.
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/sentinel-candidate-profile.yaml"
_SENTINEL_DESCRIPTION = "SENTINEL-DESCRIPTION-7f3a2b9c"
_SENTINEL_BODY_MESSAGE = "secret-body-message"
_SENTINEL_BODY_NOTE = "secret-body-note"
_SENTINEL_BODY_PATH = "secret-body-path"
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
# Successful deletion and reflection
# --------------------------------------------------------------------------- #


def test_successful_delete_returns_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/saved-searches/alpha")
        list_after = client.get("/saved-searches")
        detail_after = client.get("/saved-searches/alpha")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["id"] == search_ids["alpha"]
    assert payload["name"] == "alpha"
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
    assert payload["updated_at"] == _EPOCH.isoformat()
    assert "profile_path" not in payload
    assert "description" not in payload

    # The deletion is reflected by the existing read endpoints.
    assert list_after.status_code == 200
    names = [item["name"] for item in list_after.json()["items"]]
    assert names == ["Beta", "Top-Ranks"]
    assert detail_after.status_code == 404
    assert detail_after.json() == {"detail": "No saved search named 'alpha'."}
    assert _search_names(path) == ["Beta", "Top-Ranks"]


def test_rank_mode_deleted_item_keeps_filter_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/saved-searches/Top-Ranks")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["id"] == search_ids["Top-Ranks"]
    assert payload["name"] == "Top-Ranks"
    assert payload["mode"] == "rank"
    assert payload["query"] == "senior engineer"
    assert payload["grouped"] is True
    assert payload["untracked"] is True
    assert payload["min_score"] == 30
    assert payload["result_limit"] == 15
    assert "profile_path" not in payload
    assert "description" not in payload


# --------------------------------------------------------------------------- #
# Privacy shape on success
# --------------------------------------------------------------------------- #


def test_profile_path_and_description_absent_from_deleted_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/saved-searches/Top-Ranks")

    assert response.status_code == 200
    assert _SENTINEL_PROFILE_PATH not in response.text
    assert _SENTINEL_DESCRIPTION not in response.text
    assert "profile_path" not in response.text
    assert "description" not in response.text


# --------------------------------------------------------------------------- #
# Case-insensitive identity
# --------------------------------------------------------------------------- #


def test_case_insensitive_delete_preserves_original_casing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/saved-searches/beta")
        detail = client.get("/saved-searches/beta")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == search_ids["Beta"]
    # The stored/original casing is preserved in the response.
    assert payload["name"] == "Beta"
    assert detail.status_code == 404
    assert detail.json() == {"detail": "No saved search named 'beta'."}
    assert _search_names(path) == ["alpha", "Top-Ranks"]


# --------------------------------------------------------------------------- #
# Repeated DELETE
# --------------------------------------------------------------------------- #


def test_repeated_delete_second_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.delete("/saved-searches/alpha")
        second = client.delete("/saved-searches/alpha")

    assert first.status_code == 200
    assert first.json()["name"] == "alpha"
    assert second.status_code == 404
    assert second.json() == {"detail": "No saved search named 'alpha'."}
    # No row was recreated and no 204/no-op 200 is returned.
    assert _search_names(path) == ["Beta", "Top-Ranks"]
    assert _row_count(path, "saved_searches") == 2


# --------------------------------------------------------------------------- #
# Unknown name
# --------------------------------------------------------------------------- #


def test_unknown_name_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/saved-searches/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "No saved search named 'does-not-exist'."}
    # No mutation occurred.
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    assert _row_count(path, "saved_searches") == 3


# --------------------------------------------------------------------------- #
# Sent request body
# --------------------------------------------------------------------------- #


def test_sent_request_body_ignored_and_never_stored_or_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/saved-searches/alpha",
            json={
                "message": _SENTINEL_BODY_MESSAGE,
                "note": _SENTINEL_BODY_NOTE,
                "profile_path": _SENTINEL_BODY_PATH,
                "description": _SENTINEL_DESCRIPTION,
            },
        )

    # The route declares no body parameter, so a sent body is ignored.
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == search_ids["alpha"]
    assert payload["name"] == "alpha"
    for leaked in (
        _SENTINEL_BODY_MESSAGE,
        _SENTINEL_BODY_NOTE,
        _SENTINEL_BODY_PATH,
        _SENTINEL_DESCRIPTION,
        "message",
        "note",
    ):
        assert leaked not in response.text
    # The deletion still follows the body-less contract.
    assert _search_names(path) == ["Beta", "Top-Ranks"]


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_refuses_startup_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v10 database"
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
    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v10 database"
    assert empty.read_bytes() == before == b""


def test_malformed_database_refuses_startup_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, garbage)

    before = garbage.read_bytes()
    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass

    message = str(exc_info.value)
    assert message == "CareerOS requires a current schema v10 database"
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
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.delete("/saved-searches/alpha")
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

    # The contested DELETE changed nothing and the lock is releasable.
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    assert _row_count(path, "saved_searches") == 3
    with TestClient(app) as client:
        listing = client.get("/saved-searches")
        recovery = client.delete("/saved-searches/alpha")

    assert listing.status_code == 200
    assert listing.json()["total"] == 3
    assert recovery.status_code == 200
    assert recovery.json()["id"] == search_ids["alpha"]
    assert _search_names(path) == ["Beta", "Top-Ranks"]


def test_lock_cleanup_after_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unknown = client.delete("/saved-searches/does-not-exist")
        valid = client.delete("/saved-searches/alpha")

    assert unknown.status_code == 404
    assert valid.status_code == 200
    assert valid.json()["id"] == search_ids["alpha"]
    assert _search_names(path) == ["Beta", "Top-Ranks"]


def test_lock_cleanup_after_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)

    def fail_delete(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated deletion failure")

    monkeypatch.setattr("careeros.saved_searches.service._delete_search", fail_delete)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        failed = client.delete("/saved-searches/alpha")
    assert failed.status_code == 500

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The lock is releasable and a later valid request succeeds.
    with TestClient(app) as client:
        recovery = client.delete("/saved-searches/alpha")
    assert recovery.status_code == 200
    assert recovery.json()["id"] == search_ids["alpha"]
    assert _search_names(path) == ["Beta", "Top-Ranks"]


# --------------------------------------------------------------------------- #
# Transaction rollback
# --------------------------------------------------------------------------- #


def test_transaction_rollback_on_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    search_ids = _seed_saved_searches(path)

    def fail_delete(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated deletion failure")

    monkeypatch.setattr("careeros.saved_searches.service._delete_search", fail_delete)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.delete("/saved-searches/alpha")

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )
    for leaked in ("OperationalError", "simulated deletion failure", "sqlite3"):
        assert leaked not in response.text

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The domain rolled the whole mutation back: no partial deletion survives.
    assert _search_names(path) == ["alpha", "Beta", "Top-Ranks"]
    assert _row_count(path, "saved_searches") == 3

    # A later normal deletion succeeds.
    with TestClient(app) as client:
        recovery = client.delete("/saved-searches/alpha")
    assert recovery.status_code == 200
    assert recovery.json()["id"] == search_ids["alpha"]
    assert _search_names(path) == ["Beta", "Top-Ranks"]


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def _exercise_sequence(client: TestClient, name: str) -> tuple:
    first = client.delete(f"/saved-searches/{name}")
    second = client.delete(f"/saved-searches/{name}")
    listing = client.get("/saved-searches")
    detail = client.get(f"/saved-searches/{name}")
    return (
        first.status_code,
        first.json(),
        second.status_code,
        second.json(),
        listing.status_code,
        listing.json(),
        detail.status_code,
        detail.json(),
    )


def test_repeated_deletes_and_reads_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    _seed_saved_searches(left)
    _seed_saved_searches(right)
    _set_database(monkeypatch, left)
    with TestClient(app) as client:
        left_sequence = _exercise_sequence(client, "alpha")

    _set_database(monkeypatch, right)
    with TestClient(app) as client:
        right_sequence = _exercise_sequence(client, "alpha")

    assert left_sequence == right_sequence


# --------------------------------------------------------------------------- #
# Saved-search read ordering and execute regression
# --------------------------------------------------------------------------- #


def test_remaining_reads_and_ordering_after_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        deleted = client.delete("/saved-searches/Beta")
        listing = client.get("/saved-searches")
        alpha_detail = client.get("/saved-searches/alpha")

    assert deleted.status_code == 200
    assert deleted.json()["name"] == "Beta"
    # Remaining definitions are still listed case-insensitively by name.
    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["items"]] == ["alpha", "Top-Ranks"]
    assert alpha_detail.status_code == 200
    assert alpha_detail.json()["name"] == "alpha"


def test_execute_of_other_saved_search_unaffected_by_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
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
    create_search(
        connection,
        fields=SavedSearchFields(
            name="keep-engineer",
            query="engineer",
            mode="search",
            result_limit=10,
        ),
        now=_EPOCH,
    )
    create_search(
        connection,
        fields=SavedSearchFields(
            name="drop-me",
            query="data scientist",
            mode="search",
            result_limit=5,
        ),
        now=_EPOCH,
    )
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        deleted = client.delete("/saved-searches/drop-me")
        keep = client.post("/saved-searches/keep-engineer/execute")
        dropped = client.post("/saved-searches/drop-me/execute")

    assert deleted.status_code == 200
    # Deleting one workflow does not break executing a different one.
    assert keep.status_code == 200
    body = keep.json()
    assert body["mode"] == "search"
    assert body["grouped"] is False
    assert body["total"] == 1
    assert body["items"][0]["title"] == "Senior Engineer"
    # The deleted workflow is gone from the execution surface too.
    assert dropped.status_code == 404
    assert dropped.json() == {"detail": "No saved search named 'drop-me'."}


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
    _set_database(monkeypatch, path)

    responses: list = []
    startup_messages: list[str] = []

    # 503 while a child process holds the collection lock.
    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            responses.append(client.delete("/saved-searches/alpha"))
        _release_child(child)
    finally:
        if child.poll() is None:
            _release_child(child)

    # Startup refuses a malformed database, then the valid seeded database is
    # used for response-privacy coverage.
    malformed = tmp_path / "garbage.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, malformed)
    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass
    startup_messages.append(str(exc_info.value))

    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        responses.append(client.delete("/saved-searches/alpha"))
        responses.append(client.delete("/saved-searches/alpha"))
        responses.append(
            client.request(
                "DELETE",
                "/saved-searches/Beta",
                json={
                    "message": _SENTINEL_BODY_MESSAGE,
                    "note": _SENTINEL_BODY_NOTE,
                    "profile_path": _SENTINEL_BODY_PATH,
                    "description": _SENTINEL_DESCRIPTION,
                },
            )
        )
        responses.append(client.delete("/saved-searches/does-not-exist"))
        responses.append(client.get("/saved-searches"))
        responses.append(client.get("/saved-searches/Top-Ranks"))
        responses.append(client.get("/saved-searches/alpha"))
        responses.append(client.post("/saved-searches/Top-Ranks/execute"))

    for response in responses:
        for leaked in (
            _SENTINEL_PROFILE_PATH,
            _SENTINEL_DESCRIPTION,
            _SENTINEL_BODY_MESSAGE,
            _SENTINEL_BODY_NOTE,
            _SENTINEL_BODY_PATH,
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
    assert startup_messages == ["CareerOS requires a current schema v10 database"]
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


def test_schema_version_stays_current_v10(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_saved_searches(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.json()["schema_version"] == SCHEMA_VERSION == 10
    with connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
    assert row is not None
    assert row["value"] == "10"


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
        client.delete("/saved-searches/alpha")

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
