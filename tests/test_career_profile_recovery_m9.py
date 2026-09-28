"""Bounded integrated recovery evidence for the schema-13 Career Profile."""

from __future__ import annotations

from pathlib import Path

import pytest

from careeros.career_profile.models import CandidateProfileFactRequest
from careeros.career_profile.service import (
    assemble_profile,
    candidate_review_history,
    current_profile,
    profile_history,
    review_candidate,
)
from careeros.database.admin import backup, restore
from careeros.database.career_profile import CareerProfileConflict
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.materials.service import create_material
from careeros.materials.storage import stage_bytes
from careeros.resume_intelligence.career_facts import run_deterministic_facts
from careeros.resume_intelligence.extraction import ExtractionError, extract_material
from tests.test_resume_extraction_m3 import pdf_pages

AUTHORITY_TABLES = (
    "resume_extraction_runs",
    "resume_text_segments",
    "career_fact_runs",
    "career_fact_candidates",
    "career_fact_evidence",
    "career_fact_review_events",
    "career_profile_state",
    "career_profile_versions",
    "career_profile_facts",
    "career_profile_fact_sources",
)


def _rows(connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"'))


def test_schema13_profile_survives_restart_source_loss_backup_and_restore(
    tmp_path: Path,
) -> None:
    database = tmp_path / "source.sqlite3"
    materials_root = tmp_path / "materials"
    connection = connect(database)
    initialize_schema(connection)
    payload = pdf_pages(["Name: Synthetic Person\nSkills: Python"])
    stage, size, sha256 = stage_bytes(materials_root, [payload])
    material = create_material(
        connection,
        materials_root,
        name="Synthetic recovery résumé",
        category="resume",
        filename="synthetic-recovery.pdf",
        media_type="application/pdf",
        stage=stage,
        size=size,
        sha256=sha256,
    )
    material_id = str(material["material_id"])
    version_id = str(material["current_version"]["version_id"])
    extraction = extract_material(connection, materials_root, material_id, version_id)
    fact_run, candidates, evidence = run_deterministic_facts(connection, extraction.id)
    candidate_id = next(
        candidate.id for candidate in candidates if candidate.fact_type == "skill"
    )
    candidate_evidence_count = sum(
        item.candidate_id == candidate_id for item in evidence
    )
    review_candidate(connection, candidate_id, "approved")
    first = assemble_profile(connection, 0, [CandidateProfileFactRequest(candidate_id)])
    storage_key = str(
        connection.execute(
            "SELECT storage_key FROM material_versions WHERE id=?", (version_id,)
        ).fetchone()[0]
    )
    connection.close()

    reopened = connect(database)
    assert len(evidence) > 0
    assert (
        reopened.execute(
            "SELECT count(*) FROM career_fact_evidence WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()[0]
        == candidate_evidence_count
    )
    assert len(candidate_review_history(reopened, candidate_id)) == 1
    assert current_profile(reopened).version.id == first.version.id
    assert profile_history(reopened)[0].version.id == first.version.id
    with pytest.raises(CareerProfileConflict):
        assemble_profile(reopened, 0, [CandidateProfileFactRequest(candidate_id)])
    second = assemble_profile(reopened, 1, [CandidateProfileFactRequest(candidate_id)])
    assert second.state.revision == 2
    reopened.close()

    object_path = (
        materials_root
        / "objects"
        / storage_key[:2]
        / storage_key[2:4]
        / f"{storage_key}.blob"
    )
    object_path.unlink()
    source_lost = connect(database)
    assert current_profile(source_lost).version.id == second.version.id
    assert len(profile_history(source_lost)) == 2
    with pytest.raises(ExtractionError, match="source object"):
        extract_material(source_lost, materials_root, material_id, version_id)
    expected = {table: _rows(source_lost, table) for table in AUTHORITY_TABLES}
    source_lost.close()

    backup_path = tmp_path / "schema13.backup.sqlite3"
    restored_path = tmp_path / "restored.sqlite3"
    backup(database, backup_path)
    restore(backup_path, restored_path)
    restored = connect(restored_path)
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {table: _rows(restored, table) for table in AUTHORITY_TABLES} == expected
    assert current_profile(restored).version.id == second.version.id
    with pytest.raises(ExtractionError, match="source object"):
        extract_material(
            restored, tmp_path / "mismatched-materials", material_id, version_id
        )
    restored.close()
    assert fact_run.id
