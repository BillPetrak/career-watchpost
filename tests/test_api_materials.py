from pathlib import Path

import pytest

from careeros.api.main import create_app
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.materials.upload import (
    _ADMISSION,
    UploadHeaderError,
    UploadMetadataError,
    validate_scope,
)
from tests.support import TestClient


def _client(tmp_path: Path):
    db = tmp_path / "db.sqlite3"
    root = tmp_path / "materials"
    c = connect(db)
    initialize_schema(c)
    c.close()
    return TestClient(create_app(db, root), auto_write=True), root


def _headers(**extra):
    base = {}
    base.update(extra)
    return base


def test_material_list_is_empty_and_private_free(tmp_path):
    with _client(tmp_path)[0] as client:
        response = client.get("/materials", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_raw_create_and_location(tmp_path):
    client, root = _client(tmp_path)
    with client:
        response = client.post(
            "/materials",
            headers=_headers(
                **{
                    "Content-Type": "application/octet-stream",
                    "X-CareerOS-Write": "1",
                    "X-CareerOS-Material-Name": "Resume",
                    "X-CareerOS-Material-Category": "resume",
                    "X-CareerOS-Display-Filename": "cv.pdf",
                    "X-CareerOS-Declared-Media-Type": "application/pdf",
                }
            ),
            content=b"hello",
        )
    assert response.status_code == 201
    assert response.headers["location"].startswith("/materials/")
    body = response.json()
    assert set(body) == {
        "material_id",
        "name",
        "category",
        "status",
        "revision",
        "version_count",
        "current_version",
        "retired_at",
        "created_at",
        "updated_at",
        "versions",
    }
    assert "sha256" not in response.text and "storage_key" not in response.text
    assert root.exists()


@pytest.mark.parametrize(
    "content_type", ["multipart/form-data", "application/octet-stream; charset=utf-8"]
)
def test_upload_transport_rejected(tmp_path, content_type):
    client, root = _client(tmp_path)
    with client:
        response = client.post(
            "/materials",
            headers=_headers(**{"Content-Type": content_type}),
            content=b"x",
        )
    assert response.status_code == 415
    assert not root.exists()


def test_oversized_content_length_rejected_before_root(tmp_path):
    client, root = _client(tmp_path)
    with client:
        response = client.post(
            "/materials",
            headers=_headers(
                **{
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(25 * 1024 * 1024 + 1),
                }
            ),
            content=b"",
        )
    assert response.status_code == 413
    assert response.headers.get("connection", "").lower() == "close"
    assert not root.exists()


def test_detail_unknown_is_404(tmp_path):
    with _client(tmp_path)[0] as client:
        response = client.get("/materials/" + "a" * 32, headers=_headers())
    assert response.status_code == 404


def test_download_unknown_is_404(tmp_path):
    with _client(tmp_path)[0] as client:
        response = client.get(
            "/material-versions/" + "a" * 32 + "/content", headers=_headers()
        )
    assert response.status_code == 404


def test_ast_forbidden_upload_apis_absent():
    source = Path("src/careeros/api/main.py").read_text()
    assert "request.body(" not in source
    assert "request.form(" not in source
    assert "UploadFile" not in source
    assert "python-multipart" not in source


def _create_headers(**extra):
    headers = {
        "Content-Type": "application/octet-stream",
        "X-CareerOS-Material-Name": "Resume",
        "X-CareerOS-Material-Category": "resume",
        "X-CareerOS-Display-Filename": "cv.pdf",
    }
    headers.update(extra)
    return headers


def test_media_type_missing_and_empty_default(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        for value in (None, ""):
            headers = _create_headers()
            if value is not None:
                headers["X-CareerOS-Declared-Media-Type"] = value
            response = client.post("/materials", headers=headers, content=b"x")
            assert response.status_code == 201
            assert response.json()["current_version"]["declared_media_type"] == (
                "application/octet-stream"
            )


def test_add_version_media_type_missing_and_empty_default(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"x")
        mid = created.json()["material_id"]
        for revision, value in enumerate((None, ""), start=1):
            headers = {"X-CareerOS-Material-Revision": str(revision)}
            if value is not None:
                headers["X-CareerOS-Declared-Media-Type"] = value
            response = client.post(
                f"/materials/{mid}/versions",
                headers={**_create_headers(), **headers},
                content=(b"y" if value is None else b"z"),
            )
            assert response.status_code == 201
            assert response.json()["current_version"]["declared_media_type"] == (
                "application/octet-stream"
            )


def test_zero_body_is_validation_error_and_does_not_leave_stage(tmp_path):
    client, root = _client(tmp_path)
    with client:
        response = client.post("/materials", headers=_create_headers(), content=b"")
    assert response.status_code == 422
    assert list((root / ".staging").iterdir()) == []


@pytest.mark.parametrize(
    "header, value",
    [
        ("X-CareerOS-Material-Name", "%ZZ"),
        ("X-CareerOS-Material-Name", "%FF"),
        ("X-CareerOS-Display-Filename", "a" * 3073),
        ("X-CareerOS-Declared-Media-Type", "text/plain\x00"),
    ],
)
def test_hostile_metadata_is_rejected_before_root_creation(tmp_path, header, value):
    client, root = _client(tmp_path)
    headers = _create_headers(**{header: value})
    with client:
        response = client.post("/materials", headers=headers, content=b"x")
    assert response.status_code in (400, 422)
    assert not root.exists()


def test_streamed_oversize_is_413_and_cleans_stage(tmp_path):
    client, root = _client(tmp_path)
    with client:
        response = client.post(
            "/materials",
            headers=_create_headers(),
            content=b"x" * (25 * 1024 * 1024 + 1),
        )
    assert response.status_code == 413
    assert response.headers.get("connection", "").lower() == "close"
    if root.exists():
        assert not list((root / ".staging").iterdir())


def test_falsely_small_content_length_remains_stream_bounded(tmp_path):
    client, root = _client(tmp_path)
    headers = _create_headers(**{"Content-Length": "1"})
    with client:
        response = client.post("/materials", headers=headers, content=b"hello")
    assert response.status_code == 201
    assert not list((root / ".staging").iterdir())


def test_patch_invalid_metadata_and_zero_revision_are_422(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"x")
        material_id = created.json()["material_id"]
        assert (
            client.patch(
                f"/materials/{material_id}",
                headers={"X-CareerOS-Material-Revision": "0"},
                json={"name": "New"},
            ).status_code
            == 422
        )
        assert (
            client.patch(
                f"/materials/{material_id}",
                headers={"X-CareerOS-Material-Revision": "1"},
                json={"name": ""},
            ).status_code
            == 422
        )


def test_download_success_headers_and_tamper_is_private_500(tmp_path):
    client, root = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"hello")
        version_id = created.json()["current_version"]["version_id"]
        response = client.get(f"/material-versions/{version_id}/content")
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-length"] == "5"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "no-store"
    blob = next((root / "objects").glob("*/*/*.blob"))
    blob.write_bytes(b"tampered")
    with client:
        tampered = client.get(f"/material-versions/{version_id}/content")
    assert tampered.status_code == 500
    assert "tampered" not in tampered.text and str(root) not in tampered.text


def test_add_missing_material_returns_404_without_store(tmp_path):
    client, root = _client(tmp_path)
    with client:
        response = client.post(
            "/materials/" + "a" * 32 + "/versions",
            headers=_create_headers(**{"X-CareerOS-Material-Revision": "1"}),
            content=b"x",
        )
    assert response.status_code == 404
    assert not (root / ".careeros-store").exists()
    assert not list((root / "objects").iterdir())


def test_add_duplicate_ignores_stale_revision_and_preserves_state(tmp_path):
    client, root = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"x")
        mid = created.json()["material_id"]
        before = client.get(f"/materials/{mid}").json()
        response = client.post(
            f"/materials/{mid}/versions",
            headers=_create_headers(**{"X-CareerOS-Material-Revision": "999"}),
            content=b"x",
        )
        after = client.get(f"/materials/{mid}").json()
    assert response.status_code == 200
    assert after == before
    assert list((root / ".staging").iterdir()) == []


def test_add_distinct_stale_revision_returns_exact_conflict(tmp_path):
    client, root = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"one")
        mid = created.json()["material_id"]
        response = client.post(
            f"/materials/{mid}/versions",
            headers=_create_headers(**{"X-CareerOS-Material-Revision": "999"}),
            content=b"two",
        )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": "material_revision_conflict", "current_revision": 1}
    }
    assert not list((root / ".staging").iterdir())


def test_add_version_limit_returns_exact_conflict(tmp_path):
    client, root = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"seed")
        mid = created.json()["material_id"]
    db = tmp_path / "db.sqlite3"
    c = connect(db)
    c.executemany(
        "INSERT INTO material_versions VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                f"{n:032x}",
                mid,
                n,
                "old.pdf",
                "application/pdf",
                1,
                f"{n:064x}",
                f"{n:064x}",
                f"2026-01-01T00:00:{n:02d}+00:00",
            )
            for n in range(2, 101)
        ],
    )
    c.commit()
    c.close()
    with client:
        response = client.post(
            f"/materials/{mid}/versions",
            headers=_create_headers(**{"X-CareerOS-Material-Revision": "1"}),
            content=b"new-content",
        )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {"code": "material_version_limit_reached", "current_revision": 1}
    }
    assert not list((root / ".staging").iterdir())


def test_add_retired_is_conflict_even_for_duplicate(tmp_path):
    client, _ = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"x")
        mid = created.json()["material_id"]
        assert (
            client.put(
                f"/materials/{mid}/retirement",
                headers={"X-CareerOS-Material-Revision": "1"},
            ).status_code
            == 200
        )
        response = client.post(
            f"/materials/{mid}/versions",
            headers=_create_headers(**{"X-CareerOS-Material-Revision": "2"}),
            content=b"x",
        )
    assert response.status_code == 409


def test_bound_root_missing_fails_before_recreation(tmp_path):
    client, root = _client(tmp_path)
    with client:
        created = client.post("/materials", headers=_create_headers(), content=b"x")
        mid = created.json()["material_id"]
    renamed = root.with_name("renamed-materials")
    root.rename(renamed)
    response = client.post("/materials", headers=_create_headers(), content=b"x")
    assert response.status_code == 409
    response = client.post(
        f"/materials/{mid}/versions",
        headers=_create_headers(**{"X-CareerOS-Material-Revision": "1"}),
        content=b"y",
    )
    assert response.status_code == 409
    assert not root.exists()


def test_finalization_lock_contention_is_503(tmp_path):
    client, _ = _client(tmp_path)
    database = tmp_path / "db.sqlite3"
    with collection_lock(database, command="test"):
        response = client.post("/materials", headers=_create_headers(), content=b"x")
    assert response.status_code == 503


def test_admission_contention_is_503_before_root_creation(tmp_path):
    client, root = _client(tmp_path)
    assert _ADMISSION.acquire(blocking=False)
    try:
        with client:
            response = client.post(
                "/materials", headers=_create_headers(), content=b"x"
            )
    finally:
        _ADMISSION.release()
    assert response.status_code == 503
    assert not root.exists()


@pytest.mark.parametrize(
    "headers",
    [
        [
            ("Content-Type", "application/octet-stream"),
            ("Content-Type", "application/octet-stream"),
        ],
        [("X-CareerOS-Material-Name", "A"), ("X-CareerOS-Material-Name", "B")],
        [("Content-Length", "1"), ("Content-Length", "2")],
        [("Content-Length", "not-a-number")],
    ],
)
def test_duplicate_or_malformed_raw_headers_rejected_before_root(tmp_path, headers):
    client, root = _client(tmp_path)
    # TestClient's normal helper uses a mapping and cannot preserve duplicates;
    # validate_scope is the same raw-ASGI admission parser used by both routes.
    scope = {
        "headers": [
            (key.lower().encode("ascii"), value.encode("ascii"))
            for key, value in headers
        ]
    }
    with pytest.raises((UploadHeaderError, UploadMetadataError)):
        validate_scope(scope)
    assert not root.exists()


def test_connect_current_rw_schema_race_is_fixed_500(tmp_path, monkeypatch):
    client, root = _client(tmp_path)

    def fail(_database):
        raise ValueError("private schema detail")

    monkeypatch.setattr("careeros.api.main.connect_current_rw", fail)
    response = client.post("/materials", headers=_create_headers(), content=b"x")
    assert response.status_code == 500
    assert "private schema detail" not in response.text
    if root.exists():
        assert list((root / ".staging").iterdir()) == []


def test_read_only_database_race_is_private_fixed_500(tmp_path, monkeypatch):
    client, root = _client(tmp_path)

    def fail(database):
        raise FileNotFoundError(f"private database path: {database}")

    monkeypatch.setattr("careeros.api.main.connect_read_only", fail)
    response = client.post("/materials", headers=_create_headers(), content=b"x")
    assert response.status_code == 500
    assert "private database path" not in response.text
    assert str(tmp_path) not in response.text
    if root.exists():
        assert list((root / ".staging").iterdir()) == []
