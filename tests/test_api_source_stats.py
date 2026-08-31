"""Tests for the CareerOS source-stats endpoint (second slice)."""

from __future__ import annotations

from pathlib import Path

import pytest

from careeros.api import app
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.source_stats.models import MISSING_FIELD_COLUMNS
from tests.support import TestClient

SOURCE_ORDER = tuple(COLLECTORS)


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _job(
    source: str,
    source_job_id: str,
    title: str,
    *,
    company: str | None = "TechNova",
    location: str | None = "Berlin",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build backend services.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-30T10:00:00Z",
        collected_at="2026-07-30T12:00:00+00:00",
    )


def _seed(path: Path, jobs: list[CollectedJob]) -> None:
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(connection, jobs)
    connection.close()


def test_source_stats_returns_all_sources_in_registry_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path, [_job("arbeitnow", "py-1", "Python Developer")])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/sources/stats")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 7
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert [item["source"] for item in payload["items"]] == list(SOURCE_ORDER)


def test_source_stats_coverage_and_missing_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(
        path,
        [
            _job("arbeitnow", "py-1", "Python Developer"),
            _job("himalayas", "hs-2", "Rust Engineer", company=None, location=None),
        ],
    )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/sources/stats")

    assert response.status_code == 200
    items = {item["source"]: item for item in response.json()["items"]}

    arbeitnow = items["arbeitnow"]
    assert arbeitnow["total_jobs"] == 1
    assert all(field["missing_count"] == 0 for field in arbeitnow["fields"])
    assert all(field["provided_count"] == 1 for field in arbeitnow["fields"])
    assert all(field["coverage_pct"] == 100 for field in arbeitnow["fields"])

    himalayas = items["himalayas"]
    assert himalayas["total_jobs"] == 1
    by_field = {field["field"]: field for field in himalayas["fields"]}
    for field in ("company", "location"):
        assert by_field[field]["missing_count"] == 1
        assert by_field[field]["provided_count"] == 0
        assert by_field[field]["coverage_pct"] == 0
    for field in MISSING_FIELD_COLUMNS:
        if field in ("company", "location"):
            continue
        assert by_field[field]["missing_count"] == 0
        assert by_field[field]["provided_count"] == 1
        assert by_field[field]["coverage_pct"] == 100


def test_source_stats_never_groupable_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(
        path,
        [
            _job("arbeitnow", "py-1", "Python Developer"),
            _job("himalayas", "hs-2", "Rust Engineer", company=None),
        ],
    )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/sources/stats")

    assert response.status_code == 200
    items = {item["source"]: item for item in response.json()["items"]}
    assert items["himalayas"]["never_groupable_jobs"] == 1
    assert items["arbeitnow"]["never_groupable_jobs"] == 0


def test_source_stats_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path, [_job("arbeitnow", "py-1", "Python Developer")])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/sources/stats")

    assert response.status_code == 200
    entry = response.json()["items"][0]
    assert [field["field"] for field in entry["fields"]] == list(MISSING_FIELD_COLUMNS)
    assert [key for key in entry["fields"][0]] == [
        "field",
        "missing_count",
        "provided_count",
        "coverage_pct",
    ]


def test_source_stats_source_filter_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path, [_job("remotive", "go-2", "Go Engineer")])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/sources/stats", params={"source": "remotive"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert payload["items"][0]["source"] == "remotive"


def test_source_stats_source_filter_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path, [_job("arbeitnow", "py-1", "Python Developer")])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/sources/stats", params={"source": "nonexistent", "offset": 2, "limit": 5}
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 2, "limit": 5}


def test_source_stats_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path, [_job("arbeitnow", "py-1", "Python Developer")])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/sources/stats", params={"offset": 1, "limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 7
    assert payload["offset"] == 1
    assert payload["limit"] == 2
    assert [item["source"] for item in payload["items"]] == list(SOURCE_ORDER[1:3])


def test_source_stats_absent_database_refuses_startup_without_creating_file(
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


def test_source_stats_rejects_invalid_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path, [_job("arbeitnow", "py-1", "Python Developer")])
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            response = client.get("/sources/stats", params=params)
            assert response.status_code == 422


def test_source_stats_uninitialized_database_refuses_startup_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    _set_database(monkeypatch, path)

    before = path.read_bytes()
    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v10 database"
    assert path.read_bytes() == before == b""
