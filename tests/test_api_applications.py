"""Tests for the CareerOS application endpoints (fourth slice).

Covers ``GET /applications`` and ``GET /applications/{job_id}``: envelope and
item field order, deterministic ordering, ``status`` filtering, pagination,
database-availability behavior, and the privacy guarantee that application
notes, event history, reminders, and review decisions never appear in any
HTTP response.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision

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
    track: bool = True,
    notes: bool = True,
    review_and_reminder: bool = False,
) -> tuple[dict[str, int], dict[int, int]]:
    """Seed jobs, optional applications, and optional local workflow state.

    Returns a mapping of source-job-id to jobs.id and a mapping of job_id to
    application id. Three jobs are tracked (applied/interview/interested at
    distinct times) and one job stays untracked.
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
    if track:
        set_application_status(
            connection,
            job_id=job_ids["py-1"],
            status="applied",
            note="secret-cv-note" if notes else None,
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["go-2"],
            status="interview",
            now=_EPOCH + timedelta(hours=1),
        )
        set_application_status(
            connection,
            job_id=job_ids["fs-3"],
            status="interested",
            now=_EPOCH + timedelta(hours=2),
        )
    if review_and_reminder:
        add_reminder(
            connection,
            job_id=job_ids["py-1"],
            kind="follow_up",
            due_at="2026-08-05T09:00:00+00:00",
            message="secret-reminder-message",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["py-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
    application_ids = {
        row["job_id"]: row["id"]
        for row in connection.execute("SELECT id, job_id FROM applications")
    }
    connection.close()
    return job_ids, application_ids


# --------------------------------------------------------------------------- #
# List: shape and ordering
# --------------------------------------------------------------------------- #


def test_list_returns_all_tracked_applications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert [item["job_id"] for item in payload["items"]] == [
        job_ids["fs-3"],
        job_ids["go-2"],
        job_ids["py-1"],
    ]


def test_list_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == ["items", "total", "offset", "limit"]
    item = payload["items"][0]
    assert list(item) == [
        "application_id",
        "job_id",
        "current_status",
        "created_at",
        "updated_at",
        "job",
    ]
    assert list(item["job"]) == _JOB_FIELDS


def test_list_nested_job_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["job"] == {
        "id": job_ids["fs-3"],
        "source": "remoteok",
        "source_job_id": "fs-3",
        "title": "Frontend Developer",
        "company": "Tech Corp",
        "location": "Berlin",
        "remote_status": "remote",
        "employment_type": "Full-time",
        "published_at": "2026-08-01T10:00:00Z",
        "url": "https://example.com/fs-3",
    }
    assert "description" not in item["job"]


def test_list_orders_by_created_at_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["current_status"] for item in items] == [
        "interested",
        "interview",
        "applied",
    ]
    assert [item["job_id"] for item in items] == [
        job_ids["fs-3"],
        job_ids["go-2"],
        job_ids["py-1"],
    ]


def test_list_tie_breaks_by_id_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Alpha"),
            _make_job("remotive", "b-1", "Beta"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    set_application_status(
        connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
    )
    set_application_status(
        connection, job_id=job_ids["b-1"], status="applied", now=_EPOCH
    )
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["job_id"] for item in items] == [job_ids["b-1"], job_ids["a-1"]]


# --------------------------------------------------------------------------- #
# List: status filters
# --------------------------------------------------------------------------- #


def test_list_status_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications", params={"status": "interview"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [item["job_id"] for item in payload["items"]] == [job_ids["go-2"]]


def test_list_repeated_statuses_use_or_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/applications",
            params=[("status", "applied"), ("status", "interested")],
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert {item["job_id"] for item in payload["items"]} == {
        job_ids["py-1"],
        job_ids["fs-3"],
    }


def test_list_invalid_status_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications", params={"status": "hired"})

    assert response.status_code == 422


def test_list_invalid_status_among_valid_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/applications",
            params=[("status", "applied"), ("status", "hired")],
        )

    assert response.status_code == 422


def test_list_filters_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/applications",
            params=[
                ("status", "applied"),
                ("status", "interested"),
                ("offset", 1),
                ("limit", 1),
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert [item["job_id"] for item in payload["items"]] == [job_ids["py-1"]]


def test_list_total_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications", params={"limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 2


def test_list_out_of_range_offset_is_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications", params={"offset": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["items"] == []


def test_list_rejects_invalid_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            response = client.get("/applications", params=params)
            assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #


def test_show_tracked_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, application_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/applications/{job_ids['py-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == [
        "application_id",
        "job_id",
        "current_status",
        "created_at",
        "updated_at",
        "job",
    ]
    assert payload["application_id"] == application_ids[job_ids["py-1"]]
    assert payload["job_id"] == job_ids["py-1"]
    assert payload["current_status"] == "applied"
    assert payload["created_at"] == _EPOCH.isoformat()
    assert payload["updated_at"] == _EPOCH.isoformat()
    assert payload["job"]["title"] == "Python Developer"


def test_show_unknown_job_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_show_existing_untracked_job_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/applications/{job_ids['ds-4']}")

    assert response.status_code == 404
    assert response.json() == {
        "detail": f"No application found for job {job_ids['ds-4']}."
    }


def test_show_rejects_non_integer_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/applications/not-a-number")

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_list_absent_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_show_absent_database_returns_404_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/applications/1")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 1."}
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_list_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


def test_list_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/applications")

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_application_notes_absent_from_every_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path, notes=True)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        listing = client.get("/applications")
        detail = client.get(f"/applications/{job_ids['py-1']}")

    for response in (listing, detail):
        assert response.status_code == 200
        payload = response.json()
        item = payload["items"][0] if "items" in payload else payload
        assert "note" not in item
        assert "latest_note" not in item
        assert "secret-cv-note" not in response.text


def test_review_and_reminder_data_absent_from_every_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(path, review_and_reminder=True)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        listing = client.get("/applications")
        detail = client.get(f"/applications/{job_ids['py-1']}")

    for response in (listing, detail):
        assert response.status_code == 200
        for leaked in (
            "secret-reminder-message",
            "secret-review-note",
            "shortlist",
            "follow_up",
            "reminder",
            "decision",
        ):
            assert leaked not in response.text


# --------------------------------------------------------------------------- #
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    job_ids, _ = _seed(database_path)
    with connect(database_path) as connection:
        initialize_schema(connection)
        run_id = insert_run(connection, source="jobicy", started_at=_EPOCH)
        complete_run(
            connection,
            run_id=run_id,
            finished_at=_EPOCH + timedelta(seconds=5),
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{job_ids['py-1']}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")
        runs = client.get("/collection-runs")

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
    stats_payload = stats.json()
    assert stats_payload["total"] == len(COLLECTORS)

    assert runs.status_code == 200
    runs_payload = runs.json()
    assert runs_payload["total"] == 1
    assert runs_payload["items"][0]["run_id"] == 1


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
