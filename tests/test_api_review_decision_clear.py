"""Tests for the CareerOS review-decision clear write API.

Covers ``DELETE /review-decisions/{job_id}``: a successful clear returning the
exact ``ClearReviewDecisionResponse`` field order (``job_id``, ``changed``),
current-GET reflection (canonical no-current-decision 404), history integration
(the clear is the newest event with ``old_decision`` set, ``new_decision``
``null``, and no note in the API output), review-queue re-eligibility, first and
repeated idempotent no-ops (``changed=false``, no new event, no row mutation),
never-reviewed existing jobs, unknown-job 404, non-integer path 422, a sent
request body being ignored with nothing stored, missing/uninitialized/malformed
database 500s without any file/parent/lock creation, cross-process lock
contention mapped to a privacy-safe 503 with lock cleanup and recovery, lock
release after 404 and 500, transaction rollback on event-write failure, a
privacy leak scan over every response, deterministic repeated behavior,
prior-endpoint regression, and the shared no-``careeros.cli`` import guarantee.
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
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import lock_path_for
from careeros.reminders.service import add_reminder
from careeros.review.service import clear_decision, set_decision

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_LATER = datetime.fromisoformat("2026-08-03T13:00:00+00:00")

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

_ITEM_FIELDS = ["job_id", "changed"]

_EVENT_FIELDS = [
    "event_id",
    "job_id",
    "old_decision",
    "new_decision",
    "created_at",
]

_EMPTY_PROFILE = {}


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


def _seed_decided(path: Path) -> dict[str, int]:
    """Seed three jobs with a decision on ``a-1`` only."""
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            now=_EPOCH,
        )
    return job_ids


def _review_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _current_decision(path: Path, job_id: int) -> str | None:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT decision FROM review_decisions WHERE job_id = ?", (job_id,)
        ).fetchone()
    if row is None:
        return None
    return row["decision"]


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
# Successful clear, reflection, and history
# --------------------------------------------------------------------------- #


def test_successful_clear_returns_changed_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete(f"/review-decisions/{job_ids['a-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload == {"job_id": job_ids["a-1"], "changed": True}
    assert _current_decision(path, job_ids["a-1"]) is None


def test_current_get_reflects_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        delete_response = client.delete(f"/review-decisions/{job_ids['a-1']}")
        get_response = client.get(f"/review-decisions/{job_ids['a-1']}")

    assert delete_response.status_code == 200
    assert get_response.status_code == 404
    assert get_response.json() == {
        "detail": f"No review decision found for job {job_ids['a-1']}."
    }


def test_history_shows_clear_event_as_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        client.delete(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert history.status_code == 200
    events = history.json()["items"]
    assert history.json()["total"] == 2
    assert list(events[0]) == _EVENT_FIELDS
    assert events[0]["old_decision"] == "shortlist"
    assert events[0]["new_decision"] is None
    assert "note" not in events[0]
    assert events[1]["old_decision"] is None
    assert events[1]["new_decision"] == "shortlist"


def test_review_queue_eligibility_after_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="dismiss",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        before = client.post("/review-queue", json={"profile": _EMPTY_PROFILE})
        deleted = client.delete(f"/review-decisions/{job_ids['a-1']}")
        after = client.post("/review-queue", json={"profile": _EMPTY_PROFILE})

    assert deleted.status_code == 200
    assert deleted.json()["changed"] is True
    # Only the unreviewed job remains while a-1 is decided.
    assert before.status_code == 200
    assert before.json()["total"] == 1
    # After clearing, a-1 becomes eligible again alongside the never-reviewed job.
    assert after.status_code == 200
    assert after.json()["total"] == 2
    assert [item["review_decision"] for item in after.json()["items"]] == [
        None,
        None,
    ]
    assert [
        item["group"]["representative"]["id"] for item in after.json()["items"]
    ] == [
        job_ids["a-1"],
        job_ids["c-1"],
    ]


# --------------------------------------------------------------------------- #
# Idempotency: repeats and never-reviewed jobs
# --------------------------------------------------------------------------- #


def test_first_repeat_is_idempotent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.delete(f"/review-decisions/{job_ids['a-1']}")
        second = client.delete(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert first.status_code == 200
    assert first.json() == {"job_id": job_ids["a-1"], "changed": True}
    assert second.status_code == 200
    assert second.json() == {"job_id": job_ids["a-1"], "changed": False}
    # The no-op appends no additional event; the clear event is the only new one.
    assert history.json()["total"] == 2
    assert _review_count(path, "review_decisions") == 0
    assert _review_count(path, "review_events") == 2


def test_multiple_repeats_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.delete(f"/review-decisions/{job_ids['a-1']}")
        second = client.delete(f"/review-decisions/{job_ids['a-1']}")
        third = client.delete(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert first.json() == {"job_id": job_ids["a-1"], "changed": True}
    assert second.json() == {"job_id": job_ids["a-1"], "changed": False}
    assert third.json() == {"job_id": job_ids["a-1"], "changed": False}
    assert history.json()["total"] == 2
    assert _review_count(path, "review_events") == 2
    assert _review_count(path, "review_decisions") == 0


def test_never_reviewed_job_is_idempotent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete(f"/review-decisions/{job_ids['c-1']}")

    assert response.status_code == 200
    assert response.json() == {"job_id": job_ids["c-1"], "changed": False}
    assert _review_count(path, "review_events") == 1
    assert _review_count(path, "review_decisions") == 1


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def test_unknown_job_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/review-decisions/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_non_integer_job_id_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete("/review-decisions/not-an-int")

    assert response.status_code == 422


def test_request_body_ignored_and_never_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    sentinel_payload = {
        "note": "PRIVATE_SENTINEL",
        "bogus": "OTHER_SENTINEL",
    }
    with TestClient(app) as client:
        response = client.request(
            "DELETE", f"/review-decisions/{job_ids['a-1']}", json=sentinel_payload
        )
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    # The route declares no body parameter: semantics depend only on job_id.
    assert response.status_code == 200
    assert response.json() == {"job_id": job_ids["a-1"], "changed": True}
    assert detail.status_code == 404
    # No sentinel content was stored and no review-event note carries it.
    for leaked in ("PRIVATE_SENTINEL", "OTHER_SENTINEL", "note", "bogus"):
        assert leaked not in response.text
        assert leaked not in history.text
    with connect(path) as connection:
        notes = connection.execute(
            "SELECT note FROM review_events WHERE job_id = ?",
            (job_ids["a-1"],),
        ).fetchall()
    assert [row["note"] for row in notes] == [None, None]


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_500_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.delete("/review-decisions/1")

    assert response.status_code == 500
    assert response.json() == {
        "detail": (
            "The database schema is unavailable (run 'careeros collect "
            "<source>' to initialize it)."
        )
    }
    assert not database_path.exists()
    assert not database_path.parent.exists()
    assert not lock_path_for(database_path).exists()


def test_uninitialized_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.delete("/review-decisions/1")

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


def test_malformed_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.delete("/review-decisions/1")

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Locking / concurrency
# --------------------------------------------------------------------------- #


def test_lock_contention_503_unchanged_database_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.delete(f"/review-decisions/{job_ids['a-1']}")
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
    with TestClient(app) as client:
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        recovery = client.delete(f"/review-decisions/{job_ids['a-1']}")

    assert detail.status_code == 200
    assert recovery.status_code == 200
    assert recovery.json()["changed"] is True
    assert _review_count(path, "review_events") == 2


def test_lock_cleanup_after_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unknown = client.delete("/review-decisions/9999")
        valid = client.delete(f"/review-decisions/{job_ids['a-1']}")

    assert unknown.status_code == 404
    assert valid.status_code == 200
    assert valid.json()["changed"] is True


def test_lock_cleanup_after_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)

    def fail_event(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated event failure")

    monkeypatch.setattr("careeros.review.service.append_event", fail_event)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        failed = client.delete(f"/review-decisions/{job_ids['a-1']}")
    assert failed.status_code == 500

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The lock is releasable and a later valid request succeeds.
    with TestClient(app) as client:
        recovery = client.delete(f"/review-decisions/{job_ids['a-1']}")
    assert recovery.status_code == 200
    assert recovery.json()["changed"] is True


def test_transaction_rollback_on_event_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_decided(path)

    def fail_event(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated event failure")

    monkeypatch.setattr("careeros.review.service.append_event", fail_event)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.delete(f"/review-decisions/{job_ids['a-1']}")

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The whole mutation rolled back: the decision row and history are untouched.
    assert _current_decision(path, job_ids["a-1"]) == "shortlist"
    assert _review_count(path, "review_decisions") == 1
    assert _review_count(path, "review_events") == 1

    # A later normal DELETE succeeds.
    with TestClient(app) as client:
        recovery = client.delete(f"/review-decisions/{job_ids['a-1']}")
    assert recovery.status_code == 200
    assert recovery.json()["changed"] is True
    assert _current_decision(path, job_ids["a-1"]) is None
    assert _review_count(path, "review_events") == 2


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_privacy_leak_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="dismiss",
            note="secret-review-note-b",
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["b-1"],
            status="applied",
            note="secret-cv-note",
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
    _set_database(monkeypatch, path)

    responses = []
    with TestClient(app) as client:
        responses.append(client.delete(f"/review-decisions/{job_ids['a-1']}"))
        responses.append(client.get(f"/review-decisions/{job_ids['a-1']}"))
        responses.append(client.get(f"/review-decisions/{job_ids['a-1']}/history"))
        responses.append(client.get(f"/review-decisions/{job_ids['b-1']}"))
        responses.append(client.get(f"/review-decisions/{job_ids['b-1']}/history"))
        responses.append(client.get("/review-decisions"))
        responses.append(client.post("/review-queue", json={"profile": _EMPTY_PROFILE}))
        responses.append(client.delete("/review-decisions/9999"))
        responses.append(
            client.request(
                "DELETE",
                f"/review-decisions/{job_ids['b-1']}",
                json={"note": "secret-body-note"},
            )
        )

    for response in responses:
        for leaked in (
            "secret-review-note",
            "secret-review-note-b",
            "secret-cv-note",
            "secret-reminder-message",
            "secret-body-note",
            "note",
            "latest_note",
            "message",
            "description",
            "profile_path",
            "score_components",
            "reasons",
            "feedback",
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


def _exercise_sequence(client: TestClient, job_id: int) -> tuple:
    delete_one = client.delete(f"/review-decisions/{job_id}")
    delete_two = client.delete(f"/review-decisions/{job_id}")
    detail = client.get(f"/review-decisions/{job_id}")
    history = client.get(f"/review-decisions/{job_id}/history")
    return (
        delete_one.status_code,
        delete_one.json(),
        delete_two.status_code,
        delete_two.json(),
        detail.status_code,
        detail.json(),
        history.status_code,
        history.json()["total"],
    )


def test_repeated_delete_and_reads_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    left = tmp_path / "left.sqlite3"
    right = tmp_path / "right.sqlite3"
    left_ids = _seed_decided(left)
    right_ids = _seed_decided(right)
    _set_database(monkeypatch, left)
    with TestClient(app) as client:
        left_result = _exercise_sequence(client, left_ids["a-1"])
    _set_database(monkeypatch, right)
    with TestClient(app) as client:
        right_result = _exercise_sequence(client, right_ids["a-1"])

    assert left_ids["a-1"] == right_ids["a-1"]
    assert left_result == right_result
    assert left_result[1] == {"job_id": left_ids["a-1"], "changed": True}
    assert left_result[3] == {"job_id": left_ids["a-1"], "changed": False}
    assert left_result[4] == 404
    assert left_result[7] == 2


# --------------------------------------------------------------------------- #
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="dismiss",
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH,
        )
        clear_decision(
            connection,
            job_id=job_ids["b-1"],
            now=_LATER,
        )
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
        groups = client.get("/groups")
        summary = client.get("/groups/summary")
        ranked = client.post("/ranked-jobs", json={"profile": {}})
        queue = client.post("/review-queue", json={"profile": {}})
        decisions = client.get("/review-decisions")
        decision_detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        decision_history = client.get(f"/review-decisions/{job_ids['a-1']}/history")
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
    assert reminders.status_code == 200
    assert reminders.json()["total"] == 0
    assert groups.status_code == 200
    assert groups.json()["total"] == 3
    assert summary.status_code == 200
    assert summary.json()["distinct_opportunities"] == 3
    assert ranked.status_code == 200
    assert ranked.json()["total"] == 3
    # a-1 is tracked and b-1 is cleared, so only c-1 is unreviewed+untracked.
    assert queue.status_code == 200
    assert queue.json()["total"] == 1
    assert decisions.status_code == 200
    assert decisions.json()["total"] == 1
    assert decision_detail.status_code == 200
    assert decision_detail.json()["decision"] == "shortlist"
    assert decision_history.status_code == 200
    assert decision_history.json()["total"] == 1
    assert decision_put.status_code == 200
    assert decision_delete.status_code == 200
    assert decision_delete.json() == {
        "job_id": job_ids["c-1"],
        "changed": True,
    }
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
