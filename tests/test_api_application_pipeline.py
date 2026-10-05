"""Focused tests for the flat application-pipeline read model."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.applications.service import (
    get_application_pipeline,
    set_application_status,
)
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from tests.support import TestClient


def _job(source_id: str, title: str = "Engineer") -> CollectedJob:
    return CollectedJob(
        source="test",
        source_job_id=source_id,
        title=title,
        company="Acme",
        location="Berlin",
        description="Build things",
        url=f"https://example.test/{source_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T00:00:00Z",
        collected_at="2026-08-01T00:00:00+00:00",
    )


def _seed(path: Path, statuses: tuple[str, ...] = ()) -> None:
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(connection, [_job(str(index)) for index in range(1, len(statuses) + 1)])
    for index, status in enumerate(statuses, 1):
        set_application_status(
            connection,
            job_id=index,
            status=status,
            now=datetime(2026, 8, 1 + index, tzinfo=UTC),
        )
    connection.close()


def test_pipeline_empty_database_is_zero_filled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.db"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    response = TestClient(app).get("/application-pipeline")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["view"] == "active"
    assert body["total"] == 0
    assert body["counts"] == {
        "interested": 0,
        "applied": 0,
        "interview": 0,
        "offer": 0,
        "rejected": 0,
        "withdrawn": 0,
        "active": 0,
        "completed": 0,
        "all": 0,
    }


def test_pipeline_views_counts_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pipeline.db"
    _seed(
        path, ("interested", "applied", "interview", "offer", "rejected", "withdrawn")
    )
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    client = TestClient(app)
    body = client.get("/application-pipeline?limit=2").json()
    assert [item["job_id"] for item in body["items"]] == [4, 3]
    assert body["total"] == 4
    assert body["counts"]["all"] == 6
    assert client.get("/application-pipeline?view=completed").json()["total"] == 2
    last_page = client.get("/application-pipeline?view=all&offset=5").json()
    assert [item["job_id"] for item in last_page["items"]] == [1]
    for status in ("interested", "applied", "interview", "offer"):
        assert client.get(f"/application-pipeline?view={status}").json()["total"] == 1


def test_pipeline_order_uses_updated_at_then_id_and_is_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "duplicates.db"
    _seed(path, ("applied", "applied"))
    connection = connect(path)
    connection.execute(
        "UPDATE applications SET updated_at = ?", ("2026-09-01T00:00:00+00:00",)
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    items = TestClient(app).get("/application-pipeline?view=all").json()["items"]
    assert [item["job_id"] for item in items] == [2, 1]
    assert all(item["job_id"] in (1, 2) for item in items)


def test_pipeline_contract_and_duplicate_statuses_are_concrete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "contract.db"
    _seed(path, ("applied", "rejected"))
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    body = TestClient(app).get("/application-pipeline?view=all").json()
    assert set(body) == {"items", "counts", "view", "total", "offset", "limit"}
    assert set(body["counts"]) == {
        "interested",
        "applied",
        "interview",
        "offer",
        "rejected",
        "withdrawn",
        "active",
        "completed",
        "all",
    }
    assert [(item["job_id"], item["current_status"]) for item in body["items"]] == [
        (2, "rejected"),
        (1, "applied"),
    ]
    assert body["items"][0]["job"]["id"] == body["items"][0]["job_id"]


def test_pipeline_read_is_three_selects_and_never_reads_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trace.db"
    _seed(path, ("applied", "interview"))
    connection = connect(path)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    result = get_application_pipeline(connection, view="active", offset=0, limit=20)
    connection.close()
    selects = [
        statement.upper()
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 3
    assert not any("APPLICATION_EVENTS" in statement for statement in statements)
    assert any(statement.upper() == "BEGIN" for statement in statements)
    assert result.total == 2


def test_existing_applications_endpoint_keeps_created_order_and_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.db"
    _seed(path, ("applied", "interview"))
    connection = connect(path)
    connection.execute(
        "UPDATE applications SET created_at = ? WHERE job_id = 1",
        ("2026-01-01T00:00:00+00:00",),
    )
    connection.commit()
    connection.close()
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    body = TestClient(app).get("/applications").json()
    assert body["items"][0]["job_id"] == 2
    assert set(body["items"][0]) == {
        "application_id",
        "job_id",
        "current_status",
        "created_at",
        "updated_at",
        "job",
    }


def test_pipeline_uninitialized_and_malformed_database_are_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uninitialized = tmp_path / "uninitialized.db"
    connect(uninitialized).close()
    malformed = tmp_path / "malformed.db"
    malformed.write_bytes(b"not sqlite")
    for path in (uninitialized, malformed):
        monkeypatch.setenv("CAREEROS_DATABASE", str(path))
        assert (
            TestClient(app, raise_server_exceptions=False)
            .get("/application-pipeline")
            .status_code
            == 500
        )


def test_pipeline_same_status_idempotency_preserves_recency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "idempotent.db"
    _seed(path, ("applied",))
    before = (
        connect(path)
        .execute("SELECT updated_at FROM applications WHERE job_id = 1")
        .fetchone()["updated_at"]
    )
    connection = connect(path)
    result = set_application_status(connection, job_id=1, status="applied")
    connection.close()
    assert result.changed is False
    after = (
        connect(path)
        .execute("SELECT updated_at FROM applications WHERE job_id = 1")
        .fetchone()["updated_at"]
    )
    assert after == before
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    assert TestClient(app).get("/application-pipeline").json()["total"] == 1


@pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=101", "view=bad"])
def test_pipeline_validates_query_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    path = tmp_path / "validation.db"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    assert TestClient(app).get(f"/application-pipeline?{query}").status_code == 422


def test_pipeline_missing_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing.db"
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    assert TestClient(app).get("/application-pipeline").json()["items"] == []
    assert not path.exists()


def test_schema_version_is_ten_for_m1() -> None:
    assert SCHEMA_VERSION == 14
