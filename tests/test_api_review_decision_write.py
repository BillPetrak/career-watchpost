"""Tests for the CareerOS review-decision write API (first mutation milestone).

Covers ``PUT /review-decisions/{job_id}``: first decision assignment returning
the canonical ``ReviewDecisionItem`` projection (exact field order and nested
job projection), current-GET reflection, history events, real transitions,
same-decision idempotency (no new event, no timestamp mutation), the full 422
validation matrix (invalid decision, malformed JSON, missing body, missing
decision, unknown fields including ``note`` — with no mutation occurring),
unknown-job 404, missing/uninitialized/malformed database 500s without any
file/parent/lock creation, cross-process lock contention mapped to a
privacy-safe 503 with lock cleanup afterwards, transaction rollback on event
write failure, a privacy leak scan over every response, prior-endpoint
regression, the shared no-``careeros.cli``-import guarantee, and deterministic
repeated behavior.
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
from careeros.reminders.service import add_reminder
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
    "decision_id",
    "job_id",
    "decision",
    "created_at",
    "updated_at",
    "job",
]

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


def _review_count(path: Path, table: str) -> int:
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
# First assignment, reflection, and history
# --------------------------------------------------------------------------- #


def test_first_assignment_returns_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["job_id"] == job_ids["a-1"]
    assert payload["decision"] == "shortlist"
    assert payload["decision_id"] > 0
    assert list(payload["job"]) == _JOB_FIELDS
    assert payload["job"]["title"] == "Senior Engineer"
    assert "description" not in payload["job"]


def test_current_get_reflects_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        put_response = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "dismiss"}
        )
        get_response = client.get(f"/review-decisions/{job_ids['a-1']}")

    assert put_response.status_code == 200
    assert get_response.status_code == 200
    assert get_response.json() == put_response.json()
    assert get_response.json()["decision"] == "dismiss"


def test_history_single_event_after_first_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert history.status_code == 200
    events = history.json()["items"]
    assert history.json()["total"] == 1
    assert list(events[0]) == _EVENT_FIELDS
    assert events[0]["old_decision"] is None
    assert events[0]["new_decision"] == "shortlist"


def test_real_transition_updates_current_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        second = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "dismiss"}
        )
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert first.status_code == 200
    assert second.status_code == 200
    assert detail.status_code == 200
    assert detail.json()["decision"] == "dismiss"
    assert detail.json()["decision_id"] == first.json()["decision_id"]

    events = history.json()["items"]
    assert history.json()["total"] == 2
    # Newest first, deterministic ordering retained.
    assert events[0]["old_decision"] == "shortlist"
    assert events[0]["new_decision"] == "dismiss"
    assert events[1]["old_decision"] is None
    assert events[1]["new_decision"] == "shortlist"


def test_same_decision_idempotent_no_new_event_no_timestamp_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        second = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert first.status_code == 200
    assert second.status_code == 200
    # The no-op must not mutate timestamps: the response body is unchanged.
    assert second.json() == first.json()
    assert history.json()["total"] == 1


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def test_invalid_decision_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "bogus"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Invalid review decision 'bogus'; expected one of: "
        "shortlist, dismiss, review_later."
    )


def test_unknown_job_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put("/review-decisions/9999", json={"decision": "shortlist"})

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_non_integer_job_id_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            "/review-decisions/not-an-int", json={"decision": "shortlist"}
        )

    assert response.status_code == 422


def test_malformed_json_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            f"/review-decisions/{job_ids['a-1']}", content="not json{{"
        )

    assert response.status_code == 422


def test_missing_body_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(f"/review-decisions/{job_ids['a-1']}")

    assert response.status_code == 422


def test_missing_decision_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(f"/review-decisions/{job_ids['a-1']}", json={})

    assert response.status_code == 422


def test_unknown_field_note_422_and_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for payload in (
            {"decision": "shortlist", "note": "secret-note"},
            {"decision": "shortlist", "bogus": 1},
        ):
            response = client.put(f"/review-decisions/{job_ids['a-1']}", json=payload)
            assert response.status_code == 422
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    # No mutation occurred: still unreviewed with an empty history.
    assert detail.status_code == 404
    assert detail.json() == {
        "detail": f"No review decision found for job {job_ids['a-1']}."
    }
    assert history.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert _review_count(path, "review_decisions") == 0
    assert _review_count(path, "review_events") == 0


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
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.put(
                f"/review-decisions/{job_ids['a-1']}",
                json={"decision": "shortlist"},
            )
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
    with TestClient(app) as client:
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        recovery = client.put(
            f"/review-decisions/{job_ids['a-1']}",
            json={"decision": "shortlist"},
        )

    assert detail.status_code == 404
    assert recovery.status_code == 200
    assert recovery.json()["decision"] == "shortlist"
    assert _review_count(path, "review_events") == 1


def test_lock_cleanup_after_endpoint_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    # Trigger domain validation and unknown-job exceptions while the lock is
    # held; the lock must be released so the next request succeeds.
    with TestClient(app) as client:
        invalid = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "bogus"}
        )
        unknown = client.put("/review-decisions/9999", json={"decision": "shortlist"})
        valid = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )

    assert invalid.status_code == 422
    assert unknown.status_code == 404
    assert valid.status_code == 200


def test_transaction_rollback_on_event_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)

    def fail_event(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated event failure")

    monkeypatch.setattr("careeros.review.service.append_event", fail_event)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The whole mutation rolled back: current state and history untouched.
    assert _review_count(path, "review_decisions") == 0
    assert _review_count(path, "review_events") == 0

    # The lock is releasable and a later valid request succeeds.
    with TestClient(app) as client:
        recovery = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
    assert recovery.status_code == 200
    assert recovery.json()["decision"] == "shortlist"
    assert _review_count(path, "review_events") == 1


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
        responses.append(
            client.put(
                f"/review-decisions/{job_ids['a-1']}",
                json={"decision": "shortlist"},
            )
        )
        responses.append(
            client.put(
                f"/review-decisions/{job_ids['b-1']}",
                json={"decision": "dismiss"},
            )
        )
        responses.append(client.get("/review-decisions"))
        responses.append(client.get(f"/review-decisions/{job_ids['a-1']}"))
        responses.append(client.get(f"/review-decisions/{job_ids['b-1']}/history"))
        responses.append(
            client.put(
                f"/review-decisions/{job_ids['a-1']}", json={"decision": "bogus"}
            )
        )
        responses.append(client.put("/review-decisions/9999", json={"decision": "x"}))

    for response in responses:
        for leaked in (
            "secret-review-note",
            "secret-cv-note",
            "secret-reminder-message",
            "note",
            "latest_note",
            "message",
            "description",
            "reasons",
            "score_components",
            "conflict",
            "feedback",
            "profile",
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


def test_repeated_put_and_reads_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_put = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        first_get = client.get(f"/review-decisions/{job_ids['a-1']}")
        first_history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

        second_put = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        second_get = client.get(f"/review-decisions/{job_ids['a-1']}")
        second_history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert first_put.status_code == 200
    assert first_get.status_code == 200
    assert first_history.status_code == 200

    assert second_put.json() == first_put.json()
    assert second_get.json() == first_get.json()
    assert second_history.json() == first_history.json()
    assert first_get.json() == first_put.json()
    assert first_history.json()["total"] == 1


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
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{job_ids['a-1']}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")
        applications = client.get("/applications")
        reminders = client.get("/reminders")
        groups = client.get("/groups")
        summary = client.get("/groups/summary")
        ranked = client.post("/ranked-jobs", json={"profile": _EMPTY_PROFILE})
        queue = client.post("/review-queue", json={"profile": _EMPTY_PROFILE})
        decisions = client.get("/review-decisions")
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

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
    assert applications.json()["total"] == 0
    assert reminders.status_code == 200
    assert reminders.json()["total"] == 0
    assert groups.status_code == 200
    assert groups.json()["total"] == 3
    assert summary.status_code == 200
    assert summary.json()["distinct_opportunities"] == 3
    assert ranked.status_code == 200
    assert ranked.json()["total"] == 3
    # Two of the three jobs are decided, so only c-1 is unreviewed.
    assert queue.status_code == 200
    assert queue.json()["total"] == 1
    assert decisions.status_code == 200
    assert decisions.json()["total"] == 2
    assert detail.status_code == 200
    assert detail.json()["decision"] == "shortlist"
    assert history.status_code == 200
    assert history.json()["total"] == 1


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
