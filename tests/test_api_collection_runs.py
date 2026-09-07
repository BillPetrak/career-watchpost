"""Tests for the CareerOS collection-run history endpoint (third slice)."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros.api import app
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _seed_run(
    database_path: Path,
    source: str,
    started_at: datetime,
    *,
    succeeded: bool = True,
    error: str | None = None,
    fetched: int = 1,
    inserted: int = 1,
    updated: int = 0,
    incomplete: bool = False,
) -> None:
    """Write a run record directly into the database."""
    with connect(database_path) as connection:
        initialize_schema(connection)
        run_id = insert_run(connection, source=source, started_at=started_at)
        if not incomplete:
            complete_run(
                connection,
                run_id=run_id,
                finished_at=started_at + timedelta(seconds=5),
                succeeded=succeeded,
                fetched=fetched,
                inserted=inserted,
                updated=updated,
                error=error,
            )


def _seed_jobs(database_path: Path) -> int:
    """Seed stored jobs and return the id of the first job."""
    with connect(database_path) as connection:
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
                )
            ],
        )
        row = connection.execute("SELECT id FROM jobs ORDER BY id").fetchone()
        return int(row["id"])


# --------------------------------------------------------------------------- #
# Populated history
# --------------------------------------------------------------------------- #


def test_history_returns_all_runs_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=2))
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1))
    _seed_run(
        database_path,
        "arbeitnow",
        _EPOCH,
        succeeded=False,
        error="URLError: network down",
    )
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    items = payload["items"]
    assert [item["run_id"] for item in items] == [3, 2, 1]
    assert [item["source"] for item in items] == ["arbeitnow", "jobicy", "jobicy"]
    assert [item["started_at"] for item in items] == [
        _EPOCH.isoformat(),
        (_EPOCH - timedelta(hours=1)).isoformat(),
        (_EPOCH - timedelta(hours=2)).isoformat(),
    ]
    assert items[0]["succeeded"] == 0
    assert items[0]["fetched"] == 1
    assert items[0]["inserted"] == 1
    assert items[0]["updated"] == 0
    assert items[1]["succeeded"] == 1


def test_history_tie_breaks_by_id_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(database_path, "remoteok", _EPOCH)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs")

    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()["items"]] == [2, 1]


def test_history_item_field_order_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert list(item) == [
        "run_id",
        "source",
        "started_at",
        "finished_at",
        "succeeded",
        "fetched",
        "inserted",
        "updated",
    ]
    assert list(response.json()) == ["items", "total", "offset", "limit"]


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def test_history_source_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(database_path, "arbeitnow", _EPOCH - timedelta(hours=1))
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs", params={"source": "jobicy"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [item["source"] for item in payload["items"]] == ["jobicy"]


def test_history_source_filter_unknown_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get(
            "/collection-runs",
            params={"source": "nonexistent", "offset": 2, "limit": 5},
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 2, "limit": 5}


def test_history_failed_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH - timedelta(hours=1),
        succeeded=False,
        error="URLError: down",
    )
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=2), incomplete=True)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs", params={"failed": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["succeeded"] == 0
    assert payload["items"][0]["run_id"] == 2


def test_history_failed_filter_excludes_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1), incomplete=True)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs", params={"failed": "true"})

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["total"] == 0


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


def test_history_pagination_slices_after_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    for hours in range(5):
        _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=hours))
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs", params={"offset": 1, "limit": 2})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 5
    assert payload["offset"] == 1
    assert payload["limit"] == 2
    assert [item["run_id"] for item in payload["items"]] == [2, 3]


def test_history_pagination_total_reflects_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    for hours in range(3):
        _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=hours))
    _seed_run(database_path, "remotive", _EPOCH - timedelta(hours=10))
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get(
            "/collection-runs",
            params={"source": "jobicy", "offset": 2, "limit": 1},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["run_id"] for item in payload["items"]] == [3]


def test_history_out_of_range_offset_returns_empty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs", params={"offset": 10})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"] == []


def test_history_rejects_invalid_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            response = client.get("/collection-runs", params=params)
            assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_history_empty_initialized_database_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_history_absent_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v11"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_history_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v11"):
        with TestClient(app):
            pass


def test_history_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v11"):
        with TestClient(app):
            pass


# --------------------------------------------------------------------------- #
# Corrupt stored values
# --------------------------------------------------------------------------- #


def test_history_malformed_timestamps_and_null_values_stay_representable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', 'not-a-date', 'also-not-a-date', 1)
            """
        )
        connection.commit()
    _seed_run(database_path, "jobicy", _EPOCH, incomplete=True)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["run_id"] for item in items] == [1, 2]
    assert items[0]["started_at"] == "not-a-date"
    assert items[0]["finished_at"] == "also-not-a-date"
    assert items[0]["succeeded"] == 1
    assert items[1]["finished_at"] is None
    assert items[1]["succeeded"] is None


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_history_omits_error_field_and_diagnostic_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH,
        succeeded=False,
        error=(
            "URLError: <urlopen error [Errno 111] Connection refused> "
            "https://jobs.example.com/feed /tmp/careeros/data.db RuntimeError boom"
        ),
    )
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.get("/collection-runs")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "error" not in item
    assert item["succeeded"] == 0
    text = response.text
    for leaked in (
        "URLError",
        "Connection refused",
        "https://jobs.example.com",
        "/tmp/careeros",
        "RuntimeError",
        "boom",
    ):
        assert leaked not in text


# --------------------------------------------------------------------------- #
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    first_id = _seed_jobs(database_path)
    _seed_run(database_path, "jobicy", _EPOCH)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{first_id}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }

    assert jobs.status_code == 200
    jobs_payload = jobs.json()
    assert jobs_payload["total"] == 1
    assert [item["id"] for item in jobs_payload["items"]] == [first_id]

    assert show.status_code == 200
    assert show.json()["title"] == "Python Developer"

    assert missing.status_code == 404
    assert missing.json() == {"detail": "No job found with ID 9999."}

    assert stats.status_code == 200
    assert stats.json()["total"] == len(COLLECTORS)


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
