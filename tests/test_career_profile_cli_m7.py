"""Focused Package-A projection and manifest privacy contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from careeros import cli
from careeros.career_profile.models import (
    CareerProfileFact,
    CareerProfileSnapshot,
    CareerProfileVersion,
)
from careeros.career_profile.workspace import (
    WorkspaceError,
    _safe_locator,
    parse_manifest,
    profile_projection,
)
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.materials.service import create_material
from careeros.materials.storage import stage_bytes
from tests.test_resume_extraction_m3 import pdf_pages


def _snapshot() -> CareerProfileSnapshot:
    version = CareerProfileVersion(
        "v" * 32,
        1,
        None,
        "a" * 64,
        "approved_facts",
        "2026-01-01T00:00:00+00:00",
    )
    fact = CareerProfileFact(
        "f" * 32,
        version.id,
        "candidate:" + "c" * 32,
        "identity.name",
        "secret-normalized-key",
        json.dumps({"value": "Ada Example"}, separators=(",", ":")),
        "b" * 64,
        "approved_candidate",
        "sensitive",
        0,
    )
    return CareerProfileSnapshot(version, (fact,), (("c" * 32,),))


def test_profile_projection_allowlists_values_without_internal_keys_or_hashes() -> None:
    projected = profile_projection(_snapshot())
    assert projected is not None
    rendered = json.dumps(projected)
    assert "secret-normalized-key" not in rendered
    assert "a" * 64 not in rendered
    assert "b" * 64 not in rendered
    assert projected["facts"][0]["value"] == {"value": "Ada Example"}
    assert projected["facts"][0]["source_candidate_ids"] == ["c" * 32]


@pytest.mark.parametrize(
    ("locator", "label"),
    [
        ('{"type":"pdf_page","page":2}', "PDF page 2"),
        (
            '{"type":"docx_block","part":"word/document.xml",'
            '"kind":"paragraph","index":3}',
            "DOCX paragraph 4",
        ),
        (
            '{"type":"docx_block","part":"word/document.xml","kind":"table","index":0}',
            "DOCX table 1",
        ),
    ],
)
def test_safe_locator_accepts_frozen_m3_shapes(locator: str, label: str) -> None:
    assert _safe_locator(locator) == label


@pytest.mark.parametrize(
    "locator",
    [
        '{"type":"docx_block","part":"word/document.xml",'
        '"kind":"table_cell","index":0}',
        '{"type":"docx_block","part":"/tmp/private.xml","kind":"paragraph","index":0}',
        '{"type":"pdf_page","page":0}',
        '{"type":"pdf_page","page":true}',
        '{"type":"docx_block","part":"word/document.xml",'
        '"kind":"paragraph","index":true}',
        '{"type":"docx_block","part":"word/document.xml",'
        '"kind":"paragraph","index":0,"secret":"x"}',
        "not-json",
    ],
)
def test_safe_locator_rejects_malformed_or_unbounded_shapes(locator: str) -> None:
    with pytest.raises(WorkspaceError):
        _safe_locator(locator)


def test_manifest_rejects_authority_fields_and_accepts_empty_snapshot() -> None:
    assert parse_manifest('{"entries": []}') == ()
    with pytest.raises(WorkspaceError):
        parse_manifest(
            '{"entries":[{"kind":"candidate","primary_candidate_id":"c",'
            '"normalized_key":"private"}]}'
        )


def _material_workspace(tmp_path: Path) -> tuple[Path, Path, str, str]:
    database = tmp_path / "m7.sqlite3"
    root = tmp_path / "materials"
    connection = connect(database)
    initialize_schema(connection)
    content = pdf_pages(
        [
            "Name: Ada Example\\nEmail: ada@example.test\\n"
            "Skills: Python, Rust\\nLanguages: English"
        ]
    )
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
    return (
        database,
        root,
        str(detail["material_id"]),
        str(detail["current_version"]["version_id"]),
    )


def _analyze(
    database: Path,
    root: Path,
    material_id: str,
    version_id: str,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, object]:
    result = cli.main(
        [
            "career-profile",
            "analyze",
            material_id,
            version_id,
            "--database",
            str(database),
            "--materials-root",
            str(root),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0, captured.err
    return json.loads(captured.out)


def test_cli_exact_analyze_review_profile_lifecycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, root, material_id, version_id = _material_workspace(tmp_path)
    assert (
        cli.main(
            [
                "career-profile",
                "sources",
                "--database",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    lineage = json.loads(capsys.readouterr().out)
    assert lineage["total"] == 1
    assert lineage["items"][0]["id"] == material_id
    assert lineage["items"][0]["versions"][0]["id"] == version_id
    analyzed = _analyze(database, root, material_id, version_id, capsys)
    run_id = str(analyzed["fact_run_id"])
    assert analyzed["candidate_count"]

    assert (
        cli.main(
            [
                "career-profile",
                "candidates",
                run_id,
                "--database",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    candidates_payload = json.loads(capsys.readouterr().out)
    candidate = next(
        item for item in candidates_payload["items"] if item["fact_type"] == "skill"
    )
    candidate_id = candidate["id"]
    candidate_value = candidate["value"]["name"]
    assert "normalized_key" not in json.dumps(candidates_payload)

    assert (
        cli.main(
            [
                "career-profile",
                "candidate",
                candidate_id,
                "--database",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    detail = json.loads(capsys.readouterr().out)
    assert detail["evidence"]["total"] >= 1
    assert detail["evidence"]["items"][0]["source_label"] == "PDF page 1"
    assert detail["review_history"]["total"] == 0

    assert (
        cli.main(
            [
                "career-profile",
                "review",
                candidate_id,
                "approved",
                "--database",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "approved"

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "kind": "candidate",
                        "primary_candidate_id": candidate_id,
                        "supporting_candidate_ids": [],
                        "distinct_identity": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "career-profile",
                "save",
                str(manifest),
                "--expected-revision",
                "0",
                "--database",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    saved = json.loads(capsys.readouterr().out)
    assert saved["revision"] == 1
    assert saved["fact_count"] == 1

    for command in ("show", "history"):
        assert (
            cli.main(
                [
                    "career-profile",
                    command,
                    "--database",
                    str(database),
                    "--format",
                    "json",
                ]
            )
            == 0
        )
        output = capsys.readouterr().out
        assert candidate_value in output
        assert "normalized_key" not in output

    edit_file = tmp_path / "private-edit.json"
    edit_file.write_text('{"name":"Python 3"}', encoding="utf-8")
    assert (
        cli.main(
            [
                "career-profile",
                "review",
                candidate_id,
                "approved_edit",
                "--value-file",
                str(edit_file),
                "--database",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "approved_edit"
    assert (
        cli.main(
            [
                "career-profile",
                "review",
                candidate_id,
                "rejected",
                "--database",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()

    # Later rejection does not rewrite the immutable historical snapshot.
    assert (
        cli.main(
            [
                "career-profile",
                "show",
                "--version",
                saved["version"]["id"],
                "--database",
                str(database),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert candidate_value in capsys.readouterr().out


def test_cli_review_rejects_candidate_outside_recognized_m5_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, root, material_id, version_id = _material_workspace(tmp_path)
    analyzed = _analyze(database, root, material_id, version_id, capsys)
    connection = connect(database)
    candidate_id = str(
        connection.execute(
            "SELECT id FROM career_fact_candidates WHERE fact_run_id=? "
            "ORDER BY id LIMIT 1",
            (analyzed["fact_run_id"],),
        ).fetchone()[0]
    )
    connection.execute(
        "UPDATE career_fact_runs SET outcome='failed' WHERE id=?",
        (analyzed["fact_run_id"],),
    )
    connection.commit()
    connection.close()

    result = cli.main(
        [
            "career-profile",
            "review",
            candidate_id,
            "approved",
            "--database",
            str(database),
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "Career Profile operation failed." in captured.err


def test_cli_empty_noop_stale_and_file_error_are_privacy_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, _root, _material_id, _version_id = _material_workspace(tmp_path)
    empty = tmp_path / "empty.json"
    empty.write_text('{"entries":[]}', encoding="utf-8")
    base = [
        "career-profile",
        "save",
        str(empty),
        "--database",
        str(database),
    ]
    assert cli.main([*base, "--expected-revision", "0"]) == 0
    capsys.readouterr()
    # A deliberate semantic no-op still creates a new immutable version.
    assert cli.main([*base, "--expected-revision", "1", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 2
    assert cli.main([*base, "--expected-revision", "0"]) == 1
    stale = capsys.readouterr()
    assert "entries" not in stale.err

    secret_path = tmp_path / "PRIVATE-SENTINEL-manifest.json"
    secret_path.write_text('{"entries": [', encoding="utf-8")
    assert (
        cli.main(
            [
                "career-profile",
                "save",
                str(secret_path),
                "--expected-revision",
                "2",
                "--database",
                str(database),
            ]
        )
        == 2
    )
    failure = capsys.readouterr()
    assert "PRIVATE-SENTINEL" not in failure.err


def test_cli_cached_analyze_revalidates_source_and_lock_contention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database, root, material_id, version_id = _material_workspace(tmp_path)
    first = _analyze(database, root, material_id, version_id, capsys)
    second = _analyze(database, root, material_id, version_id, capsys)
    assert second["extraction_run_id"] == first["extraction_run_id"]
    assert second["fact_run_id"] == first["fact_run_id"]

    with collection_lock(database, command="test holder"):
        assert (
            cli.main(
                [
                    "career-profile",
                    "analyze",
                    material_id,
                    version_id,
                    "--database",
                    str(database),
                    "--materials-root",
                    str(root),
                ]
            )
            == 75
        )
    capsys.readouterr()

    connection = connect(database)
    key = str(
        connection.execute(
            "SELECT storage_key FROM material_versions WHERE id=?", (version_id,)
        ).fetchone()[0]
    )
    connection.close()
    (root / "objects" / key[:2] / key[2:4] / f"{key}.blob").write_bytes(b"corrupt")
    assert (
        cli.main(
            [
                "career-profile",
                "analyze",
                material_id,
                version_id,
                "--database",
                str(database),
                "--materials-root",
                str(root),
            ]
        )
        == 1
    )
    error = capsys.readouterr().err
    assert str(root) not in error
    assert key not in error
