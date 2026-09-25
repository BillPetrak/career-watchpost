from __future__ import annotations

import sqlite3
from dataclasses import replace
from tempfile import TemporaryDirectory

import pytest

from careeros.career_profile.models import CareerProfileState
from careeros.database.career_profile import (
    CareerProfileConflict,
    CareerProfileNotFound,
    CareerProfileValidationError,
    ProfileFactInput,
    get_current_profile_version,
    get_profile_state,
    list_fact_sources,
    list_profile_facts,
    list_profile_versions,
    save_profile,
)
from careeros.database.schema import initialize_schema


def _db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    initialize_schema(connection)
    return connection


def _fact(name: str = "Python", sort_order: int = 0) -> ProfileFactInput:
    return ProfileFactInput(
        logical_fact_id="skill:python",
        fact_type="skill",
        normalized_key="python",
        canonical_value_json='{"name":"' + name + '"}',
        origin="manual",
        privacy_classification="public",
        sort_order=sort_order,
    )


def _seed_candidates(connection: sqlite3.Connection) -> tuple[str, str]:
    connection.execute(
        "INSERT INTO materials(id,name,category,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        ("1" * 32, "resume.txt", "resume", "t", "t"),
    )
    connection.execute(
        "INSERT INTO material_versions(id,material_id,version_number,display_filename,"
        "declared_media_type,byte_size,sha256,storage_key,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("2" * 32, "1" * 32, 1, "resume.txt", "text/plain", 1, "a" * 64, "b" * 64, "t"),
    )
    connection.execute(
        "INSERT INTO resume_extraction_runs("
        "id,material_version_id,source_name,source_media_type,"
        "source_byte_size,source_sha256,source_format,detected_format,outcome,raw_text,"
        "raw_text_sha256,segment_count,extractor_fingerprint,cache_fingerprint,"
        "created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "3" * 32,
            "2" * 32,
            "resume.txt",
            "text/plain",
            1,
            "c" * 64,
            "text",
            "text",
            "succeeded",
            "x",
            "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
            0,
            "e",
            "cache",
            "t",
        ),
    )
    connection.execute(
        "INSERT INTO career_fact_runs(id,extraction_run_id,input_kind,mode,outcome,"
        "cache_fingerprint,input_sha256,input_byte_size,schema_fingerprint,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "4" * 32,
            "3" * 32,
            "extracted",
            "deterministic",
            "succeeded",
            "fcache",
            "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
            1,
            "schema",
            "t",
        ),
    )
    ids = ("a" * 32, "b" * 32)
    for candidate_id in ids:
        connection.execute(
            "INSERT INTO career_fact_candidates("
            "id,fact_run_id,fact_type,normalized_key,"
            "canonical_value_json,value_sha256,origin) VALUES (?,?,?,?,?,?,?)",
            (
                candidate_id,
                "4" * 32,
                "skill",
                "python",
                '{"name":"Python"}',
                "d" * 64,
                "deterministic",
            ),
        )
    connection.commit()
    return ids


def test_save_advances_singleton_and_preserves_immutable_history() -> None:
    connection = _db()
    first = save_profile(connection, 0, [_fact()])
    assert get_profile_state(connection) == CareerProfileState(first.version.id, 1, 2)
    second = save_profile(connection, 1, [_fact("Go")])
    assert second.version.parent_version_id == first.version.id
    assert get_profile_state(connection) == CareerProfileState(second.version.id, 2, 3)
    assert list_profile_versions(connection)[0].version_number == 2
    current = get_current_profile_version(connection)
    assert current is not None
    assert current.id == second.version.id
    assert (
        list_profile_facts(connection, first.version.id)[0].canonical_value_json
        == '{"name":"Python"}'
    )


def test_stale_save_is_atomic_and_does_not_consume_version_number() -> None:
    connection = _db()
    save_profile(connection, 0, [_fact()])
    with pytest.raises(CareerProfileConflict):
        save_profile(connection, 0, [_fact("Stale")])
    current = get_current_profile_version(connection)
    assert current is not None
    assert get_profile_state(connection) == CareerProfileState(current.id, 1, 2)
    assert len(list_profile_versions(connection)) == 1


def test_manual_profile_fact_has_no_provenance_sources() -> None:
    connection = _db()
    result = save_profile(connection, 0, [_fact()])
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM career_profile_fact_sources WHERE profile_fact_id=?",
            (result.facts[0].id,),
        ).fetchone()[0]
        == 0
    )


def test_invalid_fact_order_rejected_before_transaction() -> None:
    connection = _db()
    with pytest.raises(ValueError, match="contiguous"):
        save_profile(connection, 0, [_fact(sort_order=1)])
    assert get_profile_state(connection).revision == 0


def test_cas_with_two_connections_has_one_winner() -> None:
    with TemporaryDirectory(prefix="careeros-profile-") as directory:
        path = f"{directory}/profile.sqlite3"
        first = sqlite3.connect(path)
        first.row_factory = sqlite3.Row
        first.execute("PRAGMA foreign_keys=ON")
        initialize_schema(first)
        second = sqlite3.connect(path)
        second.row_factory = sqlite3.Row
        second.execute("PRAGMA foreign_keys=ON")
        save_profile(first, 0, [_fact()])
        with pytest.raises(CareerProfileConflict):
            save_profile(second, 0, [_fact("other")])
        assert len(list_profile_versions(first)) == 1
        first.close()
        second.close()


def test_trigger_failure_rolls_back_inserted_version() -> None:
    connection = _db()
    connection.execute(
        "CREATE TRIGGER reject_profile_state_update BEFORE UPDATE OF revision "
        "ON career_profile_state BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    with pytest.raises(CareerProfileValidationError):
        save_profile(connection, 0, [_fact()])
    assert get_profile_state(connection).revision == 0
    assert list_profile_versions(connection) == []


def test_value_hash_mismatch_rejected_and_reads_distinguish_missing_parent() -> None:
    connection = _db()
    with pytest.raises(CareerProfileValidationError, match="hash"):
        save_profile(connection, 0, [replace(_fact(), value_sha256="a" * 64)])
    with pytest.raises(CareerProfileNotFound):
        list_profile_facts(connection, "f" * 32)
    with pytest.raises(CareerProfileValidationError, match="invalid JSON"):
        save_profile(
            connection,
            0,
            [replace(_fact(), canonical_value_json="Infinity")],
        )
    with pytest.raises(CareerProfileValidationError, match="invalid JSON"):
        save_profile(
            connection,
            0,
            [replace(_fact(), canonical_value_json="1e999")],
        )


def test_approved_candidate_provenance_is_sorted_and_atomic_on_missing_source() -> None:
    connection = _db()
    first_source, second_source = _seed_candidates(connection)
    fact = ProfileFactInput(
        "skill:python",
        "skill",
        "python",
        '{"name":"Python"}',
        "approved_candidate",
        "public",
        0,
        (first_source, second_source),
    )
    first = save_profile(connection, 0, [fact], origin="approved_facts")
    assert (
        list_profile_facts(connection, first.version.id)[0].origin
        == "approved_candidate"
    )
    assert [
        source.candidate_id
        for source in list_fact_sources(connection, first.facts[0].id)
    ] == [
        first_source,
        second_source,
    ]
    before_state = get_profile_state(connection)
    before_rows = connection.execute(
        "SELECT * FROM career_profile_fact_sources "
        "ORDER BY profile_fact_id,candidate_id"
    ).fetchall()
    bad = replace(fact, source_candidate_ids=(first_source, "f" * 32))
    with pytest.raises(CareerProfileValidationError, match="source candidate"):
        save_profile(connection, 1, [bad], origin="approved_facts")
    assert get_profile_state(connection) == before_state
    assert (
        connection.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0]
        == 1
    )
    assert (
        connection.execute(
            "SELECT * FROM career_profile_fact_sources "
            "ORDER BY profile_fact_id,candidate_id"
        ).fetchall()
        == before_rows
    )


def test_profile_history_limit_and_exact_v1_rows_survive_v2() -> None:
    connection = _db()
    first = save_profile(connection, 0, [_fact()])
    v1_rows = (
        connection.execute(
            "SELECT * FROM career_profile_versions WHERE id=?", (first.version.id,)
        ).fetchone(),
        connection.execute(
            "SELECT * FROM career_profile_facts WHERE profile_version_id=?",
            (first.version.id,),
        ).fetchall(),
        connection.execute(
            "SELECT * FROM career_profile_fact_sources WHERE profile_fact_id IN "
            "(SELECT id FROM career_profile_facts WHERE profile_version_id=?)",
            (first.version.id,),
        ).fetchall(),
    )
    save_profile(connection, 1, [_fact("Go")])
    v1_after = (
        connection.execute(
            "SELECT * FROM career_profile_versions WHERE id=?", (first.version.id,)
        ).fetchone(),
        connection.execute(
            "SELECT * FROM career_profile_facts WHERE profile_version_id=?",
            (first.version.id,),
        ).fetchall(),
        connection.execute(
            "SELECT * FROM career_profile_fact_sources WHERE profile_fact_id IN "
            "(SELECT id FROM career_profile_facts WHERE profile_version_id=?)",
            (first.version.id,),
        ).fetchall(),
    )
    assert v1_rows == v1_after
    with pytest.raises(CareerProfileValidationError):
        list_profile_versions(connection, limit=1001)
