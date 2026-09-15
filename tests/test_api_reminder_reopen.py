"""Tests for the CareerOS reminder-reopen write API.

Covers ``PUT /reminders/{reminder_id}/reopen``: a first successful reopen of a
completed reminder returning the canonical ``ReminderItem`` projection (exact
field order, null ``completed_at``, ``updated_at`` moved to the domain's live
UTC timestamp, nested canonical job projection, message structurally absent),
current-state reflection through ``GET /reminders`` and
``GET /reminders/{reminder_id}`` (including the default list and the
``state=open``/``state=completed`` filters), idempotent repeated reopens
(identical response body, unchanged ``completed_at``/``updated_at``, no
additional row mutation), reopening an already-open reminder as a true no-op,
the complete -> reopen -> complete lifecycle round-trip, a seeded reminder
message staying unchanged and unexposed, unknown-reminder 404, non-integer
path 422, a sent request body being ignored with nothing stored,
missing/uninitialized/malformed database 500s without any file/parent/lock
creation, cross-process lock contention mapped to a privacy-safe 503 with lock
cleanup and recovery afterwards, lock release after 404 and 500, transaction
rollback on the reopen write failing, notification separation (reopen never
creates or touches ``reminder_notifications`` rows), a privacy leak scan over
every response, deterministic structural behavior, prior-endpoint regression,
and the shared no-``careeros.cli`` import guarantee.
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
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import lock_path_for
from careeros.reminders.service import add_reminder, complete_reminder
from careeros.review.service import set_decision
from tests.support import TestClient

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

_SRC = Path(__file__).resolve().parents[1] / "src"

_JOB_FIELDS = [
    "id",
    "source",
    "source_job_id",
    "title",
    "company",
    "location",
    "remote_status",
    "employment_type",
    "published_at",
    "url",
]

_ITEM_FIELDS = [
    "reminder_id",
    "job_id",
    "kind",
    "due_at",
    "completed_at",
    "created_at",
    "updated_at",
    "job",
]

_DUE = "2026-08-10T14:00:00+00:00"


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


def _seed_open_reminder(
    path: Path, job_id: int, kind: str = "follow_up", due_at: str = _DUE
) -> int:
    """Create an open reminder through the domain service; return its id."""
    with connect(path) as connection:
        stored = add_reminder(
            connection,
            job_id=job_id,
            kind=kind,
            due_at=due_at,
            message=None,
            now=_EPOCH,
        )
    return stored.id


def _seed_completed_reminder(
    path: Path, job_id: int, kind: str = "follow_up", due_at: str = _DUE
) -> int:
    """Create a reminder completed at ``_EPOCH``; return its id."""
    reminder_id = _seed_open_reminder(path, job_id, kind, due_at)
    with connect(path) as connection:
        complete_reminder(connection, reminder_id=reminder_id, now=_EPOCH)
    return reminder_id


def _row_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _reminder_messages(path: Path) -> list[object]:
    with connect(path) as connection:
        return [
            row["message"]
            for row in connection.execute("SELECT message FROM reminders")
        ]


def _reminder_timestamps(path: Path, reminder_id: int) -> tuple[str | None, str | None]:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT completed_at, updated_at FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    assert row is not None
    return row["completed_at"], row["updated_at"]


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
# First reopen, reflection, and idempotency
# --------------------------------------------------------------------------- #


def test_first_reopen_returns_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(f"/reminders/{reminder_id}/reopen")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["reminder_id"] == reminder_id
    assert payload["job_id"] == job_ids["a-1"]
    assert payload["kind"] == "follow_up"
    assert payload["due_at"] == "2026-08-10T14:00:00+00:00"
    assert payload["completed_at"] is None
    assert "message" not in payload
    assert list(payload["job"]) == _JOB_FIELDS
    assert payload["job"]["title"] == "Senior Engineer"
    assert "description" not in payload["job"]


def test_current_get_reflects_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reopened = client.put(f"/reminders/{reminder_id}/reopen")
        detail = client.get(f"/reminders/{reminder_id}")

    assert reopened.status_code == 200
    assert detail.status_code == 200
    assert detail.json() == reopened.json()
    assert detail.json()["completed_at"] is None


def test_default_list_includes_reopened_reminder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reopened = client.put(f"/reminders/{reminder_id}/reopen")
        listing = client.get("/reminders")

    assert reopened.status_code == 200
    # The default list returns both open and completed reminders.
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0] == reopened.json()


def test_list_state_open_includes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reopened = client.put(f"/reminders/{reminder_id}/reopen")
        open_filtered = client.get("/reminders", params={"state": "open"})
        completed_filtered = client.get("/reminders", params={"state": "completed"})

    assert reopened.status_code == 200
    assert open_filtered.status_code == 200
    assert open_filtered.json()["total"] == 1
    assert open_filtered.json()["items"][0]["reminder_id"] == reminder_id
    assert open_filtered.json()["items"][0]["completed_at"] is None
    assert completed_filtered.status_code == 200
    assert completed_filtered.json()["total"] == 0


def test_first_reopen_clears_completed_at_and_moves_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    # Seeded completion froze both timestamps at _EPOCH.
    completed_before, updated_before = _reminder_timestamps(path, reminder_id)
    assert completed_before == _EPOCH.isoformat()
    assert updated_before == _EPOCH.isoformat()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(f"/reminders/{reminder_id}/reopen")

    assert response.status_code == 200
    payload = response.json()
    assert payload["completed_at"] is None
    # The domain moved updated_at to its live UTC timestamp (after _EPOCH).
    completed_after, updated_after = _reminder_timestamps(path, reminder_id)
    assert completed_after is None
    assert updated_after != _EPOCH.isoformat()
    assert updated_after == payload["updated_at"]


def test_repeated_reopen_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.put(f"/reminders/{reminder_id}/reopen")
        second = client.put(f"/reminders/{reminder_id}/reopen")
        listing = client.get("/reminders")

    assert first.status_code == 200
    assert second.status_code == 200
    # A true domain no-op: identical response body and preserved timestamps.
    assert second.json() == first.json()
    assert second.json()["completed_at"] is None
    completed, updated = _reminder_timestamps(path, reminder_id)
    assert completed is None
    assert updated == first.json()["updated_at"]
    # No additional row mutation: still exactly one reminder row.
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert _row_count(path, "reminders") == 1


def test_multiple_repeats_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.put(f"/reminders/{reminder_id}/reopen")
        second = client.put(f"/reminders/{reminder_id}/reopen")
        third = client.put(f"/reminders/{reminder_id}/reopen")

    assert first.status_code == 200
    assert second.json() == first.json()
    assert third.json() == first.json()


def test_reopening_already_open_reminder_preserves_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_open_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(f"/reminders/{reminder_id}/reopen")

    assert response.status_code == 200
    payload = response.json()
    assert payload["completed_at"] is None
    # A true no-op: timestamps are byte-identical to the seeded values.
    completed, updated = _reminder_timestamps(path, reminder_id)
    assert completed is None
    assert updated == _EPOCH.isoformat()
    assert updated == payload["updated_at"]


def test_complete_reopen_complete_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_open_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        completed_1 = client.put(f"/reminders/{reminder_id}/complete")
        reopened = client.put(f"/reminders/{reminder_id}/reopen")
        completed_2 = client.put(f"/reminders/{reminder_id}/complete")
        reopened_2 = client.put(f"/reminders/{reminder_id}/reopen")

    assert completed_1.status_code == 200
    assert completed_1.json()["completed_at"] is not None
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None
    assert completed_2.status_code == 200
    assert completed_2.json()["completed_at"] is not None
    assert reopened_2.status_code == 200
    assert reopened_2.json()["completed_at"] is None
    assert _row_count(path, "reminders") == 1


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def test_unknown_reminder_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put("/reminders/9999/reopen")

    assert response.status_code == 404
    assert response.json() == {"detail": "No reminder found with ID 9999."}


def test_non_integer_reminder_id_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put("/reminders/not-an-int/reopen")

    assert response.status_code == 422


def test_sent_request_body_ignored_and_never_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    sentinel_payload = {
        "message": "SECRET_MESSAGE",
        "note": "SECRET_NOTE",
        "profile_path": "SECRET_PROFILE_PATH",
        "description": "SECRET_DESCRIPTION",
    }
    with TestClient(app) as client:
        response = client.request(
            "PUT",
            f"/reminders/{reminder_id}/reopen",
            json=sentinel_payload,
        )
        detail = client.get(f"/reminders/{reminder_id}")

    # The route declares no body parameter: reopen depends only on the path.
    assert response.status_code == 200
    assert response.json()["completed_at"] is None
    assert detail.status_code == 200
    assert detail.json() == response.json()
    for leaked in sentinel_payload.values():
        assert leaked not in response.text
        assert leaked not in detail.text
    # No sentinel free text reached the database; the stored message stays NULL.
    assert _reminder_messages(path) == [None]


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_500_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()
    assert not lock_path_for(database_path).exists()


def test_uninitialized_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_malformed_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Locking / concurrency
# --------------------------------------------------------------------------- #


def test_lock_contention_503_unchanged_database_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.put(f"/reminders/{reminder_id}/reopen")
        # Only the approved generic message, never lock internals.
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

    # The contested PUT changed nothing and the lock is releasable.
    completed, _updated = _reminder_timestamps(path, reminder_id)
    assert completed == _EPOCH.isoformat()
    with TestClient(app) as client:
        listing = client.get("/reminders")
        recovery = client.put(f"/reminders/{reminder_id}/reopen")

    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["completed_at"] == _EPOCH.isoformat()
    assert recovery.status_code == 200
    assert recovery.json()["completed_at"] is None


def test_lock_cleanup_after_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unknown = client.put("/reminders/9999/reopen")
        valid = client.put(f"/reminders/{reminder_id}/reopen")

    assert unknown.status_code == 404
    assert valid.status_code == 200
    assert valid.json()["completed_at"] is None


def test_lock_cleanup_after_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])

    def fail_clear(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated reopen failure")

    monkeypatch.setattr("careeros.reminders.service._clear_completed_at", fail_clear)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        failed = client.put(f"/reminders/{reminder_id}/reopen")
    assert failed.status_code == 500

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The lock is releasable and a later valid request succeeds.
    with TestClient(app) as client:
        recovery = client.put(f"/reminders/{reminder_id}/reopen")
    assert recovery.status_code == 200
    assert recovery.json()["completed_at"] is None


def test_transaction_rollback_on_reopen_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])

    def fail_clear(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated reopen failure")

    monkeypatch.setattr("careeros.reminders.service._clear_completed_at", fail_clear)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.put(f"/reminders/{reminder_id}/reopen")

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The domain rolled the whole mutation back: completion stays intact.
    completed, updated = _reminder_timestamps(path, reminder_id)
    assert completed == _EPOCH.isoformat()
    assert updated == _EPOCH.isoformat()

    # A later normal reopen succeeds.
    with TestClient(app) as client:
        recovery = client.put(f"/reminders/{reminder_id}/reopen")
    assert recovery.status_code == 200
    assert recovery.json()["completed_at"] is None


# --------------------------------------------------------------------------- #
# Notification separation
# --------------------------------------------------------------------------- #


def test_reopen_creates_and_touches_no_notification_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.put(f"/reminders/{reminder_id}/reopen")
        second = client.put(f"/reminders/{reminder_id}/reopen")

    assert first.status_code == 200
    assert second.status_code == 200
    # Reopen never creates notification rows; delivery stays out of scope.
    assert _row_count(path, "reminder_notifications") == 0


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_privacy_leak_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="secret-cv-note",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=job_ids["c-1"],
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="secret-reminder-message",
            now=_EPOCH,
        )
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    responses: list = []

    # 503 while a child process holds the collection lock.
    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            responses.append(client.put(f"/reminders/{reminder_id}/reopen"))
        _release_child(child)
    finally:
        if child.poll() is None:
            _release_child(child)

    # Malformed databases are refused at startup, then return to the seeded DB.
    malformed = tmp_path / "garbage.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, malformed)
    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass

    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        responses.append(client.put(f"/reminders/{reminder_id}/reopen"))
        responses.append(
            client.request(
                "PUT",
                f"/reminders/{reminder_id}/reopen",
                json={
                    "message": "secret-body-message",
                    "note": "secret-body-note",
                    "profile_path": "secret-body-path",
                },
            )
        )
        responses.append(client.put("/reminders/9999/reopen"))
        responses.append(client.get("/reminders"))
        responses.append(client.get(f"/reminders/{reminder_id}"))

    for response in responses:
        for leaked in (
            "secret-cv-note",
            "secret-review-note",
            "secret-reminder-message",
            "secret-body-message",
            "secret-body-note",
            "secret-body-path",
            "note",
            "latest_note",
            "message",
            "description",
            "profile_path",
            "profile",
            "reasons",
            "score_components",
            "feedback",
            "conflict",
            "notification",
            "attempt",
            "backend",
            str(path),
            str(lock_path_for(path)),
            "Traceback",
            "sqlite3",
            "pid",
            "command",
        ):
            assert leaked not in response.text, (
                f"{leaked!r} leaked in {response.status_code} response"
            )


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def _exercise_sequence(client: TestClient, reminder_id: int) -> tuple:
    first = client.put(f"/reminders/{reminder_id}/reopen")
    second = client.put(f"/reminders/{reminder_id}/reopen")
    listing = client.get("/reminders")
    detail = client.get(f"/reminders/{reminder_id}")
    return (
        first.status_code,
        first.json(),
        second.status_code,
        second.json(),
        listing.status_code,
        listing.json()["total"],
        detail.status_code,
        detail.json(),
    )


def test_repeated_puts_and_reads_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    left_ids = _seed_jobs(left)
    right_ids = _seed_jobs(right)
    left_reminder = _seed_completed_reminder(left, left_ids["a-1"])
    right_reminder = _seed_completed_reminder(right, right_ids["a-1"])
    _set_database(monkeypatch, left)
    with TestClient(app) as client:
        left_result = _exercise_sequence(client, left_reminder)
    _set_database(monkeypatch, right)
    with TestClient(app) as client:
        right_result = _exercise_sequence(client, right_reminder)

    # Structurally deterministic across equivalent databases: identical shapes
    # and values except the live reopen timestamp, which the domain does not
    # freeze.
    assert left_result[0] == right_result[0] == 200
    assert left_result[2] == right_result[2] == 200
    assert left_result[4] == right_result[4] == 200
    assert left_result[5] == right_result[5] == 1
    assert left_result[6] == right_result[6] == 200
    for result in (left_result, right_result):
        assert result[1] == result[3]
        assert result[1]["kind"] == "follow_up"
        assert result[1]["due_at"] == "2026-08-10T14:00:00+00:00"
        assert result[1]["completed_at"] is None
        assert list(result[1]) == _ITEM_FIELDS
        assert result[7] == result[1]


# --------------------------------------------------------------------------- #
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="shortlist",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=job_ids["c-1"],
            kind="follow_up",
            due_at="2026-08-11T09:00:00+00:00",
            now=_EPOCH,
        )
    completed_reminder = _seed_completed_reminder(path, job_ids["a-1"])
    open_reminder = _seed_open_reminder(path, job_ids["b-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{job_ids['a-1']}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")
        applications = client.get("/applications")
        detail = client.get(f"/applications/{job_ids['a-1']}")
        history = client.get(f"/applications/{job_ids['a-1']}/history")
        application_put = client.put(
            f"/applications/{job_ids['c-1']}", json={"status": "interested"}
        )
        reminders = client.get("/reminders")
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["c-1"],
                "kind": "interview",
                "due_at": "2026-08-10T14:00:00Z",
            },
        )
        completed = client.put(f"/reminders/{open_reminder}/complete")
        reopened = client.put(f"/reminders/{completed_reminder}/reopen")
        groups = client.get("/groups")
        summary = client.get("/groups/summary")
        ranked = client.post("/ranked-jobs", json={"profile": {}})
        queue = client.post("/review-queue", json={"profile": {}})
        decisions = client.get("/review-decisions")
        decision_detail = client.get(f"/review-decisions/{job_ids['b-1']}")
        decision_put = client.put(
            f"/review-decisions/{job_ids['c-1']}", json={"decision": "shortlist"}
        )
        decision_delete = client.delete(f"/review-decisions/{job_ids['c-1']}")
        saved = client.get("/saved-searches")
        saved_detail = client.get("/saved-searches/none")
        execute_missing = client.post("/saved-searches/none/execute", json={})

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 3
    assert show.status_code == 200
    assert show.json()["title"] == "Senior Engineer"
    assert missing.status_code == 404
    assert missing.json() == {"detail": "No job found with ID 9999."}
    assert stats.status_code == 200
    assert stats.json()["total"] == len(COLLECTORS)
    assert applications.status_code == 200
    assert applications.json()["total"] == 1
    assert detail.status_code == 200
    assert detail.json()["current_status"] == "applied"
    assert history.status_code == 200
    assert history.json()["total"] == 1
    assert application_put.status_code == 200
    # Three reminders exist; the open one was completed and the completed one
    # was reopened by the new endpoint.
    assert reminders.status_code == 200
    assert reminders.json()["total"] == 3
    assert created.status_code == 200
    assert created.json()["kind"] == "interview"
    assert completed.status_code == 200
    assert completed.json()["completed_at"] is not None
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None
    assert groups.status_code == 200
    assert groups.json()["total"] == 3
    assert summary.status_code == 200
    assert summary.json()["distinct_opportunities"] == 3
    assert ranked.status_code == 200
    assert ranked.json()["total"] == 3
    assert queue.status_code == 200
    assert decisions.status_code == 200
    assert decision_detail.status_code == 200
    assert decision_detail.json()["decision"] == "shortlist"
    assert decision_put.status_code == 200
    assert decision_delete.status_code == 200
    assert saved.status_code == 200
    assert saved.json()["total"] == 0
    assert saved_detail.status_code == 404
    assert execute_missing.status_code == 404


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
