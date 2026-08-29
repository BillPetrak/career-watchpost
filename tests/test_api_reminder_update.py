"""Tests for the CareerOS reminder-update write API.

Covers ``PATCH /reminders/{reminder_id}``: a genuine partial update that
modifies only ``kind`` and/or ``due_at`` (at least one non-null field is
required; an empty body, null-only payloads, and unknown fields including
``message``/``note``/``description``/``profile_path``/``clear_message`` are
all ``422``), the canonical ``ReminderItem`` projection (exact field order,
``due_at`` normalized to UTC per the shared domain rules, ``Z`` accepted,
nested canonical job projection, message structurally absent), current-state
reflection through ``GET /reminders`` and ``GET /reminders/{reminder_id}``,
idempotency (re-PATCHing identical values issues no domain ``UPDATE`` and
preserves ``updated_at``), CLI/domain-seeded message retention (the stored
message survives HTTP updates byte-identically and is never exposed),
adjacent lifecycle round trips (create -> complete -> reopen -> update ->
delete), unknown-reminder 404, non-integer path 422, missing/malformed body
and empty/null/unknown-field bodies, missing/uninitialized/malformed database
500s without any file/parent/lock creation, cross-process lock contention
mapped to a privacy-safe 503 with lock cleanup and recovery afterwards,
transaction rollback on the update write failing, notification separation
(update never creates ``reminder_notifications`` rows), a privacy leak scan
over every response, deterministic structural behavior across equivalent
databases, prior-endpoint regression, and the shared no-``careeros.cli``
import guarantee.
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
_NEW_DUE = "2026-08-11T09:00:00+00:00"


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


def _seed_reminder(
    path: Path,
    job_id: int,
    kind: str = "follow_up",
    due_at: str = _DUE,
    message: str | None = None,
) -> int:
    """Create an open reminder through the domain service; return its id."""
    with connect(path) as connection:
        stored = add_reminder(
            connection,
            job_id=job_id,
            kind=kind,
            due_at=due_at,
            message=message,
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


def _reminder_row(path: Path, reminder_id: int) -> dict[str, object]:
    with connect(path) as connection:
        row = connection.execute(
            "SELECT kind, message, due_at, updated_at FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


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
# Successful partial updates and reflection
# --------------------------------------------------------------------------- #


def test_update_both_kind_and_due_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            f"/reminders/{reminder_id}",
            json={"kind": "interview", "due_at": _NEW_DUE},
        )
        detail = client.get(f"/reminders/{reminder_id}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["reminder_id"] == reminder_id
    assert payload["job_id"] == job_ids["a-1"]
    assert payload["kind"] == "interview"
    assert payload["due_at"] == _NEW_DUE
    assert payload["completed_at"] is None
    assert payload["created_at"] == _EPOCH.isoformat()
    assert payload["updated_at"] != _EPOCH.isoformat()
    assert "message" not in payload
    assert list(payload["job"]) == _JOB_FIELDS
    assert payload["job"]["title"] == "Senior Engineer"
    assert "description" not in payload["job"]
    # Both mutable fields persisted and are reflected by the detail GET.
    assert _reminder_row(path, reminder_id)["kind"] == "interview"
    assert _reminder_row(path, reminder_id)["due_at"] == _NEW_DUE
    assert detail.status_code == 200
    assert detail.json()["kind"] == "interview"
    assert detail.json()["due_at"] == _NEW_DUE


def test_update_kind_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
        detail = client.get(f"/reminders/{reminder_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "offer"
    assert payload["due_at"] == _DUE
    assert payload["created_at"] == _EPOCH.isoformat()
    assert payload["updated_at"] != _EPOCH.isoformat()
    assert detail.json()["kind"] == "offer"
    assert detail.json()["due_at"] == _DUE


def test_update_due_at_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"], kind="interview")
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={"due_at": _NEW_DUE})
        detail = client.get(f"/reminders/{reminder_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "interview"
    assert payload["due_at"] == _NEW_DUE
    assert detail.json()["kind"] == "interview"
    assert detail.json()["due_at"] == _NEW_DUE


def test_due_at_explicit_offset_is_normalized_to_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            f"/reminders/{reminder_id}", json={"due_at": "2026-08-10T16:00:00+02:00"}
        )

    assert response.status_code == 200
    assert response.json()["due_at"] == _DUE
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE


def test_due_at_z_suffix_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            f"/reminders/{reminder_id}", json={"due_at": "2026-08-10T14:00:00Z"}
        )

    assert response.status_code == 200
    assert response.json()["due_at"] == _DUE


# --------------------------------------------------------------------------- #
# Domain validation (kind / due_at)
# --------------------------------------------------------------------------- #


def test_invalid_kind_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={"kind": "bogus"})

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Invalid reminder kind 'bogus'; expected one of: follow_up, interview, "
        "offer, review, custom."
    )
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE


@pytest.mark.parametrize(
    ("due_at", "detail"),
    [
        (
            "2026-08-10T14:00:00",
            "Invalid due timestamp '2026-08-10T14:00:00': a naive timestamp "
            "without an explicit UTC offset is not accepted (CareerOS never "
            "guesses a timezone).",
        ),
        (
            "2026-08-10",
            "Due timestamp must include a time component; date-only values are "
            "not accepted (CareerOS never picks a midnight or a timezone).",
        ),
        (
            "",
            "Due timestamp must be a non-empty ISO-8601 string.",
        ),
        (
            "2026-08-10T14:00:00+99:99",
            "Invalid due timestamp '2026-08-10T14:00:00+99:99'; expected "
            "ISO-8601 with an explicit UTC offset, e.g. "
            "2026-08-10T14:00:00+00:00 or 2026-08-10T14:00:00Z.",
        ),
    ],
)
def test_invalid_due_at_cases_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    due_at: str,
    detail: str,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={"due_at": due_at})

    assert response.status_code == 422
    assert response.json()["detail"] == detail
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE


# --------------------------------------------------------------------------- #
# Schema-level validation (at-least-one rule, unknown fields)
# --------------------------------------------------------------------------- #


def test_empty_payload_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={})

    assert response.status_code == 422
    assert "At least one of 'kind' or 'due_at' must be provided." in response.text
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE


@pytest.mark.parametrize(
    "payload",
    [{"kind": None}, {"due_at": None}, {"kind": None, "due_at": None}],
)
def test_null_only_payloads_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json=payload)

    assert response.status_code == 422
    assert "At least one of 'kind' or 'due_at' must be provided." in response.text
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE


@pytest.mark.parametrize(
    "field",
    [
        "message",
        "note",
        "description",
        "profile_path",
        "clear_message",
        "bogus",
    ],
)
def test_unknown_fields_rejected_422_no_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={field: "x"})

    assert response.status_code == 422
    # Zero mutation: the stored reminder is untouched.
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE
    assert _reminder_row(path, reminder_id)["updated_at"] == _EPOCH.isoformat()


# --------------------------------------------------------------------------- #
# Message retention / privacy of the stored free text
# --------------------------------------------------------------------------- #


def test_stored_message_survives_update_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    message = "Follow up about the interview panel."
    reminder_id = _seed_reminder(path, job_ids["a-1"], message=message)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(
            f"/reminders/{reminder_id}",
            json={"kind": "interview", "due_at": _NEW_DUE},
        )
        detail = client.get(f"/reminders/{reminder_id}")
        listing = client.get("/reminders")

    assert response.status_code == 200
    assert response.json()["kind"] == "interview"
    # The stored message is preserved byte-identically.
    assert _reminder_row(path, reminder_id)["message"] == message
    # And never exposed anywhere: the projection structurally omits it and the
    # message-free update cannot have altered it.
    for response_ in (response, detail, listing):
        assert message not in response_.text
        assert "message" not in response_.json()
    assert listing.json()["total"] == 1


# --------------------------------------------------------------------------- #
# Idempotency / repeated identical PATCH
# --------------------------------------------------------------------------- #


def test_idempotent_same_kind_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"], kind="follow_up")
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={"kind": "follow_up"})

    assert response.status_code == 200
    assert response.json()["kind"] == "follow_up"
    assert response.json()["due_at"] == _DUE
    # A no-op update: no domain UPDATE, timestamps untouched.
    assert response.json()["updated_at"] == _EPOCH.isoformat()
    assert _reminder_row(path, reminder_id)["updated_at"] == _EPOCH.isoformat()


def test_idempotent_same_due_at_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"], due_at=_DUE)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={"due_at": _DUE})

    assert response.status_code == 200
    assert response.json()["kind"] == "follow_up"
    assert response.json()["due_at"] == _DUE
    assert response.json()["updated_at"] == _EPOCH.isoformat()
    assert _reminder_row(path, reminder_id)["updated_at"] == _EPOCH.isoformat()


def test_repeated_identical_patch_is_stable_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    payload = {"kind": "interview", "due_at": _NEW_DUE}
    with TestClient(app) as client:
        first = client.patch(f"/reminders/{reminder_id}", json=payload)
        second = client.patch(f"/reminders/{reminder_id}", json=payload)
        third = client.patch(f"/reminders/{reminder_id}", json={"kind": "interview"})
        detail = client.get(f"/reminders/{reminder_id}")

    # The first PATCH is the single effective mutation; repeated identical
    # PATCHes are domain no-ops that preserve updated_at and the logical state.
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200
    assert first.json()["kind"] == "interview"
    assert first.json()["due_at"] == _NEW_DUE
    assert first.json()["updated_at"] != _EPOCH.isoformat()
    assert second.json() == first.json()
    assert third.json()["kind"] == "interview"
    assert third.json()["due_at"] == _NEW_DUE
    assert third.json()["updated_at"] == first.json()["updated_at"]
    assert detail.json()["updated_at"] == first.json()["updated_at"]
    assert _reminder_row(path, reminder_id)["updated_at"] == first.json()["updated_at"]


# --------------------------------------------------------------------------- #
# Lifecycle round trips and request validation
# --------------------------------------------------------------------------- #


def test_lifecycle_round_trip_create_complete_reopen_update_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={"job_id": job_ids["a-1"], "kind": "follow_up", "due_at": _DUE},
        )
        reminder_id = created.json()["reminder_id"]
        completed = client.put(f"/reminders/{reminder_id}/complete")
        reopened = client.put(f"/reminders/{reminder_id}/reopen")
        updated = client.patch(
            f"/reminders/{reminder_id}",
            json={"kind": "offer", "due_at": _NEW_DUE},
        )
        deleted = client.delete(f"/reminders/{reminder_id}")
        final_detail = client.get(f"/reminders/{reminder_id}")

    assert created.status_code == 200
    assert completed.status_code == 200
    assert completed.json()["completed_at"] is not None
    assert reopened.status_code == 200
    assert reopened.json()["completed_at"] is None
    assert updated.status_code == 200
    assert updated.json()["kind"] == "offer"
    assert updated.json()["due_at"] == _NEW_DUE
    assert updated.json()["completed_at"] is None
    assert deleted.status_code == 200
    assert deleted.json()["reminder_id"] == reminder_id
    assert final_detail.status_code == 404
    assert _reminder_ids(path) == []


@pytest.mark.parametrize("reminder_id", [9999, 0, -5])
def test_unknown_reminder_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reminder_id: int
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})

    assert response.status_code == 404
    assert response.json() == {"detail": f"No reminder found with ID {reminder_id}."}


def test_non_integer_reminder_id_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/reminders/not-an-int", json={"kind": "offer"})

    assert response.status_code == 422


def test_missing_body_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}")

    assert response.status_code == 422
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE


def test_malformed_json_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch(f"/reminders/{reminder_id}", content="not json{{")

    assert response.status_code == 422
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_500_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v9"):
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
    with pytest.raises(ValueError, match="current schema v9"):
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
    with pytest.raises(ValueError, match="current schema v9"):
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
            response = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
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

    # The contested PATCH changed nothing and the lock is releasable.
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE
    with TestClient(app) as client:
        recovery = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})

    assert recovery.status_code == 200
    assert recovery.json()["kind"] == "offer"
    assert _reminder_row(path, reminder_id)["kind"] == "offer"


def test_lock_cleanup_after_error_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unknown = client.patch("/reminders/9999", json={"kind": "offer"})
        empty = client.patch(f"/reminders/{reminder_id}", json={})
        valid = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})

    assert unknown.status_code == 404
    assert empty.status_code == 422
    # The lock was released after the 404/422: a later valid request succeeds.
    assert valid.status_code == 200
    assert valid.json()["kind"] == "offer"
    assert _reminder_row(path, reminder_id)["kind"] == "offer"


def test_transaction_rollback_on_update_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])

    def fail_update(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("simulated update failure")

    monkeypatch.setattr("careeros.reminders.service._update_reminder", fail_update)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        response = client.patch(
            f"/reminders/{reminder_id}",
            json={"kind": "offer", "due_at": _NEW_DUE},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "The database schema is unavailable (run 'careeros collect "
        "<source>' to initialize it)."
    )
    for leaked in ("OperationalError", "simulated update failure", "sqlite3"):
        assert leaked not in response.text

    monkeypatch.undo()
    _set_database(monkeypatch, path)

    # The domain rolled the whole mutation back: nothing changed.
    assert _reminder_row(path, reminder_id)["kind"] == "follow_up"
    assert _reminder_row(path, reminder_id)["due_at"] == _DUE
    assert _reminder_row(path, reminder_id)["updated_at"] == _EPOCH.isoformat()

    # A later normal update succeeds and the lock was released after the 500.
    with TestClient(app) as client:
        recovery = client.patch(
            f"/reminders/{reminder_id}", json={"kind": "offer", "due_at": _NEW_DUE}
        )
    assert recovery.status_code == 200
    assert recovery.json()["kind"] == "offer"
    assert recovery.json()["due_at"] == _NEW_DUE


# --------------------------------------------------------------------------- #
# Notification separation
# --------------------------------------------------------------------------- #


def test_update_creates_no_notification_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    reminder_id = _seed_reminder(path, job_ids["a-1"])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
        second = client.patch(f"/reminders/{reminder_id}", json={"due_at": _NEW_DUE})

    assert first.status_code == 200
    assert second.status_code == 200
    # Updating never creates or touches notification rows.
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
            responses.append(
                client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
            )
        _release_child(child)
    finally:
        if child.poll() is None:
            _release_child(child)

    # Malformed databases are refused at startup, then return to the seeded DB.
    malformed = tmp_path / "garbage.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, malformed)
    with pytest.raises(ValueError, match="current schema v9"):
        with TestClient(app):
            pass

    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        responses.append(
            client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
        )
        responses.append(
            client.patch(f"/reminders/{reminder_id}", json={"unknown_field": "x"})
        )
        responses.append(client.patch(f"/reminders/{reminder_id}", json={}))
        responses.append(
            client.patch(f"/reminders/{reminder_id}", json={"kind": "bogus"})
        )
        responses.append(client.patch("/reminders/9999", json={"kind": "offer"}))
        responses.append(client.patch("/reminders/not-an-int", json={"kind": "offer"}))
        responses.append(client.get("/reminders"))
        responses.append(client.get(f"/reminders/{reminder_id}"))

    for response in responses:
        for leaked in (
            "secret-cv-note",
            "secret-review-note",
            "secret-reminder-message",
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
    first = client.patch(
        f"/reminders/{reminder_id}",
        json={"kind": "interview", "due_at": _NEW_DUE},
    )
    second = client.patch(
        f"/reminders/{reminder_id}",
        json={"kind": "interview", "due_at": _NEW_DUE},
    )
    detail = client.get(f"/reminders/{reminder_id}")
    return (
        first.status_code,
        first.json(),
        second.status_code,
        second.json(),
        detail.status_code,
        detail.json(),
    )


def _mask_timestamp(payload: dict) -> dict:
    """Mask the only naturally varying field (``updated_at``) for comparison."""
    masked = dict(payload)
    masked["updated_at"] = "<now>"
    return masked


def test_repeated_updates_and_reads_are_deterministic(
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

    # Structurally deterministic across equivalent databases: identical shapes,
    # semantics, and logical values; the single effective update sets a new
    # updated_at that the repeated identical PATCH then preserves (no-op).
    assert left_result[0] == right_result[0] == 200
    assert left_result[2] == right_result[2] == 200
    assert left_result[4] == right_result[4] == 200
    for result in (left_result, right_result):
        assert result[1]["kind"] == "interview"
        assert result[1]["due_at"] == _NEW_DUE
        assert result[1]["completed_at"] is None
        assert list(result[1]) == _ITEM_FIELDS
        assert result[2] == 200
        assert result[3]["updated_at"] == result[1]["updated_at"]
        assert result[5]["updated_at"] == result[1]["updated_at"]
    assert _mask_timestamp(left_result[1]) == _mask_timestamp(right_result[1])
    assert _mask_timestamp(left_result[3]) == _mask_timestamp(right_result[3])
    assert _mask_timestamp(left_result[5]) == _mask_timestamp(right_result[5])


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
