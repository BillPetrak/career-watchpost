"""Tests for the CareerOS job listing and detail endpoints (first slice)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _seed(path: Path) -> tuple[int, int]:
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            CollectedJob(
                source="arbeitnow",
                source_job_id="py-1",
                title="Python Developer",
                company="Tech Corp",
                location="Athens",
                description="Build Python backends.",
                url="https://example.com/1",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-07-29T10:00:00Z",
                collected_at="2026-07-29T12:00:00+00:00",
            ),
            CollectedJob(
                source="remotive",
                source_job_id="go-2",
                title="Go Engineer",
                company="Firm Inc",
                location="Berlin",
                description="Build Go services.",
                url="https://example.com/2",
                employment_type="Full-time",
                remote_status="onsite_or_unspecified",
                published_at="2026-07-30T10:00:00Z",
                collected_at="2026-07-30T12:00:00+00:00",
            ),
        ],
    )
    rows = connection.execute("SELECT id FROM jobs ORDER BY id").fetchall()
    ids = tuple(row["id"] for row in rows)
    connection.close()
    return ids[0], ids[1]


def test_list_jobs_returns_all_in_canonical_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert [item["id"] for item in payload["items"]] == [2, 1]


def test_list_jobs_pagination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"offset": 1, "limit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["offset"] == 1
    assert payload["limit"] == 1
    assert [item["id"] for item in payload["items"]] == [1]


def test_list_jobs_source_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"source": "remotive"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["source"] == "remotive"


def test_list_jobs_remote_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"remote": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["remote_status"] == "remote"


def test_list_jobs_absent_database_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_database(monkeypatch, tmp_path / "missing.sqlite3")

    with TestClient(app) as client:
        response = client.get("/jobs")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_list_jobs_rejects_invalid_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"limit": 0})

    assert response.status_code == 422


def test_show_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    first_id, _ = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/jobs/{first_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == first_id
    assert payload["title"] == "Python Developer"
    assert payload["source"] == "arbeitnow"


def test_show_job_missing_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_show_job_absent_database_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_database(monkeypatch, tmp_path / "missing.sqlite3")

    with TestClient(app) as client:
        response = client.get("/jobs/1")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 1."}


def test_show_job_rejects_non_integer_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs/not-a-number")

    assert response.status_code == 422


def test_list_jobs_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs")

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]
