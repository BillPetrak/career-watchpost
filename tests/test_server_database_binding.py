"""Regression tests for CLI-selected API database binding."""

from __future__ import annotations

from pathlib import Path

from careeros.api.main import app, create_app
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from tests.support import TestClient


def _seed(path: Path) -> None:
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            CollectedJob(
                source="test",
                source_job_id="job-1",
                title="Backend Engineer",
                company="Example",
                location=None,
                description="Build useful things.",
                url=None,
                employment_type=None,
                remote_status="remote",
                published_at=None,
                collected_at="2026-01-01T00:00:00+00:00",
            )
        ],
    )
    connection.commit()
    connection.close()


def test_custom_app_binds_read_and_write_paths(tmp_path: Path, monkeypatch) -> None:
    selected = tmp_path / "selected.sqlite3"
    settings_path = tmp_path / "settings.sqlite3"
    _seed(selected)
    monkeypatch.setenv("CAREEROS_DATABASE", str(settings_path))

    with TestClient(create_app(selected)) as client:
        assert client.get("/health").json()["database"] == "present"
        response = client.put("/review-decisions/1", json={"decision": "shortlist"})

    assert response.status_code == 200
    connection = connect(selected)
    assert (
        connection.execute(
            "SELECT decision FROM review_decisions WHERE job_id = 1"
        ).fetchone()[0]
        == "shortlist"
    )
    connection.close()
    assert not settings_path.exists()


def test_default_app_remains_settings_driven_and_does_not_migrate(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "settings.sqlite3"
    selected = tmp_path / "other.sqlite3"
    _seed(settings_path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(settings_path))

    with TestClient(app) as client:
        assert client.get("/health").json()["schema_version"] == 9

    assert not selected.exists()
