"""Tests for the CareerOS reminder endpoints (fifth slice).

Covers ``GET /reminders`` and ``GET /reminders/{reminder_id}``: envelope and
item field order, deterministic ordering, ``state``/``kind``/``job_id``/
``after``/``before`` filtering, explicit ``as_of`` semantics for the
time-dependent states, pagination, database-availability behavior, and the
privacy guarantee that reminder messages, notification delivery history,
application notes, and review decisions never appear in any HTTP response.
"""

from __future__ import annotations

import ast
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
from careeros.reminders.service import add_reminder, complete_reminder
from careeros.review.service import set_decision
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

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

_REMINDER_FIELDS = [
    "reminder_id",
    "job_id",
    "kind",
    "due_at",
    "completed_at",
    "created_at",
    "updated_at",
    "job",
]


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    source: str,
    source_job_id: str,
    title: str,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Tech Corp",
        location="Berlin",
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed(
    path: Path,
    *,
    message: bool = True,
) -> tuple[dict[str, int], dict[str, int]]:
    """Seed four jobs and four reminders (one completed) with fixed times.

    Returns a mapping of source-job-id to jobs.id and a mapping of reminder
    kind to reminders.id.
    """
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "py-1", "Python Developer"),
            _make_job("remotive", "go-2", "Go Engineer"),
            _make_job("remoteok", "fs-3", "Frontend Developer"),
            _make_job("workingnomads", "ds-4", "Data Scientist"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    add_reminder(
        connection,
        job_id=job_ids["py-1"],
        kind="follow_up",
        due_at="2026-08-05T09:00:00+00:00",
        message="secret-reminder-message" if message else None,
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["py-1"],
        kind="interview",
        due_at="2026-08-10T14:00:00+00:00",
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["go-2"],
        kind="offer",
        due_at="2026-08-15T09:00:00+00:00",
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["fs-3"],
        kind="review",
        due_at="2026-08-03T10:00:00+00:00",
        now=_EPOCH,
    )
    reminder_ids = {
        row["kind"]: row["id"]
        for row in connection.execute("SELECT id, kind FROM reminders ORDER BY id")
    }
    complete_reminder(connection, reminder_id=reminder_ids["review"], now=_EPOCH)
    connection.close()
    return job_ids, reminder_ids


# --------------------------------------------------------------------------- #
# List: shape and ordering
# --------------------------------------------------------------------------- #


def test_list_returns_all_reminders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert [item["reminder_id"] for item in payload["items"]] == [
        reminder_ids["review"],
        reminder_ids["follow_up"],
        reminder_ids["interview"],
        reminder_ids["offer"],
    ]


def test_list_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == ["items", "total", "offset", "limit"]
    item = payload["items"][0]
    assert list(item) == _REMINDER_FIELDS
    assert list(item["job"]) == _JOB_FIELDS


def test_list_orders_by_due_at_asc_then_id_asc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["kind"] for item in items] == [
        "review",
        "follow_up",
        "interview",
        "offer",
    ]
    assert [item["due_at"] for item in items] == [
        "2026-08-03T10:00:00+00:00",
        "2026-08-05T09:00:00+00:00",
        "2026-08-10T14:00:00+00:00",
        "2026-08-15T09:00:00+00:00",
    ]


def test_list_tie_breaks_by_id_asc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, reminder_ids = _seed(path)
    with connect(path) as connection:
        add_reminder(
            connection,
            job_id=job_ids["py-1"],
            kind="custom",
            due_at="2026-08-10T14:00:00+00:00",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["kind"] for item in items] == [
        "review",
        "follow_up",
        "interview",
        "custom",
        "offer",
    ]
    assert items[2]["reminder_id"] < items[3]["reminder_id"]
    assert reminder_ids["interview"] < items[3]["reminder_id"]


def test_list_default_includes_open_and_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders")

    assert response.status_code == 200
    items = response.json()["items"]
    completed = next(item for item in items if item["kind"] == "review")
    assert completed["completed_at"] == _EPOCH.isoformat()
    assert completed["reminder_id"] == reminder_ids["review"]
    open_items = [item for item in items if item["kind"] != "review"]
    assert len(open_items) == 3
    assert all(item["completed_at"] is None for item in open_items)


# --------------------------------------------------------------------------- #
# List: state filters and as_of semantics
# --------------------------------------------------------------------------- #


def test_list_state_open_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"state": "open"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["kind"] for item in payload["items"]] == [
        "follow_up",
        "interview",
        "offer",
    ]
    assert all(item["completed_at"] is None for item in payload["items"])


def test_list_state_completed_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"state": "completed"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["reminder_id"] == reminder_ids["review"]
    assert payload["items"][0]["completed_at"] == _EPOCH.isoformat()


def test_list_state_overdue_with_explicit_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/reminders",
            params={"state": "overdue", "as_of": "2026-08-12T00:00:00Z"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["kind"] for item in payload["items"]] == [
        "follow_up",
        "interview",
    ]


def test_list_state_due_with_explicit_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/reminders",
            params={"state": "due", "as_of": "2026-08-05T09:00:00Z"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["kind"] == "follow_up"


def test_list_state_upcoming_with_explicit_as_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/reminders",
            params={"state": "upcoming", "as_of": "2026-08-06T00:00:00Z"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["kind"] for item in payload["items"]] == [
        "interview",
        "offer",
    ]


def test_list_as_of_normalizes_explicit_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/reminders",
            params={"state": "due", "as_of": "2026-08-05T11:00:00+02:00"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["kind"] == "follow_up"


def test_list_as_of_has_no_effect_without_time_dependent_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/reminders",
            params={"as_of": "2026-08-01T00:00:00Z"},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 4


def test_list_as_of_has_no_effect_on_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/reminders",
            params={"state": "open", "as_of": "2026-08-01T00:00:00Z"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert all(item["completed_at"] is None for item in payload["items"])


def test_list_invalid_as_of_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for bad in ("not-a-date", "2026-08-10", "2026-08-10T14:00:00"):
            response = client.get(
                "/reminders", params={"state": "overdue", "as_of": bad}
            )
            assert response.status_code == 422


# --------------------------------------------------------------------------- #
# List: kind, job, and due-window filters
# --------------------------------------------------------------------------- #


def test_list_kind_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"kind": "follow_up"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["reminder_id"] == reminder_ids["follow_up"]


def test_list_job_id_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"job_id": job_ids["py-1"]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["kind"] for item in payload["items"]] == [
        "follow_up",
        "interview",
    ]
    assert all(item["job_id"] == job_ids["py-1"] for item in payload["items"])


def test_list_job_id_filter_unknown_job_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"job_id": 9999})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_list_after_before_window_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        after = client.get("/reminders", params={"after": "2026-08-06T00:00:00+00:00"})
        before = client.get(
            "/reminders", params={"before": "2026-08-12T00:00:00+00:00"}
        )
        window = client.get(
            "/reminders",
            params={
                "after": "2026-08-06T00:00:00+00:00",
                "before": "2026-08-12T00:00:00+00:00",
            },
        )

    assert after.status_code == 200
    assert [item["kind"] for item in after.json()["items"]] == [
        "interview",
        "offer",
    ]
    assert before.status_code == 200
    assert [item["kind"] for item in before.json()["items"]] == [
        "review",
        "follow_up",
        "interview",
    ]
    assert window.status_code == 200
    assert [item["kind"] for item in window.json()["items"]] == ["interview"]


def test_list_invalid_state_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"state": "bogus"})

    assert response.status_code == 422
    assert "bogus" in response.json()["detail"]


def test_list_invalid_kind_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"kind": "bogus"})

    assert response.status_code == 422
    assert "bogus" in response.json()["detail"]


def test_list_invalid_after_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"after": "not-a-date"})

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# List: pagination
# --------------------------------------------------------------------------- #


def test_list_filters_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/reminders",
            params={"job_id": job_ids["py-1"], "offset": 1, "limit": 1},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert [item["kind"] for item in payload["items"]] == ["interview"]


def test_list_total_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    assert len(payload["items"]) == 2


def test_list_out_of_range_offset_is_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders", params={"offset": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    assert payload["items"] == []


def test_list_rejects_invalid_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"offset": -1},
            {"job_id": 0},
        ):
            response = client.get("/reminders", params=params)
            assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #


def test_show_reminder_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/reminders/{reminder_ids['follow_up']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _REMINDER_FIELDS
    assert payload["reminder_id"] == reminder_ids["follow_up"]
    assert payload["job_id"] == job_ids["py-1"]
    assert payload["kind"] == "follow_up"
    assert payload["due_at"] == "2026-08-05T09:00:00+00:00"
    assert payload["completed_at"] is None
    assert payload["created_at"] == _EPOCH.isoformat()
    assert payload["updated_at"] == _EPOCH.isoformat()
    assert payload["job"]["title"] == "Python Developer"
    assert payload["job"]["source"] == "arbeitnow"


def test_show_completed_reminder_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/reminders/{reminder_ids['review']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "review"
    assert payload["completed_at"] == _EPOCH.isoformat()


def test_show_unknown_reminder_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "No reminder found with ID 9999."}


def test_show_reminder_whose_job_is_missing_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, reminder_ids = _seed(path)
    with connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_ids["py-1"],))
        connection.commit()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/reminders/{reminder_ids['follow_up']}")

    assert response.status_code == 404
    assert response.json() == {"detail": f"No job found with ID {job_ids['py-1']}."}


def test_show_orphaned_reminder_absent_from_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _reminder_ids = _seed(path)
    with connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_ids["py-1"],))
        connection.commit()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders")

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {item["kind"] for item in response.json()["items"]} == {
        "offer",
        "review",
    }


def test_show_rejects_non_integer_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders/not-a-number")

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_list_absent_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_show_absent_database_returns_404_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_list_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_show_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_list_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_show_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_reminder_messages_omitted_from_every_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _job_ids, reminder_ids = _seed(path, message=True)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        listing = client.get("/reminders")
        detail = client.get(f"/reminders/{reminder_ids['follow_up']}")

    assert listing.status_code == 200
    for item in listing.json()["items"]:
        assert "message" not in item
    assert "secret-reminder-message" not in listing.text

    assert detail.status_code == 200
    assert "message" not in detail.json()
    assert "secret-reminder-message" not in detail.text


def test_application_review_and_notification_data_absent_from_every_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, reminder_ids = _seed(path, message=True)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["py-1"],
            status="applied",
            note="secret-cv-note",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["py-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
        connection.execute(
            """
            INSERT INTO reminder_notifications
                (reminder_id, scheduled_due_at, attempted_at, delivered_at,
                 status, error, backend)
            VALUES (?, ?, ?, NULL, 'failed', ?, 'dbus')
            """,
            (
                reminder_ids["follow_up"],
                "2026-08-05T09:00:00+00:00",
                _EPOCH.isoformat(),
                "https://secret-notify-host/err RuntimeError boom",
            ),
        )
        connection.commit()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        listing = client.get("/reminders")
        detail = client.get(f"/reminders/{reminder_ids['follow_up']}")

    for response in (listing, detail):
        assert response.status_code == 200
        for leaked in (
            "secret-reminder-message",
            "secret-cv-note",
            "secret-review-note",
            "secret-notify-host",
            "RuntimeError",
            "boom",
            "attempted_at",
            "delivered_at",
            "backend",
            "note",
            "decision",
            "shortlist",
            "applied",
        ):
            assert leaked not in response.text


# --------------------------------------------------------------------------- #
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    job_ids, reminder_ids = _seed(database_path)
    with connect(database_path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["py-1"],
            status="applied",
            now=_EPOCH,
        )
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{job_ids['py-1']}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")
        applications = client.get("/applications")
        application_show = client.get(f"/applications/{job_ids['py-1']}")
        reminders = client.get("/reminders")
        reminder_show = client.get(f"/reminders/{reminder_ids['offer']}")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }

    assert jobs.status_code == 200
    jobs_payload = jobs.json()
    assert jobs_payload["total"] == 4
    assert [item["id"] for item in jobs_payload["items"]] == sorted(job_ids.values())

    assert show.status_code == 200
    assert show.json()["title"] == "Python Developer"

    assert missing.status_code == 404
    assert missing.json() == {"detail": "No job found with ID 9999."}

    assert stats.status_code == 200
    assert stats.json()["total"] == len(COLLECTORS)

    assert applications.status_code == 200
    assert applications.json()["total"] == 1

    assert application_show.status_code == 200
    assert application_show.json()["current_status"] == "applied"

    assert reminders.status_code == 200
    assert reminders.json()["total"] == 4

    assert reminder_show.status_code == 200
    assert reminder_show.json()["kind"] == "offer"


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
