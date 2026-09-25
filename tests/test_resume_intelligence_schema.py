from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.schema import initialize_schema


def _insert_material_version(connection: sqlite3.Connection) -> str:
    connection.execute(
        """
        INSERT INTO material_store(singleton, store_id, created_at)
        VALUES (1, ?, '2026-01-01T00:00:00+00:00')
        """,
        ("1" * 32,),
    )
    connection.execute(
        """
        INSERT INTO materials(id, name, category, created_at, updated_at)
        VALUES (?, 'Resume', 'resume',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00')
        """,
        ("2" * 32,),
    )
    version_id = "3" * 32
    connection.execute(
        """
        INSERT INTO material_versions(
            id, material_id, version_number, display_filename,
            declared_media_type, byte_size, sha256, storage_key, created_at
        ) VALUES (?, ?, 1, 'resume.txt', 'text/plain', 6, ?, ?,
                  '2026-01-01T00:00:00+00:00')
        """,
        (version_id, "2" * 32, "4" * 64, "5" * 64),
    )
    return version_id


def _insert_extraction(
    connection: sqlite3.Connection, run_id: str, material_version_id: str
) -> None:
    connection.execute(
        """
        INSERT INTO resume_extraction_runs(
            id, material_version_id, source_name, source_media_type,
            source_byte_size, source_sha256, source_format, detected_format,
            outcome, raw_text, raw_text_sha256, segment_count,
            extractor_fingerprint, cache_fingerprint, created_at
        ) VALUES (?, ?, 'resume.txt', 'text/plain', 6, ?, 'text', 'text',
                  'succeeded', 'abcdef', ?, 1, 'extractor-v1', ?,
                  '2026-01-01T00:00:00+00:00')
        """,
        (run_id, material_version_id, "4" * 64, "6" * 64, f"cache-{run_id}"),
    )


def _insert_sanitization(
    connection: sqlite3.Connection, run_id: str, extraction_run_id: str
) -> None:
    connection.execute(
        """
        INSERT INTO resume_sanitization_runs(
            id, extraction_run_id, parent_sanitization_run_id,
            sanitized_text, sanitized_text_sha256, sanitizer_fingerprint,
            policy_fingerprint, override_manifest_json, cache_fingerprint,
            outcome, created_at
        ) VALUES (?, ?, NULL, '[X]', ?, 'sanitizer-v1', 'policy-v1', '{}', ?,
                  'succeeded', '2026-01-01T00:00:00+00:00')
        """,
        (run_id, extraction_run_id, "7" * 64, f"cache-{run_id}"),
    )


@pytest.fixture
def schema_connection(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "schema.sqlite3")
    initialize_schema(connection)
    material_version_id = _insert_material_version(connection)
    _insert_extraction(connection, "a" * 32, material_version_id)
    _insert_extraction(connection, "b" * 32, material_version_id)
    _insert_sanitization(connection, "c" * 32, "a" * 32)
    _insert_sanitization(connection, "d" * 32, "b" * 32)
    connection.commit()
    yield connection
    connection.close()


def test_span_coordinate_space_and_review_constraints(
    schema_connection: sqlite3.Connection,
) -> None:
    connection = schema_connection
    invalid_spans = (
        ("copy", 0, 2, 0, 1),
        ("replace", 0, 2, 0, 0),
        ("delete", 0, 2, 0, 1),
    )
    for ordinal, (kind, raw_start, raw_end, clean_start, clean_end) in enumerate(
        invalid_spans
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO resume_sanitization_spans(
                    sanitization_run_id, ordinal, kind,
                    raw_start_byte, raw_end_byte,
                    sanitized_start_byte, sanitized_end_byte,
                    raw_slice_sha256, sanitized_slice_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "c" * 32,
                    ordinal,
                    kind,
                    raw_start,
                    raw_end,
                    clean_start,
                    clean_end,
                    "8" * 64,
                    "8" * 64,
                ),
            )

    connection.execute(
        """
        INSERT INTO career_fact_runs(
            id, extraction_run_id, sanitization_run_id, input_kind, mode,
            extractor_fingerprint, schema_fingerprint, outcome,
            cache_fingerprint, input_sha256, input_byte_size, created_at
        ) VALUES (?, ?, NULL, 'extracted', 'deterministic', 'extractor-v1',
                  'schema-v1', 'succeeded', 'fact-cache', ?, 6,
                  '2026-01-01T00:00:00+00:00')
        """,
        ("e" * 32, "a" * 32, "9" * 64),
    )
    connection.execute(
        """
        INSERT INTO career_fact_candidates(
            id, fact_run_id, fact_type, normalized_key,
            canonical_value_json, value_sha256, confidence, origin
        ) VALUES (?, ?, 'skill', 'python', '"Python"', ?, 1.0,
                  'deterministic')
        """,
        ("f" * 32, "e" * 32, "a" * 64),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO career_fact_evidence(
                candidate_id, ordinal, coordinate_space,
                start_byte, end_byte, slice_sha256
            ) VALUES (?, 0, 'raw', 0, 1, ?)
            """,
            ("f" * 32, "b" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO career_fact_review_events(
                candidate_id, event_kind, edited_value_json,
                edited_value_sha256, created_at
            ) VALUES (?, 'approved', '"Edited"', ?,
                      '2026-01-01T00:00:00+00:00')
            """,
            ("f" * 32, "c" * 64),
        )


def test_same_extraction_and_resolved_span_foreign_keys(
    schema_connection: sqlite3.Connection,
) -> None:
    connection = schema_connection
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO resume_sanitization_runs(
                id, extraction_run_id, parent_sanitization_run_id,
                sanitized_text, sanitized_text_sha256,
                sanitizer_fingerprint, policy_fingerprint,
                override_manifest_json, cache_fingerprint, outcome, created_at
            ) VALUES (?, ?, ?, 'x', ?, 'sanitizer', 'policy', '{}',
                      'mismatch-parent', 'succeeded',
                      '2026-01-01T00:00:00+00:00')
            """,
            ("1" * 32, "b" * 32, "c" * 32, "d" * 64),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO career_fact_runs(
                id, extraction_run_id, sanitization_run_id,
                input_kind, mode, schema_fingerprint, provider_fingerprint,
                model_fingerprint, prompt_fingerprint, outcome,
                cache_fingerprint, input_sha256, input_byte_size,
                token_count, created_at
            ) VALUES (?, ?, ?, 'sanitized', 'model', 'schema', 'provider',
                      'model', 'prompt', 'succeeded', 'mismatch-fact', ?, 3, 1,
                      '2026-01-01T00:00:00+00:00')
            """,
            ("2" * 32, "b" * 32, "c" * 32, "e" * 64),
        )

    connection.execute(
        """
        INSERT INTO resume_sanitization_spans(
            sanitization_run_id, ordinal, kind,
            raw_start_byte, raw_end_byte,
            sanitized_start_byte, sanitized_end_byte,
            raw_slice_sha256, sanitized_slice_sha256
        ) VALUES (?, 0, 'replace', 0, 6, 0, 3, ?, ?)
        """,
        ("c" * 32, "f" * 64, "0" * 64),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO resume_pii_findings(
                sanitization_run_id, ordinal, category, rule_identity,
                confidence, disposition, origin, raw_start_byte, raw_end_byte,
                sanitization_span_ordinal
            ) VALUES (?, 0, 'email', 'email-rule', 1.0, 'replace', 'rule',
                      0, 1, 0)
            """,
            ("d" * 32,),
        )


def test_profile_state_and_restrictive_provenance_constraints(
    schema_connection: sqlite3.Connection,
) -> None:
    connection = schema_connection
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO career_profile_state(
                singleton, current_profile_version_id, revision,
                next_version_number
            ) VALUES (2, NULL, 0, 1)
            """
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE career_profile_state SET revision=-1 WHERE singleton=1"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            UPDATE career_profile_state
            SET current_profile_version_id=? WHERE singleton=1
            """,
            ("0" * 32,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM resume_extraction_runs WHERE id=?", ("a" * 32,))


def test_manual_profile_fact_may_have_no_source_and_sources_are_restrictive(
    schema_connection: sqlite3.Connection,
) -> None:
    connection = schema_connection
    connection.execute(
        """
        INSERT INTO career_profile_versions(
            id, version_number, parent_version_id,
            content_fingerprint, origin, created_at
        ) VALUES (?, 1, NULL, ?, 'manual', '2026-01-01T00:00:00+00:00')
        """,
        ("1" * 32, "1" * 64),
    )
    connection.execute(
        """
        INSERT INTO career_profile_facts(
            id, profile_version_id, logical_fact_id, fact_type,
            normalized_key, canonical_value_json, value_sha256,
            origin, privacy_classification, sort_order
        ) VALUES (?, ?, 'manual:1', 'skill', 'python', '"Python"', ?,
                  'manual', 'private', 0)
        """,
        ("2" * 32, "1" * 32, "2" * 64),
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM career_profile_fact_sources WHERE profile_fact_id=?",
            ("2" * 32,),
        ).fetchone()[0]
        == 0
    )
    connection.execute(
        """
        INSERT INTO career_fact_runs(
            id, extraction_run_id, sanitization_run_id, input_kind, mode,
            extractor_fingerprint, schema_fingerprint, outcome,
            cache_fingerprint, input_sha256, input_byte_size, created_at
        ) VALUES (?, ?, NULL, 'extracted', 'deterministic', 'extractor-v1',
                  'schema-v1', 'succeeded', 'source-fact-cache', ?, 6,
                  '2026-01-01T00:00:00+00:00')
        """,
        ("3" * 32, "a" * 32, "3" * 64),
    )
    connection.execute(
        """
        INSERT INTO career_fact_candidates(
            id, fact_run_id, fact_type, normalized_key,
            canonical_value_json, value_sha256, confidence, origin
        ) VALUES (?, ?, 'skill', 'python', '"Python"', ?, 1.0,
                  'deterministic')
        """,
        ("4" * 32, "3" * 32, "4" * 64),
    )
    connection.execute(
        """
        INSERT INTO career_profile_fact_sources(profile_fact_id, candidate_id)
        VALUES (?, ?)
        """,
        ("2" * 32, "4" * 32),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM career_fact_candidates WHERE id=?", ("4" * 32,))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM career_profile_facts WHERE id=?", ("2" * 32,))
