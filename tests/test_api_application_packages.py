from __future__ import annotations

# SQL fixtures mirror the package/preparation ownership contract.
# ruff: noqa: E501
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

import careeros.api.main as api_main
import careeros.application_packages.service as package_service
from careeros.api.deps import read_only_connection, unlocked_writable_connection
from careeros.api.main import create_app
from careeros.application_packages.service import (
    open_draft,
    set_approval,
    set_material_selection,
)
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict
from careeros.materials.service import create_material
from careeros.materials.storage import stage_bytes
from tests.support import TestClient


def seed(path: Path) -> int:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    timestamp = "2026-09-01T10:00:00+00:00"
    connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,description,collected_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            "fixture",
            "fictional-1",
            "Fictional Engineer",
            "Example Org",
            "Fictional job information",
            timestamp,
            timestamp,
        ),
    )
    job_id = int(connection.execute("SELECT id FROM jobs").fetchone()[0])
    connection.execute(
        "INSERT INTO applications(job_id,current_status,created_at,updated_at) VALUES(?,?,?,?)",
        (job_id, "applied", timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO application_preparations(application_id,revision,requirements_reviewed_revision,requirements_reviewed_at,ready_confirmed_revision,ready_confirmed_at,created_at,updated_at) VALUES(1,0,0,?,0,?,?,?)",
        (timestamp, timestamp, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO application_requirement_items(id,application_id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "question-1",
            1,
            "question",
            "Why this fictional role?",
            None,
            1,
            "satisfied",
            None,
            0,
            "manual",
            "manual",
            1,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    connection.close()
    return job_id


def write_headers(package_revision: int | None = None) -> dict[str, str]:
    headers = {
        "X-CareerOS-Write": "1",
        "X-CareerOS-Preparation-Revision": "0",
    }
    if package_revision is not None:
        headers["X-CareerOS-Package-Revision"] = str(package_revision)
    return headers


def seed_finalizable_material_package(
    path: Path, root: Path, *, requirements: int = 2
) -> tuple[int, str, int]:
    job_id = seed(path)
    connection = connect(path)
    connection.execute(
        "UPDATE application_requirement_items SET id='material-0',kind='material',label='Fictional document',fulfillment_state='satisfied' WHERE id='question-1'"
    )
    for index in range(1, requirements):
        connection.execute(
            "INSERT INTO application_requirement_items(id,application_id,kind,label,detail,required,fulfillment_state,deadline_at,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"material-{index}",
                1,
                "material",
                f"Fictional document {index}",
                None,
                1,
                "satisfied",
                None,
                index,
                "manual",
                "manual",
                1,
                "2026-09-01T10:00:00+00:00",
                "2026-09-01T10:00:00+00:00",
            ),
        )
    connection.commit()
    stage, size, digest = stage_bytes(root, [b"fictional route material"])
    material = create_material(
        connection,
        root,
        name="Fictional Route Resume",
        category="resume",
        filename="fictional-route.pdf",
        media_type="application/pdf",
        stage=stage,
        size=size,
        sha256=digest,
    )
    material_versions = [str(material["current_version"]["version_id"])]
    if requirements >= 3:
        second_stage, second_size, second_digest = stage_bytes(
            root, [b"second fictional route material"]
        )
        second = create_material(
            connection,
            root,
            name="Fictional Route Portfolio",
            category="portfolio",
            filename="fictional-portfolio.pdf",
            media_type="application/pdf",
            stage=second_stage,
            size=second_size,
            sha256=second_digest,
        )
        material_versions.append(str(second["current_version"]["version_id"]))
    draft, _ = open_draft(connection, job_id, preparation_revision=0)
    revision = 0
    for index in range(requirements):
        selected = set_material_selection(
            connection,
            str(draft["id"]),
            f"material-{index}",
            material_versions[-1]
            if index == requirements - 1
            else material_versions[0],
            package_revision=revision,
            preparation_revision=0,
        )
        revision = int(selected["revision"])
    set_approval(
        connection,
        str(draft["id"]),
        approved=True,
        package_revision=revision,
        preparation_revision=0,
        root=root,
    )
    connection.close()
    return job_id, str(draft["id"]), revision


def test_complete_answer_package_lifecycle_and_privacy(tmp_path):
    path = tmp_path / "career.sqlite3"
    root = tmp_path / "materials"
    job_id = seed(path)
    app = create_app(path, root)
    with TestClient(app) as client:
        empty = client.get(f"/applications/{job_id}/package")
        assert empty.status_code == 200
        assert empty.json()["package_id"] is None

        created = client.put(
            f"/applications/{job_id}/package/draft",
            json={},
            headers=write_headers(),
        )
        assert created.status_code == 201
        assert created.headers["location"].startswith("/application-package-versions/")
        draft = created.json()
        version_id = draft["version_id"]
        assert draft["state"] == "draft"
        assert "description" not in draft["job_snapshot"]
        assert "job_content_fingerprint" not in created.text

        answered = client.put(
            f"/application-package-versions/{version_id}/answers/question-1",
            json={"text": "A fictional, user-authored answer."},
            headers=write_headers(0),
        )
        assert answered.status_code == 200
        assert answered.json()["revision"] == 1

        confirmed = client.put(
            f"/application-package-versions/{version_id}/answers/question-1/confirmation",
            json={"confirmed": True},
            headers=write_headers(1),
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["revision"] == 2

        approved = client.put(
            f"/application-package-versions/{version_id}/approval",
            json={"approved": True},
            headers=write_headers(2),
        )
        assert approved.status_code == 200
        assert approved.json()["approval"]["status"] == "current"

        finalized = client.put(
            f"/application-package-versions/{version_id}/finalization",
            json={"finalize": True},
            headers=write_headers(2),
        )
        assert finalized.status_code == 200
        frozen = finalized.json()
        assert frozen["state"] == "finalized"
        assert frozen["answers"][0]["text"] == "A fictional, user-authored answer."
        assert frozen["completeness"]["reasons"][-1]["code"] == "package_complete"

        retried = client.put(
            f"/application-package-versions/{version_id}/finalization",
            json={"finalize": True},
            headers=write_headers(2),
        )
        assert retried.status_code == 200
        assert retried.json()["finalized_at"] == frozen["finalized_at"]

        summary = client.get(f"/applications/{job_id}/package").json()
        assert summary["draft"] is None
        assert summary["history"]["total"] == 1
        assert "answer" not in str(summary).lower()

        pipeline = client.get("/application-pipeline").json()
        package = pipeline["items"][0]["package"]
        assert package == {
            "has_draft": False,
            "draft_approved_for_current_revision": False,
            "finalized_count": 1,
            "latest_finalized_at": frozen["finalized_at"],
        }
        assert "completeness" not in package

    connection = sqlite3.connect(path)
    assert (
        connection.execute("SELECT current_status FROM applications").fetchone()[0]
        == "applied"
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM application_events").fetchone()[0] == 0
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == 0
    )
    assert connection.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("body", "expected"),
    [({}, "package_revision_required"), ({"extra": True}, "invalid_package_request")],
)
def test_package_errors_are_stable_and_private(tmp_path, body, expected):
    path = tmp_path / "career.sqlite3"
    job_id = seed(path)
    app = create_app(path, tmp_path / "materials")
    with TestClient(app) as client:
        created = client.put(
            f"/applications/{job_id}/package/draft",
            json={},
            headers=write_headers(),
        ).json()
        headers = (
            write_headers()
            if expected == "package_revision_required"
            else write_headers(0)
        )
        response = client.request(
            "DELETE",
            f"/application-package-versions/{created['version_id']}/answers/question-1",
            json=body,
            headers=headers,
        )
    assert response.status_code in (422, 428)
    assert response.json() == {"detail": expected}
    assert str(path) not in response.text


def test_finalization_dependency_is_not_prelocked(tmp_path, monkeypatch):
    path = tmp_path / "career.sqlite3"
    seed(path)
    app = create_app(path, tmp_path / "materials")
    called = False

    def observed_finalize(*_args, **_kwargs):
        nonlocal called
        called = True
        raise CollectionLockConflict(path, Path(str(path) + ".lock"), {})

    monkeypatch.setattr(api_main, "finalize_package", observed_finalize)
    with TestClient(app) as client:
        response = client.put(
            "/application-package-versions/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/finalization",
            json={"finalize": True},
            headers=write_headers(0),
        )
    assert called
    assert response.status_code == 503
    assert response.json() == {
        "detail": "The database is temporarily busy; retry the request later."
    }


def test_pipeline_remains_exactly_five_selects(tmp_path):
    path = tmp_path / "career.sqlite3"
    seed(path)
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    app = create_app(path, tmp_path / "materials")

    def traced_connection():
        yield connection

    app.dependency_overrides[read_only_connection] = traced_connection
    with TestClient(app) as client:
        response = client.get("/application-pipeline")
    connection.close()
    assert response.status_code == 200
    assert (
        sum(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        == 5
    )


def test_http_finalization_query_hash_and_retry_contract(tmp_path, monkeypatch):
    path = tmp_path / "career.sqlite3"
    root = tmp_path / "materials"
    _job_id, version_id, revision = seed_finalizable_material_package(
        path, root, requirements=3
    )
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    app = create_app(path, root)

    def traced_connection():
        yield connection

    app.dependency_overrides[unlocked_writable_connection] = traced_connection
    hash_opens = 0
    lock_acquisitions = 0
    real_open = package_service._open_verified_material_metadata
    real_lock = package_service.collection_lock

    def counted_open(*args, **kwargs):
        nonlocal hash_opens
        hash_opens += 1
        return real_open(*args, **kwargs)

    @contextmanager
    def counted_lock(*args, **kwargs):
        nonlocal lock_acquisitions
        lock_acquisitions += 1
        with real_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(
        package_service, "_open_verified_material_metadata", counted_open
    )
    monkeypatch.setattr(package_service, "collection_lock", counted_lock)
    with TestClient(app) as client:
        initial = client.put(
            f"/application-package-versions/{version_id}/finalization",
            json={"finalize": True},
            headers=write_headers(revision),
        )
        initial_selects = sum(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )
        assert initial.status_code == 200
        assert initial_selects == 10
        assert hash_opens == 2
        assert lock_acquisitions == 1

        frozen = initial.json()
        statements.clear()
        hash_opens = 0
        lock_acquisitions = 0
        real_finalize = api_main.finalize_package

        def retry_with_forbidden_hook(*args, **kwargs):
            def forbidden_hook():
                raise AssertionError("HTTP retry ran the preflight hook")

            return real_finalize(*args, **kwargs, _after_preflight=forbidden_hook)

        monkeypatch.setattr(api_main, "finalize_package", retry_with_forbidden_hook)
        retried = client.put(
            f"/application-package-versions/{version_id}/finalization",
            json={"finalize": True},
            headers=write_headers(revision),
        )
        retry_selects = sum(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )
    connection.close()
    assert retried.status_code == 200
    assert retried.json() == frozen
    assert retry_selects == 5
    assert hash_opens == 0
    assert lock_acquisitions == 0


def test_finalized_mutation_and_deletion_http_matrix(tmp_path):
    path = tmp_path / "career.sqlite3"
    job_id = seed(path)
    app = create_app(path, tmp_path / "materials")
    with TestClient(app) as client:
        draft = client.put(
            f"/applications/{job_id}/package/draft", json={}, headers=write_headers()
        ).json()
        version_id = draft["version_id"]
        missing = client.request(
            "DELETE",
            f"/application-package-versions/{version_id}",
            json={"confirm": True},
            headers={"X-CareerOS-Write": "1"},
        )
        malformed = client.request(
            "DELETE",
            f"/application-package-versions/{version_id}",
            json={"confirm": True},
            headers={"X-CareerOS-Write": "1", "X-CareerOS-Package-Revision": "-1"},
        )
        missing_version = client.request(
            "DELETE",
            "/application-package-versions/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            json={"confirm": True},
            headers={"X-CareerOS-Write": "1", "X-CareerOS-Package-Revision": "0"},
        )
        stale = client.request(
            "DELETE",
            f"/application-package-versions/{version_id}",
            json={"confirm": True},
            headers={"X-CareerOS-Write": "1", "X-CareerOS-Package-Revision": "1"},
        )
        assert (missing.status_code, missing.json()) == (
            428,
            {"detail": "package_revision_required"},
        )
        assert (malformed.status_code, malformed.json()) == (
            422,
            {"detail": "invalid_package_revision"},
        )
        assert (missing_version.status_code, missing_version.json()) == (
            404,
            {"detail": "package_version_not_found"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "package_revision_conflict"

        client.put(
            f"/application-package-versions/{version_id}/answers/question-1",
            json={"text": "Fictional answer"},
            headers=write_headers(0),
        )
        client.put(
            f"/application-package-versions/{version_id}/answers/question-1/confirmation",
            json={"confirmed": True},
            headers=write_headers(1),
        )
        client.put(
            f"/application-package-versions/{version_id}/approval",
            json={"approved": True},
            headers=write_headers(2),
        )
        finalized = client.put(
            f"/application-package-versions/{version_id}/finalization",
            json={"finalize": True},
            headers=write_headers(2),
        )
        assert finalized.status_code == 200
        immutable = client.put(
            f"/application-package-versions/{version_id}/answers/question-1",
            json={"text": "Must not replace frozen text"},
            headers=write_headers(2),
        )
        assert (immutable.status_code, immutable.json()) == (
            409,
            {"detail": "package_version_immutable"},
        )
        deleted = client.request(
            "DELETE",
            f"/application-package-versions/{version_id}",
            json={"confirm": True},
            headers={"X-CareerOS-Write": "1", "X-CareerOS-Package-Revision": "2"},
        )
        repeated = client.request(
            "DELETE",
            f"/application-package-versions/{version_id}",
            json={"confirm": True},
            headers={"X-CareerOS-Write": "1", "X-CareerOS-Package-Revision": "2"},
        )
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert (repeated.status_code, repeated.json()) == (
        404,
        {"detail": "package_version_not_found"},
    )


def test_future_reference_conflict_is_privacy_safe_http_409(tmp_path):
    path = tmp_path / "career.sqlite3"
    root = tmp_path / "materials"
    _job_id, version_id, revision = seed_finalizable_material_package(
        path, root, requirements=1
    )
    app = create_app(path, root)
    with TestClient(app) as client:
        assert (
            client.put(
                f"/application-package-versions/{version_id}/finalization",
                json={"finalize": True},
                headers=write_headers(revision),
            ).status_code
            == 200
        )
        connection = connect(path)
        connection.execute(
            "CREATE TABLE future_package_refs(version_id TEXT NOT NULL REFERENCES application_package_versions(id) ON DELETE RESTRICT)"
        )
        connection.execute("INSERT INTO future_package_refs VALUES(?)", (version_id,))
        connection.commit()
        connection.close()
        response = client.request(
            "DELETE",
            f"/application-package-versions/{version_id}",
            json={"confirm": True},
            headers={
                "X-CareerOS-Write": "1",
                "X-CareerOS-Package-Revision": str(revision),
            },
        )
    assert (response.status_code, response.json()) == (
        409,
        {"detail": "package_version_referenced"},
    )


def test_package_http_broad_privacy_sentinel_scan(tmp_path):
    path = tmp_path / "career.sqlite3"
    root = tmp_path / "private-materials-root-sentinel"
    job_id, version_id, revision = seed_finalizable_material_package(
        path, root, requirements=1
    )
    connection = connect(path)
    description = "PRIVATE_JOB_DESCRIPTION_SENTINEL_7f41"
    connection.execute(
        "UPDATE jobs SET description=? WHERE id=?", (description, job_id)
    )
    store_id = str(
        connection.execute("SELECT store_id FROM material_store").fetchone()[0]
    )
    material_row = connection.execute(
        "SELECT storage_key,sha256 FROM material_versions"
    ).fetchone()
    storage_key, sha256 = map(str, material_row)
    connection.execute(
        "UPDATE application_package_versions SET job_content_fingerprint=(SELECT job_content_fingerprint FROM application_package_versions WHERE id=?) WHERE id=?",
        (version_id, version_id),
    )
    connection.commit()
    connection.close()
    app = create_app(path, root)
    with TestClient(app) as client:
        responses = [
            client.get(f"/applications/{job_id}/package"),
            client.get(f"/application-package-versions/{version_id}"),
            client.get("/application-pipeline"),
            client.put(
                f"/application-package-versions/{version_id}/finalization",
                json={"finalize": True},
                headers=write_headers(revision),
            ),
        ]
    combined = "\n".join(response.text for response in responses)
    for sentinel in (str(root), description, store_id, storage_key, sha256):
        assert sentinel not in combined
