from pathlib import Path

from careeros.api.main import app
from careeros.applications.service import set_application_status
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from tests.support import TestClient


def seeded(tmp_path: Path, monkeypatch):
    path = tmp_path / "api-prep.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO jobs(source,title,collected_at) VALUES "
        "('x','Job','2026-01-01T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO jobs(source,title,collected_at) VALUES "
        "('x','Untracked','2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    set_application_status(connection, job_id=1, status="interested")
    connection.close()
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    return TestClient(app)


def headers(revision: int):
    return {"X-CareerOS-Write": "1", "X-CareerOS-Preparation-Revision": str(revision)}


def test_get_and_add_contract(tmp_path, monkeypatch):
    client = seeded(tmp_path, monkeypatch)
    response = client.get("/applications/1/preparation")
    assert response.status_code == 200
    assert response.json()["revision"] == 0
    response = client.post(
        "/applications/1/requirements",
        headers=headers(0),
        json={"kind": "task", "label": "Apply", "required": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert {
        "id",
        "application_id",
        "kind",
        "label",
        "required",
        "fulfillment_state",
        "sort_order",
        "origin",
        "capture_method",
        "confirmed",
    } <= set(body["items"][0])
    assert len(body["items"][0]["id"]) == 32


def test_api_revision_and_unknown_errors(tmp_path, monkeypatch):
    client = seeded(tmp_path, monkeypatch)
    response = client.post(
        "/applications/99/requirements",
        headers=headers(0),
        json={"kind": "task", "label": "A"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "job_not_found"}
    response = client.post(
        "/applications/2/requirements",
        headers=headers(0),
        json={"kind": "task", "label": "A"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "application_not_tracked"}
    assert (
        client.post(
            "/applications/1/requirements",
            headers={"X-CareerOS-Write": "1"},
            json={"kind": "task", "label": "A"},
        ).status_code
        == 428
    )
    assert client.get("/applications/99/preparation").json() == {
        "detail": "job_not_found"
    }
    assert client.get("/applications/1/preparation").status_code == 200


def test_revision_conflict_exposes_only_current_revision(tmp_path, monkeypatch):
    client = seeded(tmp_path, monkeypatch)
    client.post(
        "/applications/1/requirements",
        headers=headers(0),
        json={"kind": "task", "label": "A"},
    )
    response = client.post(
        "/applications/1/requirements",
        headers=headers(0),
        json={"kind": "task", "label": "B"},
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "preparation_revision_conflict",
            "current_revision": 1,
        }
    }


def test_logical_revision_zero_includes_readiness_reasons(tmp_path, monkeypatch):
    client = seeded(tmp_path, monkeypatch)
    body = client.get("/applications/1/preparation").json()
    assert body["created_at"] is None
    assert body["updated_at"] is None
    assert [reason["code"] for reason in body["readiness"]["reasons"]] == [
        "job_information_unavailable",
        "requirements_not_reviewed",
    ]


def test_api_crud_review_ready(tmp_path, monkeypatch):
    client = seeded(tmp_path, monkeypatch)
    created = client.post(
        "/applications/1/requirements",
        headers=headers(0),
        json={"kind": "task", "label": "A"},
    ).json()
    item_id = created["items"][0]["id"]
    assert (
        client.patch(
            f"/application-requirements/{item_id}",
            headers=headers(1),
            json={"confirmed": True},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/application-requirements/{item_id}",
            headers=headers(2),
            json={"fulfillment_state": "satisfied"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/applications/1/requirements-review", headers=headers(3)
        ).status_code
        == 200
    )
    assert (
        client.put("/applications/1/ready-confirmation", headers=headers(3)).status_code
        == 200
    )
    assert (
        client.delete(
            "/applications/1/ready-confirmation", headers=headers(3)
        ).status_code
        == 200
    )
    assert (
        client.delete(
            "/application-requirements/" + item_id, headers=headers(3)
        ).status_code
        == 200
    )


def test_extra_body_rejected_and_deadline_semantics(tmp_path, monkeypatch):
    client = seeded(tmp_path, monkeypatch)
    response = client.post(
        "/applications/1/requirements",
        headers=headers(0),
        json={"kind": "deadline", "label": "D", "satisfied": True},
    )
    assert response.status_code == 422
    response = client.post(
        "/applications/1/requirements",
        headers=headers(0),
        json={"kind": "deadline", "label": "D", "fulfillment_state": "satisfied"},
    )
    assert response.status_code == 422
