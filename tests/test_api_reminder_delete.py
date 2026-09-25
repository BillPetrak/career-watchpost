"""Tests for the CareerOS reminder-deletion write API.

Covers ``DELETE /reminders/{reminder_id}``: a first successful deletion
returning the canonical ``ReminderItem`` projection of the deleted reminder
(exact field order, echoed ``completed_at``, nested canonical job projection,
message structurally absent), current-state reflection through ``GET /reminders``
and ``GET /reminders/{reminder_id}`` (a canonical 404 after deletion) and the
state filters, a repeated DELETE being a canonical 404 (the resource is gone;
no synthetic ``changed`` flag, no 204), deletion of a completed reminder
preserving its pre-delete ``completed_at``, adjacent lifecycle round trips
(create -> complete -> delete, reopen no-op -> delete), unknown-reminder 404,
non-integer path 422, a sent request body being ignored with nothing stored,
missing/uninitialized/malformed database 500s without any file/parent/lock
creation, cross-process lock contention mapped to a privacy-safe 503 with lock
cleanup and recovery afterwards, lock release after 404 and 500, transaction
rollback on the delete write failing, the pre-existing ``ON DELETE RESTRICT``
constraint on ``reminder_notifications`` surfacing as a privacy-safe 500 with
both rows preserved, notification separation (deletion never creates
``reminder_notifications`` rows), a privacy leak scan over every response,
deterministic structural behavior, prior-endpoint regression, and the shared
no-``careeros.cli`` import guarantee.
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
from careeros.reminders.service import (
    add_reminder,
    complete_reminder,
)
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


def _payload(
    job_id: int,
    kind: str = "follow_up",
    due_at: str = _DUE,
) -> dict[str, object]:
    return {"job_id": job_id, "kind": kind, "due_at": due_at}


def _seed_reminder(
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
    """Create an open reminder and complete it; return its id."""
    reminder_id = _seed_reminder(path, job_id, kind=kind, due_at=due_at)
    with connect(path) as connection:
        complete_reminder(connection, reminder_id=reminder_id, now=_EPOCH)
    return reminder_id


def _row_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _reminder_ids(path: Path) -> list[int]:
    with connect(path) as connection:
        return [
            int(row["id"])
            for row in connection.execute("SELECT id FROM reminders ORDER BY id")
        ]


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
# First deletion, reflection, and repeated-DELETE semantics
# --------------------------------------------------------------------------- #


def test_first_delete_returns_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete(f"/reminders/{reminder_id}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["reminder_id"] == reminder_id
    assert payload["job_id"] == job_ids["a-1"]
    assert payload["kind"] == "follow_up"
    assert payload["due_at"] == _DUE
    assert payload["completed_at"] is None
    assert payload["created_at"] == _EPOCH.isoformat()
    assert payload["updated_at"] == _EPOCH.isoformat()
    assert "message" not in payload
    assert list(payload["job"]) == _JOB_FIELDS
    assert payload["job"]["title"] == "Senior Engineer"
    assert "description" not in payload["job"]
    # The deleted reminder row is gone.
    assert _reminder_ids(path) == []


def test_current_get_reflects_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        deleted = client.delete(f"/reminders/{reminder_id}")
        detail = client.get(f"/reminders/{reminder_id}")
        listing = client.get("/reminders")

    assert deleted.status_code == 200
    # After deletion the resource no longer exists: canonical 404.
    assert detail.status_code == 404
    assert detail.json() == {"detail": f"No reminder found with ID {reminder_id}."}
    assert listing.status_code == 200
    assert listing.json()["total"] == 0
    assert listing.json()["items"] == []


def test_list_and_state_filters_exclude_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    open_id = _seed_reminder(path, job_ids["a-1"])
    completed_id = _seed_completed_reminder(path, job_ids["b-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        deleted = client.delete(f"/reminders/{completed_id}")
        all_listing = client.get("/reminders")
        open_listing = client.get("/reminders", params={"state": "open"})
        completed_listing = client.get("/reminders", params={"state": "completed"})

    assert deleted.status_code == 200
    assert all_listing.status_code == 200
    assert all_listing.json()["total"] == 1
    assert all_listing.json()["items"][0]["reminder_id"] == open_id
    assert open_listing.status_code == 200
    assert open_listing.json()["total"] == 1
    assert open_listing.json()["items"][0]["reminder_id"] == open_id
    assert completed_listing.status_code == 200
    assert completed_listing.json()["total"] == 0


def test_repeated_delete_second_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.delete(f"/reminders/{reminder_id}")
        second = client.delete(f"/reminders/{reminder_id}")

    # First DELETE removes the reminder; the second is a canonical 404 because
    # the resource no longer exists. No synthetic changed flag, no 204, and no
    # 200 no-op: the reminder row is gone after the first request.
    assert first.status_code == 200
    assert first.json()["reminder_id"] == reminder_id
    assert second.status_code == 404
    assert second.json() == {"detail": f"No reminder found with ID {reminder_id}."}
    assert _reminder_ids(path) == []


def test_delete_completed_reminder_preserves_completed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_completed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete(f"/reminders/{reminder_id}")

    # Deleting a completed reminder is allowed and the returned representation
    # preserves its pre-delete completion timestamp.
    assert response.status_code == 200
    payload = response.json()
    assert payload["reminder_id"] == reminder_id
    assert payload["completed_at"] == _EPOCH.isoformat()
    assert payload["updated_at"] == _EPOCH.isoformat()
    assert _reminder_ids(path) == []


def test_lifecycle_round_trip_create_complete_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post("/reminders", json=_payload(job_ids["a-1"]))
        reminder_id = created.json()["reminder_id"]
        completed = client.put(f"/reminders/{reminder_id}/complete")
        deleted = client.delete(f"/reminders/{reminder_id}")
        final_detail = client.get(f"/reminders/{reminder_id}")

    assert created.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["completed_at"] is not None
    assert deleted.status_code == 200
    assert deleted.json()["reminder_id"] == reminder_id
    assert deleted.json()["completed_at"] is not None
    assert final_detail.status_code == 404
    assert _reminder_ids(path) == []


def test_lifecycle_round_trip_reopen_noop_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reopened = client.put(f"/reminders/{reminder_id}/reopen")
        deleted = client.delete(f"/reminders/{reminder_id}")
        final_detail = client.get(f"/reminders/{reminder_id}")

    # Reopening an already-open reminder is an idempotent no-op; deleting then
    # removes the reminder and the final GET is a canonical 404.
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None
    assert deleted.status_code == 200
    assert deleted.json()["completed_at"] is None
    assert final_detail.status_code == 404
    assert _reminder_ids(path) == []


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def test_unknown_reminder_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/reminders/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "No reminder found with ID 9999."}
    assert _reminder_ids(path) == []


def test_non_integer_reminder_id_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/reminders/not-an-int")

    assert response.status_code == 422


def test_sent_request_body_ignored_and_never_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    sentinel_payload = {
        "message": "SECRET_MESSAGE",
        "note": "SECRET_NOTE",
        "profile_path": "SECRET_PROFILE_PATH",
        "description": "SECRET_DESCRIPTION",
    }
    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            f"/reminders/{reminder_id}",
            json=sentinel_payload,
        )
        detail = client.get(f"/reminders/{reminder_id}")

    # The route declares no body parameter: deletion depends only on the path
    # and the sent body is never parsed, stored, logged, or echoed.
    assert response.status_code == 200
    assert response.json()["reminder_id"] == reminder_id
    assert detail.status_code == 404
    for leaked in sentinel_payload.values():
        assert leaked not in response.text
        assert leaked not in detail.text
    assert _reminder_ids(path) == []


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_500_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v13"):
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
    with pytest.raises(ValueError, match="current schema v13"):
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
    with pytest.raises(ValueError, match="current schema v13"):
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
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.delete(f"/reminders/{reminder_id}")
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

    # The contested DELETE changed nothing and the lock is releasable.
    assert _reminder_ids(path) == [reminder_id]
    with TestClient(app) as client:
        listing = client.get("/reminders")
        recovery = client.delete(f"/reminders/{reminder_id}")

    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert recovery.status_code == 200
    assert recovery.json()["reminder_id"] == reminder_id
    assert _reminder_ids(path) == []


def test_lock_cleanup_after_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unknown = client.delete("/reminders/9999")
        valid = client.delete(f"/reminders/{reminder_id}")

    assert unknown.status_code == 404
    assert valid.status_code == 200
    assert _reminder_ids(path) == []


def test_lock_cleanup_after_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])

    def fail_delete(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated deletion failure")

    monkeypatch.setattr("careeros.reminders.service._delete_reminder", fail_delete)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        failed = client.delete(f"/reminders/{reminder_id}")
    assert failed.status_code == 500

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The lock is releasable and a later valid request succeeds.
    with TestClient(app) as client:
        recovery = client.delete(f"/reminders/{reminder_id}")
    assert recovery.status_code == 200
    assert _reminder_ids(path) == []


def test_transaction_rollback_on_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])

    def fail_delete(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated deletion failure")

    monkeypatch.setattr("careeros.reminders.service._delete_reminder", fail_delete)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.delete(f"/reminders/{reminder_id}")

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The domain rolled the whole mutation back: no partial deletion survives.
    assert _reminder_ids(path) == [reminder_id]

    # A later normal deletion succeeds.
    with TestClient(app) as client:
        recovery = client.delete(f"/reminders/{reminder_id}")
    assert recovery.status_code == 200
    assert _reminder_ids(path) == []


# --------------------------------------------------------------------------- #
# ON DELETE RESTRICT (reminder_notifications) and notification separation
# --------------------------------------------------------------------------- #


def test_on_delete_restrict_notification_veto_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reminder referenced by reminder_notifications is protected by the
    # existing ON DELETE RESTRICT foreign key (schema v8). This is a
    # pre-existing domain constraint, not a new HTTP status: the delete
    # attempt raises sqlite3.IntegrityError, the domain service rolls back,
    # and the app-level sqlite3.Error handler returns the canonical
    # privacy-safe 500. Both rows survive; no partial mutation occurs.
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    with connect(path) as connection:
        connection.execute(
            """
            INSERT INTO reminder_notifications (
                reminder_id, scheduled_due_at, attempted_at, status, backend
            )
            VALUES (?, ?, ?, 'attempted', 'test')
            """,
            (reminder_id, _DUE, _EPOCH.isoformat()),
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete(f"/reminders/{reminder_id}")

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )
    for leaked in ("FOREIGN KEY", "IntegrityError", "constraint", "sqlite3"):
        assert leaked not in response.text
    # No partial mutation: the reminder and its notification row both survive.
    assert _reminder_ids(path) == [reminder_id]
    assert _row_count(path, "reminder_notifications") == 1


def test_delete_creates_no_notification_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.delete(f"/reminders/{reminder_id}")
        second = client.delete(f"/reminders/{reminder_id}")

    assert first.status_code == 200
    assert second.status_code == 404
    # Deletion never creates or touches notification rows.
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
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    responses: list = []

    # 503 while a child process holds the collection lock.
    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            responses.append(client.delete(f"/reminders/{reminder_id}"))
        _release_child(child)
    finally:
        if child.poll() is None:
            _release_child(child)

    # Malformed databases are refused at startup, then return to the seeded DB.
    malformed = tmp_path / "garbage.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, malformed)
    with pytest.raises(ValueError, match="current schema v13"):
        with TestClient(app):
            pass

    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        responses.append(client.delete(f"/reminders/{reminder_id}"))
        responses.append(
            client.request(
                "DELETE",
                f"/reminders/{reminder_id}",
                json={
                    "message": "secret-body-message",
                    "note": "secret-body-note",
                    "profile_path": "secret-body-path",
                },
            )
        )
        responses.append(client.delete("/reminders/9999"))
        responses.append(client.delete("/reminders/not-an-int"))
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
    first = client.delete(f"/reminders/{reminder_id}")
    second = client.delete(f"/reminders/{reminder_id}")
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


def test_repeated_deletes_and_reads_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    left_ids = _seed_jobs(left)
    right_ids = _seed_jobs(right)
    left_reminder = _seed_reminder(left, left_ids["a-1"])
    right_reminder = _seed_reminder(right, right_ids["a-1"])
    _set_database(monkeypatch, left)
    with TestClient(app) as client:
        left_result = _exercise_sequence(client, left_reminder)
    _set_database(monkeypatch, right)
    with TestClient(app) as client:
        right_result = _exercise_sequence(client, right_reminder)

    # Structurally deterministic across equivalent databases: identical shapes
    # and semantics except the naturally generated reminder id.
    assert left_result[0] == right_result[0] == 200
    assert left_result[2] == right_result[2] == 404
    assert left_result[4] == right_result[4] == 200
    assert left_result[5] == right_result[5] == 0
    assert left_result[6] == right_result[6] == 404
    for result in (left_result, right_result):
        reminder_id = result[1]["reminder_id"]
        assert result[1]["kind"] == "follow_up"
        assert result[1]["due_at"] == _DUE
        assert result[1]["completed_at"] is None
        assert list(result[1]) == _ITEM_FIELDS
        assert result[3] == {"detail": f"No reminder found with ID {reminder_id}."}
        assert result[7] == result[3]


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
    open_reminder = _seed_reminder(path, job_ids["a-1"])
    completed_reminder = _seed_completed_reminder(path, job_ids["b-1"])
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
        completed = client.put(f"/reminders/{completed_reminder}/complete")
        reopened = client.put(f"/reminders/{open_reminder}/reopen")
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
    # Three reminders exist; the completed one was completed (no-op) and the
    # open one was reopened (no-op) by the prior endpoints.
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
