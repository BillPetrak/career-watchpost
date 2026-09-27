"""Focused presentation-boundary checks for the M7 Career Profile API."""

from __future__ import annotations

from pathlib import Path

import pytest

import careeros.api.main as api_main
import careeros.career_profile.workspace as workspace
from careeros.api.main import create_app
from careeros.api.schemas import CareerProfileReviewRequest, CareerProfileSaveRequest
from careeros.database.career_profile import CareerProfileConflict
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.materials.service import create_material
from careeros.materials.storage import stage_bytes
from tests.support import TestClient
from tests.test_resume_docx_extraction_m3 import body, package
from tests.test_resume_extraction_m3 import pdf_pages


@pytest.fixture
def profile_api(tmp_path: Path):
    database = tmp_path / "profile.sqlite3"
    root = tmp_path / "materials"
    connection = connect(database)
    initialize_schema(connection)
    text = (
        "Name: Ada Example\nEmail: ada@example.test\nPhone: +30 210 123 4567\n"
        "Portfolio: https://example.test/work\nSkills: Python, Rust\n"
        "Languages: English\nExperience:\n"
        "- Engineer at Fictional Labs - 2020-01 - 2022\nEducation:\n"
        "- MSc at Example University - 2018 - 2020\nCertifications:\n"
        "- Cloud Certificate - Example Org - 2024"
    )
    content = pdf_pages([text])
    stage, size, digest = stage_bytes(root, [content])
    detail = create_material(
        connection,
        root,
        name="Fictional résumé",
        category="resume",
        filename="fictional.pdf",
        media_type="application/pdf",
        stage=stage,
        size=size,
        sha256=digest,
    )
    connection.close()
    client = TestClient(create_app(database=database, materials_root_path=root))
    return {
        "client": client,
        "database": database,
        "root": root,
        "material_id": str(detail["material_id"]),
        "version_id": str(detail["current_version"]["version_id"]),
    }


def _analyze(profile_api: dict[str, object]) -> dict[str, object]:
    client = profile_api["client"]
    assert isinstance(client, TestClient)
    response = client.post(
        "/career-profile/sources/"
        + str(profile_api["material_id"])
        + "/versions/"
        + str(profile_api["version_id"])
        + "/analyze"
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_profile_request_models_reject_authority_fields_and_non_object_edit():
    try:
        CareerProfileSaveRequest.model_validate(
            {
                "expected_revision": 0,
                "entries": [
                    {
                        "kind": "candidate",
                        "primary_candidate_id": "candidate-1",
                        "normalized_key": "PRIVATE-SENTINEL",
                    }
                ],
            }
        )
    except ValueError:
        pass
    else:
        raise AssertionError("authority fields must not be accepted")
    try:
        CareerProfileReviewRequest.model_validate(
            {"decision": "approved_edit", "edited_value": ["PRIVATE-SENTINEL"]}
        )
    except ValueError:
        pass
    else:
        raise AssertionError("edited values must be JSON objects")


def test_missing_database_profile_reads_are_safe(tmp_path: Path):
    client = TestClient(
        create_app(database=tmp_path / "missing.sqlite3"), auto_write=False
    )
    response = client.get("/career-profile/current")
    assert response.status_code == 500
    assert "missing.sqlite3" not in response.text
    assert "PRIVATE-SENTINEL" not in response.text


def test_profile_validation_never_echoes_submitted_secret(tmp_path: Path):
    client = TestClient(
        create_app(database=tmp_path / "missing.sqlite3"), auto_write=True
    )
    response = client.put(
        "/career-profile/candidates/candidate-1/review",
        json={
            "decision": "approved_edit",
            "edited_value": {"PRIVATE-SENTINEL": "PRIVATE-SENTINEL"},
        },
    )
    assert response.status_code in {403, 500}
    assert "PRIVATE-SENTINEL" not in response.text


def test_profile_routes_are_registered():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/career-profile/sources" in paths
    assert "/career-profile/current" in paths
    assert "/career-profile/history" in paths
    assert "/career-profile/versions" in paths


def test_initialized_profile_current_and_pagination_validation(tmp_path: Path):
    database = tmp_path / "profile.sqlite3"
    connection = connect(database)
    try:
        initialize_schema(connection)
    finally:
        connection.close()
    client = TestClient(create_app(database=database), auto_write=False)
    current = client.get("/career-profile/current")
    assert current.status_code == 200
    assert current.json()["revision"] == 0
    invalid = client.get("/career-profile/candidates/run?evidence_limit=101")
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": {"code": "invalid_career_profile_request"}}
    assert "profile.sqlite3" not in invalid.text


def test_empty_and_semantic_noop_saves_use_revision_cas(tmp_path: Path):
    database = tmp_path / "profile.sqlite3"
    connection = connect(database)
    try:
        initialize_schema(connection)
    finally:
        connection.close()
    client = TestClient(create_app(database=database))
    first = client.post(
        "/career-profile/versions", json={"expected_revision": 0, "entries": []}
    )
    assert first.status_code == 200
    assert first.json()["revision"] == 1
    second = client.post(
        "/career-profile/versions", json={"expected_revision": 1, "entries": []}
    )
    assert second.status_code == 200
    assert second.json()["revision"] == 2
    stale = client.post(
        "/career-profile/versions", json={"expected_revision": 1, "entries": []}
    )
    assert stale.status_code == 409
    assert stale.json() == {
        "detail": {"code": "career_profile_revision_conflict", "current_revision": 2}
    }


def test_sources_analyze_candidates_and_exact_evidence(profile_api) -> None:
    client: TestClient = profile_api["client"]
    sources = client.get("/career-profile/sources")
    assert sources.status_code == 200
    payload = sources.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == profile_api["material_id"]
    rendered = sources.text
    for forbidden in ("storage_key", "normalized_key", str(profile_api["root"])):
        assert forbidden not in rendered

    first = _analyze(profile_api)
    second = _analyze(profile_api)
    assert second["extraction_run_id"] == first["extraction_run_id"]
    assert second["fact_run_id"] == first["fact_run_id"]
    candidates = client.get(
        f"/career-profile/fact-runs/{first['fact_run_id']}/candidates?limit=100"
    )
    assert candidates.status_code == 200
    candidate_items = candidates.json()["items"]
    assert candidate_items
    assert "normalized_key" not in candidates.text
    candidate = next(item for item in candidate_items if item["fact_type"] == "skill")
    detail = client.get(
        f"/career-profile/candidates/{candidate['id']}?evidence_limit=1&history_limit=1"
    )
    assert detail.status_code == 200
    projected = detail.json()
    assert projected["evidence"]["total"] >= 1
    assert projected["evidence"]["items"][0]["source_label"] == "PDF page 1"
    assert projected["evidence"]["items"][0]["text"] in (
        "Python",
        "Rust",
    )
    assert "raw_text" not in detail.text
    assert "note" not in detail.text


def test_review_all_nine_edit_shapes_and_page_history(profile_api) -> None:
    client: TestClient = profile_api["client"]
    analyzed = _analyze(profile_api)
    response = client.get(
        f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates?limit=100"
    )
    by_type = {item["fact_type"]: item for item in response.json()["items"]}
    assert set(by_type) == {
        "identity.name",
        "contact.email",
        "contact.phone",
        "portfolio_reference",
        "skill",
        "employment",
        "education",
        "certification",
        "language",
    }
    edits = {
        "identity.name": {"name": "Ada Exampleton"},
        "contact.email": {"email": "ada.changed@example.test"},
        "contact.phone": {"phone": "+30 210 765 4321"},
        "portfolio_reference": {"url": "https://example.test/new-work"},
        "skill": {"name": "Python 3"},
        "employment": {
            "title": "Senior Engineer",
            "company": "Fictional Labs",
            "start_date": "2020-01",
            "end_date": "2022",
        },
        "education": {
            "degree": "MSc",
            "institution": "Example University",
            "start_date": "2018",
            "end_date": "2020",
        },
        "certification": {
            "name": "Cloud Certificate",
            "issuer": "Example Org",
            "date": "2024",
        },
        "language": {"name": "English (Advanced)"},
    }
    for fact_type, edited in edits.items():
        result = client.put(
            f"/career-profile/candidates/{by_type[fact_type]['id']}/review",
            json={"decision": "approved_edit", "edited_value": edited},
        )
        assert result.status_code == 200, (fact_type, result.text)
        assert result.json()["review"]["status"] == "approved_edit"
    candidate_id = by_type["skill"]["id"]
    assert (
        client.put(
            f"/career-profile/candidates/{candidate_id}/review",
            json={"decision": "rejected"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/career-profile/candidates/{candidate_id}/review",
            json={"decision": "approved"},
        ).status_code
        == 200
    )
    detail = client.get(
        f"/career-profile/candidates/{candidate_id}?history_offset=1&history_limit=1&evidence_limit=1"
    ).json()
    assert detail["review_history"]["total"] == 3
    assert detail["review_history"]["offset"] == 1
    assert len(detail["review_history"]["items"]) == 1


@pytest.mark.parametrize(
    "query",
    [
        "evidence_offset=-1",
        "evidence_limit=0",
        "evidence_limit=101",
        "history_offset=-1",
        "history_limit=0",
        "history_limit=101",
    ],
)
def test_candidate_pagination_validation_is_fixed_and_safe(
    profile_api, query: str
) -> None:
    client: TestClient = profile_api["client"]
    analyzed = _analyze(profile_api)
    candidate = client.get(
        f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates"
    ).json()["items"][0]
    response = client.get(f"/career-profile/candidates/{candidate['id']}?{query}")
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_career_profile_request"}}


def test_candidate_review_history_pages_all_events_newest_first(profile_api) -> None:
    client: TestClient = profile_api["client"]
    analyzed = _analyze(profile_api)
    candidate = client.get(
        f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates"
    ).json()["items"][0]
    candidate_id = candidate["id"]
    for index in range(25):
        response = client.put(
            f"/career-profile/candidates/{candidate_id}/review",
            json={"decision": "approved" if index % 2 == 0 else "rejected"},
        )
        assert response.status_code == 200, response.text

    page_one = client.get(
        f"/career-profile/candidates/{candidate_id}"
        "?history_offset=0&history_limit=20&evidence_limit=1"
    ).json()["review_history"]
    page_two = client.get(
        f"/career-profile/candidates/{candidate_id}"
        "?history_offset=20&history_limit=20&evidence_limit=1"
    ).json()["review_history"]
    assert page_one["total"] == page_two["total"] == 25
    assert (page_one["offset"], page_one["limit"]) == (0, 20)
    assert (page_two["offset"], page_two["limit"]) == (20, 20)
    ids = [item["id"] for item in page_one["items"] + page_two["items"]]
    assert len(ids) == len(set(ids)) == 25
    assert ids == sorted(ids, reverse=True)
    assert all("note" not in item for item in page_one["items"] + page_two["items"])


def test_candidate_detail_uses_requested_m6_history_page(
    monkeypatch, profile_api
) -> None:
    client: TestClient = profile_api["client"]
    analyzed = _analyze(profile_api)
    candidate = client.get(
        f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates"
    ).json()["items"][0]
    calls: list[tuple[int, int]] = []
    original = workspace.candidate_review_history_page

    def page(connection, candidate_id, *, offset, limit):
        calls.append((offset, limit))
        return original(connection, candidate_id, offset=offset, limit=limit)

    monkeypatch.setattr(workspace, "candidate_review_history_page", page)
    response = client.get(
        f"/career-profile/candidates/{candidate['id']}"
        "?history_offset=7&history_limit=3&evidence_limit=1"
    )
    assert response.status_code == 200
    assert calls == [(7, 3)]


def test_validation_secret_never_echoes_and_missing_resources_are_404(
    profile_api, caplog: pytest.LogCaptureFixture
) -> None:
    client: TestClient = profile_api["client"]
    secret = "PRIVATE-SENTINEL-EDITED-VALUE"
    response = client.put(
        "/career-profile/candidates/missing/review",
        json={"decision": "approved_edit", "edited_value": secret},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_career_profile_request"}}
    assert secret not in response.text
    assert secret not in caplog.text
    analyzed = _analyze(profile_api)
    candidate_id = (
        profile_api["client"]
        .get(f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates")
        .json()["items"][0]["id"]
    )
    domain_invalid = client.put(
        f"/career-profile/candidates/{candidate_id}/review",
        json={"decision": "approved_edit", "edited_value": {secret: secret}},
    )
    assert domain_invalid.status_code == 422
    assert domain_invalid.json() == {
        "detail": {"code": "invalid_career_profile_request"}
    }
    assert secret not in domain_invalid.text
    assert secret not in caplog.text
    assert client.get("/career-profile/fact-runs/missing/candidates").status_code == 404
    assert client.get("/career-profile/candidates/missing").status_code == 404
    assert client.get("/career-profile/versions/missing").status_code == 404


def test_profile_save_history_version_cas_and_no_authority_fields(profile_api) -> None:
    client: TestClient = profile_api["client"]
    analyzed = _analyze(profile_api)
    candidate = next(
        item
        for item in client.get(
            f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates?limit=100"
        ).json()["items"]
        if item["fact_type"] == "skill"
    )
    candidate_id = candidate["id"]
    assert (
        client.put(
            f"/career-profile/candidates/{candidate_id}/review",
            json={"decision": "approved"},
        ).status_code
        == 200
    )
    saved = client.post(
        "/career-profile/versions",
        json={
            "expected_revision": 0,
            "entries": [
                {
                    "kind": "candidate",
                    "primary_candidate_id": candidate_id,
                    "supporting_candidate_ids": [],
                    "distinct_identity": False,
                    "privacy_override": "private",
                }
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    current = saved.json()
    assert current["revision"] == 1
    assert current["profile"]["facts"][0]["privacy_classification"] == "private"
    assert (
        current["profile"]["facts"][0]["assembly_primary_candidate_id"] == candidate_id
    )
    rendered = saved.text
    assert "normalized_key" not in rendered
    version_id = current["profile"]["version"]["id"]
    assert client.get("/career-profile/history").json()["total"] == 1
    assert client.get(f"/career-profile/versions/{version_id}").status_code == 200
    forbidden = client.post(
        "/career-profile/versions",
        json={
            "expected_revision": 1,
            "entries": [
                {
                    "kind": "candidate",
                    "primary_candidate_id": candidate_id,
                    "normalized_key": "PRIVATE-SENTINEL",
                }
            ],
        },
    )
    assert forbidden.status_code == 422
    assert "PRIVATE-SENTINEL" not in forbidden.text


def test_lock_corruption_source_recheck_and_m5_race_are_safe(
    profile_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client: TestClient = profile_api["client"]
    database: Path = profile_api["database"]
    with collection_lock(database, command="test holder"):
        busy = client.post(
            f"/career-profile/sources/{profile_api['material_id']}/versions/{profile_api['version_id']}/analyze"
        )
    assert busy.status_code == 503
    analyzed = _analyze(profile_api)
    connection = connect(database)
    key = str(
        connection.execute(
            "SELECT storage_key FROM material_versions WHERE id=?",
            (profile_api["version_id"],),
        ).fetchone()[0]
    )
    connection.close()
    root: Path = profile_api["root"]
    (root / "objects" / key[:2] / key[2:4] / f"{key}.blob").write_bytes(b"corrupt")
    corrupt = client.post(
        f"/career-profile/sources/{profile_api['material_id']}/versions/{profile_api['version_id']}/analyze"
    )
    assert corrupt.status_code == 500
    assert str(root) not in corrupt.text and key not in corrupt.text

    candidate_id = client.get(
        f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates"
    ).json()["items"][0]["id"]

    def changed(*_args, **_kwargs):
        raise CareerProfileConflict("M5 aggregate authority changed")

    monkeypatch.setattr(api_main, "review_candidate", changed)
    conflict = client.put(
        f"/career-profile/candidates/{candidate_id}/review",
        json={"decision": "approved"},
    )
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": {"code": "career_profile_authority_changed"}}


def test_corrupt_candidate_and_locator_fail_closed_without_leakage(profile_api) -> None:
    client: TestClient = profile_api["client"]
    analyzed = _analyze(profile_api)
    candidates = client.get(
        f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates?limit=100"
    ).json()["items"]
    candidate_id = candidates[0]["id"]
    database: Path = profile_api["database"]
    connection = connect(database)
    connection.execute(
        "UPDATE resume_text_segments SET source_locator=? WHERE extraction_run_id=?",
        (
            '{"type":"docx_block","part":"/PRIVATE/PATH","kind":"paragraph","index":0}',
            analyzed["extraction_run_id"],
        ),
    )
    connection.commit()
    connection.close()
    locator = client.get(f"/career-profile/candidates/{candidate_id}")
    assert locator.status_code == 500
    assert "PRIVATE" not in locator.text

    connection = connect(database)
    connection.execute(
        "UPDATE career_fact_candidates SET value_sha256=? WHERE id=?",
        ("0" * 64, candidate_id),
    )
    connection.commit()
    connection.close()
    authority = client.get(
        f"/career-profile/fact-runs/{analyzed['fact_run_id']}/candidates"
    )
    assert authority.status_code == 500
    assert authority.json() == {"detail": "Career Profile authority is unavailable"}


def test_docx_evidence_uses_bounded_friendly_locator(tmp_path: Path) -> None:
    database = tmp_path / "docx.sqlite3"
    root = tmp_path / "materials"
    connection = connect(database)
    initialize_schema(connection)
    document = body(
        "<w:p><w:r><w:t>Skills: Python</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Languages: English</w:t></w:r></w:p>"
    )
    content = package(document=document)
    stage, size, digest = stage_bytes(root, [content])
    material = create_material(
        connection,
        root,
        name="Fictional DOCX résumé",
        category="resume",
        filename="fictional.docx",
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        stage=stage,
        size=size,
        sha256=digest,
    )
    connection.close()
    client = TestClient(create_app(database=database, materials_root_path=root))
    analyzed = client.post(
        f"/career-profile/sources/{material['material_id']}/versions/"
        f"{material['current_version']['version_id']}/analyze"
    )
    assert analyzed.status_code == 200, analyzed.text
    candidates = client.get(
        f"/career-profile/fact-runs/{analyzed.json()['fact_run_id']}/candidates"
    ).json()["items"]
    skill = next(item for item in candidates if item["fact_type"] == "skill")
    detail = client.get(f"/career-profile/candidates/{skill['id']}")
    assert detail.status_code == 200
    assert detail.json()["evidence"]["items"][0]["source_label"] == ("DOCX paragraph 1")
    assert "word/document.xml" not in detail.text
