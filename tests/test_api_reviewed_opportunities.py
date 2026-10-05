"""Focused tests for the reviewed-opportunities read model."""

from datetime import datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.review.service import set_decision
from tests.support import TestClient


def _job(source: str, source_id: str, title: str = "Engineer") -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_id,
        title=title,
        company="Acme GmbH",
        location="Berlin",
        description="private description",
        url=f"https://example.com/{source_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed(path: Path) -> dict[str, int]:
    with connect(path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _job("arbeitnow", "a"),
                _job("remotive", "b"),
                _job("jobicy", "c", "Designer"),
            ],
        )
        ids = {
            row["source_job_id"]: row["id"]
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        ensure_grouped(connection)
        set_decision(
            connection,
            job_id=ids["a"],
            decision="shortlist",
            now=datetime.fromisoformat("2026-08-03T12:00:00+00:00"),
        )
        set_decision(
            connection,
            job_id=ids["b"],
            decision="dismiss",
            now=datetime.fromisoformat("2026-08-04T12:00:00+00:00"),
        )
    return ids


def test_grouped_counts_and_owner(monkeypatch, tmp_path):
    path = tmp_path / "reviewed.sqlite3"
    ids = _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    with TestClient(app) as client:
        response = client.get("/reviewed-opportunities")
    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"shortlist": 1, "review_later": 0, "dismiss": 0}
    assert body["total"] == 1
    assert body["items"][0]["decision_job_id"] == ids["a"]
    assert body["items"][0]["conflict"] is True
    assert "description" not in body["items"][0]["group"]["representative"]


def test_flat_pagination_and_validation(monkeypatch, tmp_path):
    path = tmp_path / "reviewed.sqlite3"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    with TestClient(app) as client:
        response = client.get(
            "/reviewed-opportunities",
            params={"grouped": "false", "decision": "dismiss", "limit": 1},
        )
        invalid = client.get("/reviewed-opportunities", params={"decision": "bad"})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["review_decision"] == "dismiss"
    assert invalid.status_code == 422


def test_missing_database_is_read_only(monkeypatch, tmp_path):
    path = tmp_path / "missing" / "reviewed.sqlite3"
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not path.exists()


def test_exact_envelope_all_states_and_counts_before_filter(monkeypatch, tmp_path):
    path = tmp_path / "reviewed.sqlite3"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    with TestClient(app) as client:
        response = client.get("/reviewed-opportunities", params={"limit": 1})
        later = client.get(
            "/reviewed-opportunities", params={"decision": "review_later"}
        )
    assert response.status_code == 200
    body = response.json()
    assert list(body) == [
        "items",
        "counts",
        "grouped",
        "decision",
        "total",
        "offset",
        "limit",
    ]
    assert list(body["items"][0]) == [
        "group",
        "review_decision",
        "decision_job_id",
        "decision_updated_at",
        "conflict",
    ]
    assert body["counts"] == {"shortlist": 1, "review_later": 0, "dismiss": 0}
    assert body["total"] == 1
    assert later.json()["counts"] == body["counts"]
    assert later.json()["items"] == []


def test_grouped_once_flat_independent_and_owner_clear(monkeypatch, tmp_path):
    path = tmp_path / "reviewed.sqlite3"
    ids = _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    with TestClient(app) as client:
        grouped = client.get("/reviewed-opportunities")
        flat = client.get(
            "/reviewed-opportunities",
            params={"grouped": "false", "decision": "dismiss"},
        )
        clear = client.delete(f"/review-decisions/{ids['a']}")
        after_clear = client.get(
            "/reviewed-opportunities", params={"decision": "dismiss"}
        )
    assert grouped.json()["total"] == 1
    assert flat.json()["total"] == 1
    assert flat.json()["items"][0]["decision_job_id"] == ids["b"]
    assert clear.status_code == 200
    assert after_clear.json()["total"] == 1
    assert after_clear.json()["items"][0]["decision_job_id"] == ids["b"]
    assert after_clear.json()["items"][0]["conflict"] is False


def test_pagination_and_ordering(monkeypatch, tmp_path):
    path = tmp_path / "reviewed.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _job("arbeitnow", "a"),
                _job("remotive", "b", "Designer"),
                _job("jobicy", "c", "Writer"),
            ],
        )
        ids = [
            row["id"] for row in connection.execute("SELECT id FROM jobs ORDER BY id")
        ]
        for job_id in ids:
            set_decision(
                connection,
                job_id=job_id,
                decision="shortlist",
                now=datetime.fromisoformat("2026-08-03T12:00:00+00:00"),
            )
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    with TestClient(app) as client:
        first = client.get("/reviewed-opportunities", params={"limit": 2})
        second = client.get("/reviewed-opportunities", params={"offset": 2, "limit": 2})
    assert [item["decision_job_id"] for item in first.json()["items"]] == [
        ids[2],
        ids[1],
    ]
    assert [item["decision_job_id"] for item in second.json()["items"]] == [ids[0]]
    assert first.json()["total"] == 3


def test_reviewed_read_does_not_invoke_other_workflows(monkeypatch, tmp_path):
    path = tmp_path / "reviewed.sqlite3"
    _seed(path)
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))

    def fail(*_args, **_kwargs):
        raise AssertionError("unrelated workflow invoked")

    monkeypatch.setattr("careeros.api.main.build_report", fail)
    monkeypatch.setattr("careeros.api.main.build_queue", fail)
    monkeypatch.setattr("careeros.review.service.score_groups", fail)
    with TestClient(app) as client:
        response = client.get("/reviewed-opportunities")
    assert response.status_code == 200
    with connect(path) as connection:
        assert connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == str(SCHEMA_VERSION)
