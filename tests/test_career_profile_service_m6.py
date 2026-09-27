from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import careeros.career_profile.service as profile_service
from careeros.career_profile.models import (
    CandidateProfileFactRequest,
    ManualProfileFactRequest,
)
from careeros.career_profile.service import (
    assemble_profile,
    candidate_review_history,
    candidate_review_history_page,
    current_profile,
    effective_candidate_review,
    profile_history,
    review_candidate,
)
from careeros.database.career_profile import (
    CareerProfileConflict,
    CareerProfileValidationError,
    ProfileFactInput,
    get_profile_state,
    save_profile,
)
from careeros.database.connection import connect
from careeros.database.resume_intelligence import (
    ResumeValidationError,
    persist_extraction,
)
from careeros.database.schema import initialize_schema
from careeros.resume_intelligence.career_facts import (
    FACT_TYPES,
    run_deterministic_facts,
    validate_candidate_value,
)
from careeros.resume_intelligence.models import ResumeExtractionRun, ResumeTextSegment


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "m6.sqlite3")
    initialize_schema(c)
    c.execute("INSERT INTO material_store VALUES (1, ?, ?)", ("1" * 32, "now"))
    c.execute(
        "INSERT INTO materials VALUES (?, ?, 'resume', 1, 'active', NULL, ?, ?)",
        ("2" * 32, "Synthetic Resume", "now", "now"),
    )
    raw = (
        "Name: Ada Example\nEmail: ada@example.test\nPhone: +30 210 123 4567\n"
        "Portfolio: https://example.test/work\nSkills: Python, Rust\n"
        "Languages: English\nExperience:\n"
        "- Engineer at Fictional Labs — 2020-01 - 2022\n"
        "Education:\n- MSc at Example University — 2018-2020\n"
        "Certifications:\n- Cloud Certificate — Example Org — 2024\n"
    ).encode()
    c.execute(
        "INSERT INTO material_versions VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            "3" * 32,
            "2" * 32,
            "resume.txt",
            "text/plain",
            len(raw),
            _sha(raw),
            "4" * 64,
            "now",
        ),
    )
    c.commit()
    extraction = ResumeExtractionRun(
        "a" * 32,
        "3" * 32,
        "resume.txt",
        "text/plain",
        len(raw),
        _sha(raw),
        "text",
        "text",
        "succeeded",
        raw.decode(),
        _sha(raw),
        1,
        "extractor",
        "extract-cache",
        "2026-01-01T00:00:00+00:00",
    )
    persist_extraction(
        c,
        extraction,
        [ResumeTextSegment(extraction.id, 0, 0, len(raw), "synthetic", _sha(raw))],
    )
    run_deterministic_facts(c, extraction.id, created_at="2026-01-01T00:00:00+00:00")
    yield c
    c.close()


def _candidate(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM career_fact_candidates WHERE fact_type='skill' "
        "ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    return row


def _candidates(db: sqlite3.Connection, fact_type: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM career_fact_candidates WHERE fact_type=? ORDER BY id",
        (fact_type,),
    ).fetchall()


def test_candidate_review_history_page_is_bounded_and_ordered(db) -> None:
    candidate_id = str(_candidate(db)["id"])
    for decision in ("approved", "rejected", "approved", "rejected", "approved"):
        review_candidate(db, candidate_id, decision, note="audit")

    first = candidate_review_history_page(db, candidate_id, offset=0, limit=2)
    middle = candidate_review_history_page(db, candidate_id, offset=2, limit=2)
    final = candidate_review_history_page(db, candidate_id, offset=4, limit=2)
    beyond = candidate_review_history_page(db, candidate_id, offset=99, limit=2)

    assert first["total"] == middle["total"] == final["total"] == 5
    assert (first["offset"], first["limit"]) == (0, 2)
    assert (middle["offset"], middle["limit"]) == (2, 2)
    assert (final["offset"], final["limit"]) == (4, 2)
    assert len(first["items"]) == len(middle["items"]) == 2
    assert len(final["items"]) == 1
    assert beyond["items"] == []
    assert [
        item["id"] for item in first["items"] + middle["items"] + final["items"]
    ] == [item["id"] for item in candidate_review_history(db, candidate_id)]

    with pytest.raises(CareerProfileValidationError):
        candidate_review_history_page(db, candidate_id, offset=-1, limit=2)
    with pytest.raises(CareerProfileValidationError):
        candidate_review_history_page(db, candidate_id, offset=0, limit=0)
    with pytest.raises(CareerProfileValidationError):
        candidate_review_history_page(db, candidate_id, offset=0, limit=101)


def test_candidate_review_history_page_empty_and_missing_candidate(db) -> None:
    candidate_id = str(_candidate(db)["id"])
    assert candidate_review_history_page(db, candidate_id, offset=0, limit=20) == {
        "items": [],
        "total": 0,
        "offset": 0,
        "limit": 20,
    }
    with pytest.raises(CareerProfileValidationError):
        candidate_review_history_page(db, "missing", offset=0, limit=20)
    assert effective_candidate_review(db, candidate_id).status == "pending"


def test_candidate_review_history_page_equal_timestamp_and_page_local_integrity(
    db,
) -> None:
    candidate = _candidate(db)
    candidate_id = str(candidate["id"])
    stamp = "2026-09-26T15:42:13.123456+00:00"
    db.executemany(
        "INSERT INTO career_fact_review_events"
        "(candidate_id,event_kind,created_at) VALUES (?,?,?)",
        [(candidate_id, "approved", stamp), (candidate_id, "rejected", stamp)],
    )
    db.commit()
    equal_page = candidate_review_history_page(db, candidate_id, offset=0, limit=2)
    assert [item["id"] for item in equal_page["items"]] == sorted(
        [item["id"] for item in equal_page["items"]], reverse=True
    )

    db.execute(
        "UPDATE career_fact_review_events SET created_at='not-a-timestamp' "
        "WHERE candidate_id=? AND id=?",
        (candidate_id, equal_page["items"][1]["id"]),
    )
    db.commit()
    with pytest.raises(ResumeValidationError):
        candidate_review_history_page(db, candidate_id, offset=0, limit=2)


def test_candidate_review_history_page_does_not_validate_unrequested_corruption(
    db,
) -> None:
    candidate = _candidate(db)
    candidate_id = str(candidate["id"])
    newer = "2026-09-27T15:42:13.123456+00:00"
    older = "2026-09-26T15:42:13.123456+00:00"
    db.executemany(
        "INSERT INTO career_fact_review_events"
        "(candidate_id,event_kind,created_at) VALUES (?,?,?)",
        [(candidate_id, "approved", newer), (candidate_id, "rejected", older)],
    )
    db.commit()
    older_id = int(
        db.execute(
            "SELECT id FROM career_fact_review_events WHERE candidate_id=? "
            "ORDER BY created_at ASC, id ASC",
            (candidate_id,),
        ).fetchone()[0]
    )
    db.execute(
        "UPDATE career_fact_review_events SET created_at='0000' WHERE id=?",
        (older_id,),
    )
    db.commit()
    page = candidate_review_history_page(db, candidate_id, offset=0, limit=1)
    assert len(page["items"]) == 1
    with pytest.raises(ResumeValidationError):
        candidate_review_history(db, candidate_id)


def test_candidate_review_history_page_rejects_corrupt_approved_edit(db) -> None:
    candidate = _candidate(db)
    candidate_id = str(candidate["id"])
    stamp = "2026-09-26T15:42:13.123456+00:00"
    db.execute(
        "INSERT INTO career_fact_review_events"
        "(candidate_id,event_kind,edited_value_json,edited_value_sha256,created_at) "
        "VALUES (?,?,?,?,?)",
        (
            candidate_id,
            "approved_edit",
            candidate["canonical_value_json"],
            candidate["value_sha256"],
            stamp,
        ),
    )
    db.commit()
    event_id = int(
        db.execute(
            "SELECT id FROM career_fact_review_events WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()[0]
    )
    db.execute(
        "UPDATE career_fact_review_events SET edited_value_sha256=? WHERE id=?",
        ("0" * 64, event_id),
    )
    db.commit()
    with pytest.raises(CareerProfileValidationError):
        candidate_review_history_page(db, candidate_id, offset=0, limit=1)


def test_candidate_review_history_page_rejects_unrecognized_event_kind(db) -> None:
    candidate_id = str(_candidate(db)["id"])
    db.execute("PRAGMA ignore_check_constraints=ON")
    db.execute(
        "INSERT INTO career_fact_review_events(candidate_id,event_kind,created_at) "
        "VALUES (?,?,?)",
        (candidate_id, "unknown", "2026-09-26T15:42:13.123456+00:00"),
    )
    db.commit()
    db.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(CareerProfileValidationError):
        candidate_review_history_page(db, candidate_id, offset=0, limit=1)


def _add_resume_run(
    db: sqlite3.Connection,
    text: str,
    *,
    material_version_id: str = "5" * 32,
    extraction_id: str = "6" * 32,
) -> list[sqlite3.Row]:
    raw = text.encode()
    next_version = int(
        db.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM material_versions "
            "WHERE material_id=?",
            ("2" * 32,),
        ).fetchone()[0]
    )
    db.execute(
        "INSERT INTO material_versions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            material_version_id,
            "2" * 32,
            next_version,
            "resume-extra.txt",
            "text/plain",
            len(raw),
            _sha(raw),
            "7" * 64,
            "now",
        ),
    )
    db.commit()
    extraction = ResumeExtractionRun(
        extraction_id,
        material_version_id,
        "resume-extra.txt",
        "text/plain",
        len(raw),
        _sha(raw),
        "text",
        "text",
        "succeeded",
        text,
        _sha(raw),
        1,
        "extractor",
        "extra-extract-cache",
        "2026-01-02T00:00:00+00:00",
    )
    persist_extraction(
        db,
        extraction,
        [ResumeTextSegment(extraction.id, 0, 0, len(raw), "synthetic", _sha(raw))],
    )
    run_deterministic_facts(db, extraction.id, created_at="2026-01-02T00:00:00+00:00")
    return db.execute(
        "SELECT * FROM career_fact_candidates WHERE fact_run_id="
        "(SELECT id FROM career_fact_runs WHERE extraction_run_id=?) ORDER BY id",
        (extraction.id,),
    ).fetchall()


def test_review_retry_order_and_approved_edit_is_effective(
    db: sqlite3.Connection,
) -> None:
    candidate = _candidate(db)
    candidate_id = str(candidate["id"])
    original = tuple(candidate)
    pending = effective_candidate_review(db, candidate_id)
    assert pending.status == "pending"
    approved = review_candidate(db, candidate_id, "approved")
    assert approved.status == "approved"
    assert review_candidate(db, candidate_id, "approved") == approved
    assert len(candidate_review_history(db, candidate_id)) == 1
    edited = review_candidate(
        db, candidate_id, "approved_edit", edited_value_json='{"name":"Python 3"}'
    )
    assert edited.status == "approved_edit"
    assert edited.normalized_key != approved.normalized_key
    assert tuple(_candidate(db)) == original
    review_candidate(
        db,
        candidate_id,
        "approved_edit",
        edited_value_json='{"name":"Python 3"}',
        note="audit",
    )
    assert len(candidate_review_history(db, candidate_id)) == 3
    rejected = review_candidate(db, candidate_id, "rejected")
    assert rejected.status == "rejected"
    assert review_candidate(db, candidate_id, "approved").status == "approved"


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        ("approved", "rejected"),
        ("approved", "approved_edit"),
        ("rejected", "approved"),
        ("rejected", "approved_edit"),
        ("approved_edit", "approved"),
        ("approved_edit", "rejected"),
    ],
)
def test_every_changed_review_transition_is_legal(
    db: sqlite3.Connection, initial: str, target: str
) -> None:
    candidate_id = str(_candidate(db)["id"])

    def apply(decision: str, value: str) -> None:
        review_candidate(
            db,
            candidate_id,
            decision,
            edited_value_json=(
                '{"name":"' + value + '"}' if decision == "approved_edit" else None
            ),
        )

    apply(initial, "Initial Edit")
    apply(target, "Target Edit")
    assert effective_candidate_review(db, candidate_id).status == target
    assert len(candidate_review_history(db, candidate_id)) == 2


def test_concurrent_identical_review_retries_append_once(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    database_path = Path(str(db.execute("PRAGMA database_list").fetchone()[2]))
    db.close()
    barrier = Barrier(2)

    def approve() -> str:
        connection = connect(database_path)
        try:
            barrier.wait()
            return review_candidate(connection, candidate_id, "approved").status
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _value: approve(), range(2))) == [
            "approved",
            "approved",
        ]
    check = connect(database_path)
    try:
        assert (
            check.execute(
                "SELECT COUNT(*) FROM career_fact_review_events WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        check.close()


def test_review_history_is_complete_and_validates_edited_authority(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    for value in ("Python 2", "Python 3", "Python 4"):
        review_candidate(
            db,
            candidate_id,
            "approved_edit",
            edited_value_json='{"name":"' + value + '"}',
            note="audit",
        )
    assert len(candidate_review_history(db, candidate_id)) == 4
    db.execute(
        "UPDATE career_fact_review_events SET edited_value_sha256=? "
        "WHERE candidate_id=? AND event_kind='approved_edit'",
        ("0" * 64, candidate_id),
    )
    db.commit()
    with pytest.raises(CareerProfileValidationError, match="corrupt"):
        candidate_review_history(db, candidate_id)


def test_profile_snapshot_is_a_validated_public_read(db: sqlite3.Connection) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    saved = assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    from careeros.career_profile import profile_snapshot

    snapshot = profile_snapshot(db, saved.version.id)
    assert snapshot.version.id == saved.version.id
    assert snapshot.source_candidate_ids == ((candidate_id,),)


def test_review_authority_equal_timestamp_uses_event_id_and_rejects_malformed(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    timestamp = "2026-09-26T15:42:13.123456+00:00"
    db.execute(
        "INSERT INTO career_fact_review_events "
        "(candidate_id,event_kind,created_at) VALUES (?,?,?)",
        (candidate_id, "approved", timestamp),
    )
    db.execute(
        "INSERT INTO career_fact_review_events "
        "(candidate_id,event_kind,created_at) VALUES (?,?,?)",
        (candidate_id, "rejected", timestamp),
    )
    db.commit()
    result = effective_candidate_review(db, candidate_id)
    assert result.status == "rejected"
    db.execute(
        "UPDATE career_fact_review_events SET created_at=? WHERE candidate_id=?",
        ("2026-09-26T15:42:13Z", candidate_id),
    )
    db.commit()
    with pytest.raises(ResumeValidationError, match="canonical"):
        effective_candidate_review(db, candidate_id)


def test_approved_edit_changed_key_survives_immutable_history(
    db: sqlite3.Connection,
) -> None:
    candidate = _candidate(db)
    candidate_id = str(candidate["id"])
    review_candidate(
        db, candidate_id, "approved_edit", edited_value_json='{"name":"Python 3"}'
    )
    saved = assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    snapshot = current_profile(db)
    assert snapshot is not None
    assert snapshot.facts[0].canonical_value_json == '{"name":"Python 3"}'
    assert (
        snapshot.facts[0].normalized_key
        == effective_candidate_review(db, candidate_id).normalized_key
    )
    assert snapshot.facts[0].value_sha256 == _sha(b'{"name":"Python 3"}')
    assert profile_history(db)[0].version.id == saved.version.id
    review_candidate(db, candidate_id, "rejected")
    review_candidate(
        db, candidate_id, "approved_edit", edited_value_json='{"name":"Python 4"}'
    )
    assert current_profile(db) is not None
    assert profile_history(db)[0].facts[0].canonical_value_json == '{"name":"Python 3"}'


def test_multi_source_uses_effective_keys_and_preserves_edited_history(
    db: sqlite3.Connection,
) -> None:
    skills = _candidates(db, "skill")
    assert len(skills) == 2
    first_id, second_id = (str(item["id"]) for item in skills)
    original_rows = {str(item["id"]): tuple(item) for item in skills}
    review_candidate(db, first_id, "approved")
    with pytest.raises(CareerProfileValidationError, match="approved"):
        assemble_profile(db, 0, [CandidateProfileFactRequest(first_id, (second_id,))])
    review_candidate(db, second_id, "rejected")
    with pytest.raises(CareerProfileValidationError, match="approved"):
        assemble_profile(db, 0, [CandidateProfileFactRequest(first_id, (second_id,))])
    for candidate_id in (first_id, second_id):
        review_candidate(
            db,
            candidate_id,
            "approved_edit",
            edited_value_json='{"name":"Go"}',
        )
    saved = assemble_profile(
        db, 0, [CandidateProfileFactRequest(first_id, (second_id,))]
    )
    assert saved.facts[0].canonical_value_json == '{"name":"Go"}'
    assert current_profile(db).source_candidate_ids == (
        tuple(sorted((first_id, second_id))),
    )
    assert {
        str(row["id"]): tuple(row) for row in _candidates(db, "skill")
    } == original_rows
    review_candidate(
        db, second_id, "approved_edit", edited_value_json='{"name":"Rust 2"}'
    )
    with pytest.raises(CareerProfileValidationError, match="merge-compatible"):
        assemble_profile(db, 1, [CandidateProfileFactRequest(first_id, (second_id,))])
    assert profile_history(db)[0].facts[0].canonical_value_json == '{"name":"Go"}'


def test_primary_is_always_source_and_duplicate_input_rejected(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    result = assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    source = db.execute(
        "SELECT candidate_id FROM career_profile_fact_sources WHERE profile_fact_id=?",
        (result.facts[0].id,),
    ).fetchone()
    assert source[0] == candidate_id
    with pytest.raises(CareerProfileValidationError, match="duplicate"):
        assemble_profile(
            db,
            1,
            [CandidateProfileFactRequest(candidate_id, (candidate_id,))],
        )


def test_profile_source_origin_and_single_use_guards(db: sqlite3.Connection) -> None:
    candidate = _candidate(db)
    candidate_id = str(candidate["id"])
    review_candidate(db, candidate_id, "approved")
    with pytest.raises(CareerProfileValidationError, match="two profile facts"):
        assemble_profile(
            db,
            0,
            [
                CandidateProfileFactRequest(candidate_id),
                CandidateProfileFactRequest(candidate_id),
            ],
        )
    common = dict(
        logical_fact_id="synthetic:fact",
        fact_type=str(candidate["fact_type"]),
        normalized_key=str(candidate["normalized_key"]),
        canonical_value_json=str(candidate["canonical_value_json"]),
        privacy_classification="public",
        sort_order=0,
        value_sha256=str(candidate["value_sha256"]),
    )
    with pytest.raises(CareerProfileValidationError, match="require sources"):
        save_profile(db, 0, [ProfileFactInput(origin="approved_candidate", **common)])
    for origin in ("manual", "imported"):
        with pytest.raises(CareerProfileValidationError, match="cannot have sources"):
            save_profile(
                db,
                0,
                [
                    ProfileFactInput(
                        origin=origin,
                        source_candidate_ids=(candidate_id,),
                        **common,
                    )
                ],
            )


def test_equal_key_history_requires_deliberate_identity_choice(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    saved = assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    key = saved.facts[0].normalized_key
    request = CandidateProfileFactRequest("new-candidate")
    with pytest.raises(CareerProfileValidationError, match="equal-key"):
        profile_service._resolve_identity(db, request, ("new-candidate",), "skill", key)
    assert (
        profile_service._resolve_identity(
            db,
            CandidateProfileFactRequest(
                "new-candidate", logical_fact_id=saved.facts[0].logical_fact_id
            ),
            ("new-candidate",),
            "skill",
            key,
        )
        == saved.facts[0].logical_fact_id
    )
    assert (
        profile_service._resolve_identity(
            db,
            CandidateProfileFactRequest("new-candidate", distinct_identity=True),
            ("new-candidate",),
            "skill",
            key,
        )
        == "candidate:new-candidate"
    )


def test_logical_identity_survives_edit_support_removal_and_reappearance(
    db: sqlite3.Connection,
) -> None:
    first, second = _candidates(db, "skill")
    first_id, second_id = str(first["id"]), str(second["id"])
    review_candidate(db, first_id, "approved")
    initial = assemble_profile(db, 0, [CandidateProfileFactRequest(first_id)])
    logical_id = initial.facts[0].logical_fact_id
    review_candidate(
        db, first_id, "approved_edit", edited_value_json='{"name":"Shared"}'
    )
    edited = assemble_profile(db, 1, [CandidateProfileFactRequest(first_id)])
    assert edited.facts[0].logical_fact_id == logical_id
    assemble_profile(db, 2, [])
    restored = assemble_profile(db, 3, [CandidateProfileFactRequest(first_id)])
    assert restored.facts[0].logical_fact_id == logical_id
    review_candidate(
        db, second_id, "approved_edit", edited_value_json='{"name":"Shared"}'
    )
    supported = assemble_profile(
        db, 4, [CandidateProfileFactRequest(first_id, (second_id,))]
    )
    assert supported.facts[0].logical_fact_id == logical_id
    without_support = assemble_profile(db, 5, [CandidateProfileFactRequest(first_id)])
    assert without_support.facts[0].logical_fact_id == logical_id
    assert (
        without_support.version.content_fingerprint
        != supported.version.content_fingerprint
    )


def test_explicit_later_association_and_ambiguous_history_fail_closed(
    db: sqlite3.Connection,
) -> None:
    first_id = str(_candidate(db)["id"])
    review_candidate(db, first_id, "approved_edit", edited_value_json='{"name":"Go"}')
    initial = assemble_profile(db, 0, [CandidateProfileFactRequest(first_id)])
    logical_id = initial.facts[0].logical_fact_id
    added = _add_resume_run(db, "Skills: go\n")[0]
    added_id = str(added["id"])
    review_candidate(db, added_id, "approved")
    associated = assemble_profile(
        db,
        1,
        [
            CandidateProfileFactRequest(
                added_id,
                logical_fact_id=logical_id,
            )
        ],
    )
    assert associated.facts[0].logical_fact_id == logical_id
    assert associated.facts[0].canonical_value_json == '{"name":"go"}'
    assert associated.facts[0].origin == "approved_candidate"
    candidate = _candidate(db)
    save_profile(
        db,
        2,
        [
            ProfileFactInput(
                "synthetic:conflict",
                str(candidate["fact_type"]),
                str(candidate["normalized_key"]),
                str(candidate["canonical_value_json"]),
                "approved_candidate",
                "public",
                0,
                (first_id,),
                str(candidate["value_sha256"]),
            )
        ],
        origin="approved_facts",
    )
    with pytest.raises(CareerProfileValidationError, match="ambiguous"):
        assemble_profile(db, 3, [CandidateProfileFactRequest(first_id)])


def test_exact_prepared_state_token_is_checked_before_save(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    original = profile_service._preflight_digest

    def mutate_state(connection: sqlite3.Connection, run_id: str) -> str:
        digest = original(connection, run_id)
        connection.execute(
            "UPDATE career_profile_state SET next_version_number=9 WHERE singleton=1"
        )
        connection.commit()
        return digest

    monkeypatch.setattr(profile_service, "_preflight_digest", mutate_state)
    with pytest.raises(CareerProfileConflict):
        assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    assert db.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0] == 0


def test_prepared_current_parent_token_is_checked_even_when_revision_matches(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = assemble_profile(db, 0, [])
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    original = profile_service._preflight_digest

    def mutate_parent(connection: sqlite3.Connection, run_id: str) -> str:
        digest = original(connection, run_id)
        connection.execute(
            "UPDATE career_profile_state SET current_profile_version_id=NULL "
            "WHERE singleton=1"
        )
        connection.commit()
        return digest

    monkeypatch.setattr(profile_service, "_preflight_digest", mutate_parent)
    with pytest.raises(CareerProfileConflict, match="prepared profile state"):
        assemble_profile(db, 1, [CandidateProfileFactRequest(candidate_id)])
    assert db.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0] == 1
    assert (
        first.version.id
        == db.execute("SELECT id FROM career_profile_versions").fetchone()[0]
    )


@pytest.mark.parametrize("mutation", ["m5", "review"])
def test_authority_mutation_between_preparation_and_lock_rolls_back(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    original_save = profile_service.save_profile

    def mutate_then_save(*args, **kwargs):
        if mutation == "m5":
            db.execute(
                "UPDATE career_fact_candidates SET confidence=0.5 WHERE id=?",
                (candidate_id,),
            )
        else:
            db.execute(
                "INSERT INTO career_fact_review_events"
                "(candidate_id,event_kind,created_at) VALUES (?,?,?)",
                (
                    candidate_id,
                    "rejected",
                    "2099-01-01T00:00:00.000000+00:00",
                ),
            )
        db.commit()
        return original_save(*args, **kwargs)

    monkeypatch.setattr(profile_service, "save_profile", mutate_then_save)
    with pytest.raises(CareerProfileConflict):
        assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    assert db.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0] == 0
    assert get_profile_state(db).revision == 0


def test_one_prevalidation_per_run_and_none_inside_profile_lock(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills = _candidates(db, "skill")
    for candidate in skills:
        review_candidate(db, str(candidate["id"]), "approved")
    original = profile_service.validate_recognized_fact_authority
    calls = 0

    def checked(connection: sqlite3.Connection, run_id: str):
        nonlocal calls
        calls += 1
        assert not connection.in_transaction
        return original(connection, run_id)

    monkeypatch.setattr(profile_service, "validate_recognized_fact_authority", checked)
    assemble_profile(
        db,
        0,
        [CandidateProfileFactRequest(str(candidate["id"])) for candidate in skills],
    )
    assert calls == 1


def test_stale_revision_does_not_create_partial_snapshot(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    assemble_profile(db, 0, [])
    with pytest.raises(CareerProfileConflict):
        assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    assert db.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM career_profile_facts").fetchone()[0] == 0


def test_two_connection_service_cas_has_exactly_one_winner(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    database_path = Path(str(db.execute("PRAGMA database_list").fetchone()[2]))
    barrier = Barrier(2)

    def save() -> str:
        connection = connect(database_path)
        try:
            barrier.wait()
            assemble_profile(connection, 0, [CandidateProfileFactRequest(candidate_id)])
            return "saved"
        except CareerProfileConflict:
            return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _value: save(), range(2))) == [
            "conflict",
            "saved",
        ]
    assert db.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0] == 1


def test_empty_and_semantic_noop_saves_create_complete_versions(
    db: sqlite3.Connection,
) -> None:
    first = assemble_profile(db, 0, [])
    second = assemble_profile(db, 1, [])
    assert first.version.parent_version_id is None
    assert second.version.parent_version_id == first.version.id
    assert second.version.content_fingerprint == first.version.content_fingerprint
    assert [item.version.version_number for item in profile_history(db)] == [2, 1]


def test_candidate_semantic_noop_has_new_rows_and_same_fingerprint(
    db: sqlite3.Connection,
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    request = [CandidateProfileFactRequest(candidate_id)]
    first = assemble_profile(db, 0, request)
    second = assemble_profile(db, 1, request)
    assert second.version.id != first.version.id
    assert second.facts[0].id != first.facts[0].id
    assert second.version.parent_version_id == first.version.id
    assert second.version.content_fingerprint == first.version.content_fingerprint
    assert second.state.revision == 2


def test_final_validation_failure_rolls_back_every_insert(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    original = profile_service.validate_profile_aggregate
    calls = 0

    def fail_final(connection: sqlite3.Connection):
        nonlocal calls
        calls += 1
        state = original(connection)
        if calls == 2:
            raise CareerProfileValidationError("induced final validation failure")
        return state

    monkeypatch.setattr(profile_service, "validate_profile_aggregate", fail_final)
    with pytest.raises(CareerProfileValidationError, match="induced"):
        assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    assert db.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM career_profile_facts").fetchone()[0] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM career_profile_fact_sources").fetchone()[0]
        == 0
    )
    state = db.execute(
        "SELECT current_profile_version_id,revision,next_version_number "
        "FROM career_profile_state WHERE singleton=1"
    ).fetchone()
    assert tuple(state) == (None, 0, 1)


@pytest.mark.parametrize(
    ("stage", "trigger_sql"),
    [
        (
            "version",
            "CREATE TRIGGER fail_m6 BEFORE INSERT ON career_profile_versions "
            "BEGIN SELECT RAISE(ABORT,'version'); END",
        ),
        (
            "fact",
            "CREATE TRIGGER fail_m6 BEFORE INSERT ON career_profile_facts "
            "BEGIN SELECT RAISE(ABORT,'fact'); END",
        ),
        (
            "source",
            "CREATE TRIGGER fail_m6 BEFORE INSERT ON career_profile_fact_sources "
            "BEGIN SELECT RAISE(ABORT,'source'); END",
        ),
        (
            "state",
            "CREATE TRIGGER fail_m6 BEFORE UPDATE ON career_profile_state "
            "BEGIN SELECT RAISE(ABORT,'state'); END",
        ),
    ],
)
def test_each_profile_write_stage_rolls_back_atomically(
    db: sqlite3.Connection, stage: str, trigger_sql: str
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    db.execute(trigger_sql)
    db.commit()
    with pytest.raises(CareerProfileValidationError, match="integrity"):
        assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    assert stage in {"version", "fact", "source", "state"}
    assert db.execute("SELECT COUNT(*) FROM career_profile_versions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM career_profile_facts").fetchone()[0] == 0
    assert (
        db.execute("SELECT COUNT(*) FROM career_profile_fact_sources").fetchone()[0]
        == 0
    )
    assert tuple(
        db.execute(
            "SELECT current_profile_version_id,revision,next_version_number "
            "FROM career_profile_state WHERE singleton=1"
        ).fetchone()
    ) == (None, 0, 1)


@pytest.mark.parametrize("mutation", ["value", "order", "origin_sources"])
def test_malformed_immutable_profile_history_fails_without_repair(
    db: sqlite3.Connection, mutation: str
) -> None:
    candidate_id = str(_candidate(db)["id"])
    review_candidate(db, candidate_id, "approved")
    saved = assemble_profile(db, 0, [CandidateProfileFactRequest(candidate_id)])
    if mutation == "value":
        db.execute(
            "UPDATE career_profile_facts SET canonical_value_json=? WHERE id=?",
            ('{"name":"Tampered"}', saved.facts[0].id),
        )
    elif mutation == "order":
        db.execute(
            "UPDATE career_profile_facts SET sort_order=1 WHERE id=?",
            (saved.facts[0].id,),
        )
    else:
        db.execute(
            "UPDATE career_profile_facts SET origin='manual' WHERE id=?",
            (saved.facts[0].id,),
        )
    db.commit()
    before = tuple(
        db.execute(
            "SELECT * FROM career_profile_facts WHERE id=?", (saved.facts[0].id,)
        ).fetchone()
    )
    with pytest.raises(CareerProfileValidationError):
        current_profile(db)
    after = tuple(
        db.execute(
            "SELECT * FROM career_profile_facts WHERE id=?", (saved.facts[0].id,)
        ).fetchone()
    )
    assert after == before


@pytest.mark.parametrize(
    "value",
    [
        '{"name":null}',
        '{"name":" Python"}',
        '{"name":"Python\\n3"}',
        '{"name":"Python","extra":"x"}',
        '{"name":" Python 3"}',
    ],
)
def test_approved_edit_rejects_noncanonical_or_invalid_values(
    db: sqlite3.Connection, value: str
) -> None:
    candidate_id = str(_candidate(db)["id"])
    with pytest.raises((CareerProfileValidationError, ValueError)):
        review_candidate(db, candidate_id, "approved_edit", edited_value_json=value)


@pytest.mark.parametrize(
    ("fact_type", "value"),
    [
        ("identity.name", '{"name":"Ada Lovelace"}'),
        ("contact.email", '{"email":"ada@example.test"}'),
        ("contact.phone", '{"phone":"+30 210 123 4567"}'),
        ("portfolio_reference", '{"url":"https://example.test/work"}'),
        ("skill", '{"name":"Python"}'),
        ("employment", '{"company":"Analytical Engines","title":"Engineer"}'),
        ("education", '{"degree":"MSc","institution":"Synthetic University"}'),
        ("certification", '{"name":"Cloud Certificate"}'),
        ("language", '{"name":"English"}'),
    ],
)
def test_all_nine_m5_edit_shapes_validate(
    db: sqlite3.Connection, fact_type: str, value: str
) -> None:
    assert fact_type in FACT_TYPES
    canonical, digest, key = validate_candidate_value(fact_type, value)
    assert canonical == value
    assert len(digest) == 64
    assert key.startswith(fact_type + ":")
    candidate = _candidates(db, fact_type)[0]
    effective = review_candidate(
        db,
        str(candidate["id"]),
        "approved_edit",
        edited_value_json=value,
    )
    assert effective.canonical_value_json == value
    assert effective.value_sha256 == digest
    assert effective.normalized_key == key


def test_privacy_defaults_are_exact_and_version_local(
    db: sqlite3.Connection,
) -> None:
    assert profile_service._PRIVACY == {
        "identity.name": "sensitive",
        "contact.email": "sensitive",
        "contact.phone": "sensitive",
        "portfolio_reference": "private",
        "skill": "public",
        "employment": "private",
        "education": "private",
        "certification": "public",
        "language": "public",
    }
    assert len(profile_service._PRIVACY) == 9
    candidates = db.execute(
        "SELECT * FROM career_fact_candidates ORDER BY fact_type,id"
    ).fetchall()
    for candidate in candidates:
        review_candidate(db, str(candidate["id"]), "approved")
    requests = [
        CandidateProfileFactRequest(str(candidate["id"])) for candidate in candidates
    ]
    first = assemble_profile(db, 0, requests)
    assert first.version.origin == "approved_facts"
    assert all(fact.origin == "approved_candidate" for fact in first.facts)
    assert {
        fact.fact_type: fact.privacy_classification for fact in first.facts
    } == profile_service._PRIVACY
    override = (
        "private" if first.facts[0].privacy_classification != "private" else "public"
    )
    requests[0] = CandidateProfileFactRequest(
        requests[0].primary_candidate_id,
        privacy_override=override,
    )
    second = assemble_profile(db, 1, requests)
    assert second.facts[0].privacy_classification == override
    assert second.version.content_fingerprint != first.version.content_fingerprint
    assert profile_history(db)[1].facts[0].privacy_classification != override
    with pytest.raises(CareerProfileValidationError, match="privacy"):
        assemble_profile(
            db,
            2,
            [
                CandidateProfileFactRequest(
                    requests[0].primary_candidate_id,
                    privacy_override="secret",  # type: ignore[arg-type]
                )
            ],
        )


def test_existing_manual_and_imported_facts_carry_without_sources(
    db: sqlite3.Connection,
) -> None:
    first = save_profile(
        db,
        0,
        [
            ProfileFactInput(
                "manual:one",
                "note",
                "manual-one",
                '{"value":"Manual"}',
                "manual",
                "private",
                0,
            ),
            ProfileFactInput(
                "imported:two",
                "note",
                "imported-two",
                '{"value":"Imported"}',
                "imported",
                "sensitive",
                1,
            ),
        ],
        origin="manual",
    )
    carried = assemble_profile(
        db,
        1,
        [
            ManualProfileFactRequest("imported:two", privacy_override="private"),
            ManualProfileFactRequest("manual:one"),
        ],
    )
    assert carried.version.origin == "approved_facts"
    assert [fact.origin for fact in carried.facts] == ["imported", "manual"]
    assert [fact.logical_fact_id for fact in carried.facts] == [
        "imported:two",
        "manual:one",
    ]
    assert carried.facts[0].privacy_classification == "private"
    assert all(
        db.execute(
            "SELECT COUNT(*) FROM career_profile_fact_sources WHERE profile_fact_id=?",
            (fact.id,),
        ).fetchone()[0]
        == 0
        for fact in carried.facts
    )
    assert profile_history(db)[1].version.id == first.version.id


@pytest.mark.parametrize(
    "value",
    [
        "{}",
        '{"name":"Python","extra":"x"}',
        '{"name":null}',
        '{"name":3}',
        '{"name":""}',
        '{"name":"Python\\n3"}',
        '{ "name": "Python" }',
        '{"name":NaN}',
        '{"name":Infinity}',
        '{"end_date":"2020"}',
    ],
)
def test_m5_edit_shape_and_json_guards(value: str) -> None:
    with pytest.raises(ValueError):
        validate_candidate_value("skill", value)


@pytest.mark.parametrize(
    ("fact_type", "value"),
    [
        ("identity.name", '{"name":"123"}'),
        ("contact.email", '{"email":"not-an-email"}'),
        ("contact.phone", '{"phone":"123"}'),
        ("portfolio_reference", '{"url":"ftp://example.test/work"}'),
        (
            "employment",
            '{"company":"Example","start_date":"2024-13","title":"Engineer"}',
        ),
        (
            "employment",
            '{"company":"Example","end_date":"2024","title":"Engineer"}',
        ),
    ],
)
def test_m5_value_level_edit_rules_fail_closed(fact_type: str, value: str) -> None:
    with pytest.raises(ValueError):
        validate_candidate_value(fact_type, value)
