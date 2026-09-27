from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.resume_intelligence import (
    ResumeConflictError,
    ResumeNotFoundError,
    ResumeValidationError,
    append_fact_review,
    append_fact_review_in_transaction,
    append_sanitization_review,
    fact_candidates,
    fact_evidence,
    fact_review_history,
    fact_review_history_records_page,
    find_successful_fact_runs,
    latest_fact_review,
    latest_fact_review_record,
    latest_sanitization_review,
    list_fact_runs,
    load_fact_aggregate,
    load_fact_authority,
    persist_extraction,
    persist_fact_run,
    persist_sanitization,
    sanitization_findings,
    sanitization_review_history,
    sanitization_spans,
    validate_recognized_fact_authority,
)
from careeros.database.schema import initialize_schema
from careeros.resume_intelligence.career_facts import extract_deterministic_facts
from careeros.resume_intelligence.models import (
    CareerFactCandidate,
    CareerFactEvidence,
    CareerFactReviewEvent,
    CareerFactRun,
    ResumeExtractionRun,
    ResumePiiFinding,
    ResumeSanitizationRun,
    ResumeTextSegment,
    SanitizationReviewEvent,
    SanitizationSpan,
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def db(tmp_path: Path):
    c = connect(tmp_path / "repo.sqlite3")
    initialize_schema(c)
    c.execute("INSERT INTO material_store VALUES (1, ?, ?)", ("1" * 32, "now"))
    c.execute(
        "INSERT INTO materials VALUES (?, ?, 'resume', 1, 'active', NULL, ?, ?)",
        ("2" * 32, "Resume", "now", "now"),
    )
    c.execute(
        "INSERT INTO material_versions VALUES (?, ?, 1, ?, ?, 3, ?, ?, ?)",
        ("3" * 32, "2" * 32, "resume.txt", "text/plain", "4" * 64, "5" * 64, "now"),
    )
    c.commit()
    yield c
    c.close()


def extraction(
    run_id: str = "a" * 32, cache: str = "extract-cache"
) -> ResumeExtractionRun:
    return ResumeExtractionRun(
        run_id,
        "3" * 32,
        "resume.txt",
        "text/plain",
        3,
        "4" * 64,
        "text",
        "text",
        "succeeded",
        "abc",
        digest(b"abc"),
        1,
        "extractor",
        cache,
        "2026-01-01",
    )


def segment(run_id: str) -> ResumeTextSegment:
    return ResumeTextSegment(run_id, 0, 0, 3, "page:1", digest(b"abc"))


def test_extraction_success_cache_and_rollback(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    assert db.execute("SELECT count(*) FROM resume_text_segments").fetchone()[0] == 1
    with pytest.raises(ResumeConflictError):
        persist_extraction(db, extraction("b" * 32), [segment("b" * 32)])
    with pytest.raises(ResumeValidationError):
        persist_extraction(
            db, extraction("c" * 32, "other"), [segment("wrong".ljust(32, "c"))]
        )
    assert db.execute("SELECT count(*) FROM resume_extraction_runs").fetchone()[0] == 1


def test_sanitization_map_findings_reviews_and_rollback(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    clean = b"aXc"
    sanitization = ResumeSanitizationRun(
        "d" * 32,
        run.id,
        None,
        "aXc",
        digest(clean),
        "sanitizer",
        "policy",
        "{}",
        "san-cache",
        "succeeded",
        "2026-01-02",
    )
    spans = [
        SanitizationSpan(
            sanitization.id, 0, "copy", 0, 1, 0, 1, digest(b"a"), digest(b"a")
        ),
        SanitizationSpan(
            sanitization.id,
            1,
            "replace",
            1,
            2,
            1,
            2,
            digest(b"b"),
            digest(b"X"),
            "redacted",
        ),
        SanitizationSpan(
            sanitization.id, 2, "copy", 2, 3, 2, 3, digest(b"c"), digest(b"c")
        ),
    ]
    finding = ResumePiiFinding(
        sanitization.id, 0, "name", "detector", "rule", 1.0, "replace", 1, 2, 1
    )
    persist_sanitization(db, sanitization, spans, [finding])
    append_sanitization_review(
        db, SanitizationReviewEvent(sanitization.id, "approved", "2026-01-03")
    )
    append_sanitization_review(
        db, SanitizationReviewEvent(sanitization.id, "revoked", "2026-01-04")
    )
    assert [e.event_kind for e in sanitization_review_history(db, sanitization.id)] == [
        "revoked",
        "approved",
    ]
    bad = ResumeSanitizationRun(
        "e" * 32,
        run.id,
        None,
        "aXc",
        digest(clean),
        "s",
        "p",
        "{}",
        "bad-cache",
        "succeeded",
        "2026-01-05",
    )
    with pytest.raises(ResumeValidationError):
        persist_sanitization(db, bad, [spans[0]], [])
    assert (
        db.execute("SELECT count(*) FROM resume_sanitization_runs").fetchone()[0] == 1
    )


def test_fact_input_space_hash_and_append_only_review(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    fact_run = CareerFactRun(
        "f" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        "fact-cache",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    candidate = CareerFactCandidate(
        "1" * 32,
        fact_run.id,
        "skill",
        "python",
        '"Python"',
        digest(b'"Python"'),
        1.0,
        "deterministic",
    )
    evidence = CareerFactEvidence(candidate.id, 0, "extracted", 0, 3, digest(b"abc"))
    persist_fact_run(db, fact_run, [candidate], [evidence])
    append_fact_review(
        db, CareerFactReviewEvent(candidate.id, "approved", "2026-01-06")
    )
    append_fact_review(
        db,
        CareerFactReviewEvent(
            candidate.id,
            "approved_edit",
            "2026-01-07",
            '"Python 3"',
            digest(b'"Python 3"'),
        ),
    )
    assert [e.event_kind for e in fact_review_history(db, candidate.id)] == [
        "approved_edit",
        "approved",
    ]
    with pytest.raises(ResumeValidationError):
        persist_fact_run(
            db,
            CareerFactRun(
                "2" * 32,
                run.id,
                None,
                "extracted",
                "deterministic",
                "succeeded",
                "bad",
                "0" * 64,
                3,
                None,
                "extractor",
                "schema",
                None,
                None,
                None,
                "now",
            ),
            [candidate],
            [],
        )


def test_load_fact_aggregate_is_complete_bounded_and_ordered(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    fact = CareerFactRun(
        "f" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        "aggregate-cache",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    first = CareerFactCandidate(
        "1" * 32,
        fact.id,
        "skill",
        "a",
        '"A"',
        digest(b'"A"'),
        1.0,
        "deterministic",
    )
    second = CareerFactCandidate(
        "2" * 32,
        fact.id,
        "skill",
        "b",
        '"B"',
        digest(b'"B"'),
        1.0,
        "deterministic",
    )
    persist_fact_run(
        db,
        fact,
        [second, first],
        [
            CareerFactEvidence(second.id, 0, "extracted", 0, 1, digest(b"a")),
            CareerFactEvidence(first.id, 0, "extracted", 1, 3, digest(b"bc")),
        ],
    )
    loaded_run, candidates, evidence = load_fact_aggregate(db, fact.id)
    assert loaded_run == fact
    assert [candidate.id for candidate in candidates] == [first.id, second.id]
    assert [(item.candidate_id, item.ordinal) for item in evidence] == [
        (first.id, 0),
        (second.id, 0),
    ]
    with pytest.raises(ResumeValidationError, match="candidate limit"):
        load_fact_aggregate(db, fact.id, candidate_limit=1)
    with pytest.raises(ResumeValidationError, match="evidence limit"):
        load_fact_aggregate(db, fact.id, evidence_total_limit=1)


def test_successful_fact_cache_rejection_is_transactional(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    fact = CareerFactRun(
        "f" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        "serialized-cache",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    persist_fact_run(db, fact, reject_existing_successful_cache=True)
    duplicate = CareerFactRun(
        "e" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        fact.cache_fingerprint,
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "later",
    )
    with pytest.raises(ResumeConflictError):
        persist_fact_run(db, duplicate, reject_existing_successful_cache=True)
    assert db.execute("SELECT count(*) FROM career_fact_runs").fetchone()[0] == 1


def test_latest_tie_order_and_overlapping_findings(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    clean = b"aXc"
    sanitization = ResumeSanitizationRun(
        "d" * 32,
        run.id,
        None,
        "aXc",
        digest(clean),
        "s",
        "p",
        "{}",
        "san",
        "succeeded",
        "now",
    )
    spans = [
        SanitizationSpan(
            sanitization.id, 0, "copy", 0, 1, 0, 1, digest(b"a"), digest(b"a")
        ),
        SanitizationSpan(
            sanitization.id, 1, "replace", 1, 2, 1, 2, digest(b"b"), digest(b"X"), "x"
        ),
        SanitizationSpan(
            sanitization.id, 2, "copy", 2, 3, 2, 3, digest(b"c"), digest(b"c")
        ),
    ]
    findings = [
        ResumePiiFinding(
            sanitization.id, 0, "name", "rule", "detector", 1.0, "replace", 1, 2, 1
        ),
        ResumePiiFinding(
            sanitization.id, 1, "phone", "rule", "detector", 1.0, "replace", 1, 2, 1
        ),
    ]
    persist_sanitization(db, sanitization, spans, findings)
    append_sanitization_review(
        db, SanitizationReviewEvent(sanitization.id, "approved", "same")
    )
    append_sanitization_review(
        db, SanitizationReviewEvent(sanitization.id, "revoked", "same")
    )
    assert latest_sanitization_review(db, sanitization.id).event_kind == "revoked"
    assert len(sanitization_findings(db, sanitization.id)) == 2


def test_failed_children_late_insert_and_not_found_reads(db) -> None:
    failed = ResumeExtractionRun(
        "9" * 32,
        "3" * 32,
        "resume.txt",
        "text/plain",
        3,
        "4" * 64,
        "text",
        "text",
        "failed",
        None,
        None,
        0,
        "extractor",
        "failed",
        "now",
    )
    with pytest.raises(ResumeValidationError):
        persist_extraction(db, failed, [segment(failed.id)])
    run = extraction()
    with pytest.raises(ResumeValidationError):
        persist_extraction(
            db, run, [ResumeTextSegment(run.id, 0, 0, 3, "x" * 2000, digest(b"abc"))]
        )
    assert db.execute("SELECT count(*) FROM resume_extraction_runs").fetchone()[0] == 0
    persist_extraction(db, run, [segment(run.id)])
    with pytest.raises(ResumeNotFoundError):
        sanitization_findings(db, "0" * 32)
    with pytest.raises(ResumeNotFoundError):
        sanitization_review_history(db, "0" * 32)
    with pytest.raises(ResumeNotFoundError):
        fact_review_history(db, "0" * 32)


def test_sanitized_fact_and_exact_evidence_validation(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    clean = b"aXc"
    sanitization = ResumeSanitizationRun(
        "d" * 32,
        run.id,
        None,
        "aXc",
        digest(clean),
        "s",
        "p",
        "{}",
        "san",
        "succeeded",
        "now",
    )
    spans = [
        SanitizationSpan(
            sanitization.id, 0, "copy", 0, 1, 0, 1, digest(b"a"), digest(b"a")
        ),
        SanitizationSpan(
            sanitization.id, 1, "replace", 1, 2, 1, 2, digest(b"b"), digest(b"X"), "x"
        ),
        SanitizationSpan(
            sanitization.id, 2, "copy", 2, 3, 2, 3, digest(b"c"), digest(b"c")
        ),
    ]
    persist_sanitization(db, sanitization, spans)
    fact = CareerFactRun(
        "f" * 32,
        run.id,
        sanitization.id,
        "sanitized",
        "model",
        "succeeded",
        "fc",
        digest(clean),
        3,
        None,
        None,
        "schema",
        "provider",
        "model",
        "prompt",
        "now",
    )
    candidate = CareerFactCandidate(
        "1" * 32, fact.id, "skill", "x", '"X"', digest(b'"X"'), None, "model"
    )
    persist_fact_run(
        db,
        fact,
        [candidate],
        [CareerFactEvidence(candidate.id, 0, "sanitized", 0, 3, digest(clean))],
    )
    assert [span.ordinal for span in sanitization_spans(db, sanitization.id)] == [
        0,
        1,
        2,
    ]
    assert [item.ordinal for item in fact_evidence(db, candidate.id)] == [0]
    assert [item.id for item in list_fact_runs(db, run.id)] == [fact.id]
    assert [item.id for item in find_successful_fact_runs(db, "fc")] == [fact.id]
    wrong = CareerFactRun(
        "2" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        "wrong",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    wrong_candidate = CareerFactCandidate(
        "2" * 32, wrong.id, "skill", "x", '"X"', digest(b'"X"'), None, "deterministic"
    )
    with pytest.raises(ResumeValidationError):
        persist_fact_run(
            db,
            wrong,
            [wrong_candidate],
            [
                CareerFactEvidence(
                    wrong_candidate.id, 0, "sanitized", 0, 1, digest(b"a")
                )
            ],
        )
    bad = CareerFactRun(
        "3" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        "bad",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    bad_candidate = CareerFactCandidate(
        "3" * 32, bad.id, "skill", "x", '"X"', digest(b'"X"'), None, "deterministic"
    )
    with pytest.raises(ResumeValidationError):
        persist_fact_run(
            db,
            bad,
            [bad_candidate],
            [CareerFactEvidence(bad_candidate.id, 0, "extracted", 1, 2, "0" * 64)],
        )
    assert db.execute("SELECT count(*) FROM career_fact_runs").fetchone()[0] == 1


def test_same_extraction_and_failed_child_contracts(db) -> None:
    first = extraction()
    persist_extraction(db, first, [segment(first.id)])
    second = extraction("e" * 32, "extract-second")
    persist_extraction(db, second, [segment(second.id)])
    clean = b"aXc"
    sanitization = ResumeSanitizationRun(
        "d" * 32,
        first.id,
        None,
        "aXc",
        digest(clean),
        "s",
        "p",
        "{}",
        "san",
        "succeeded",
        "now",
    )
    spans = [
        SanitizationSpan(
            sanitization.id, 0, "copy", 0, 1, 0, 1, digest(b"a"), digest(b"a")
        ),
        SanitizationSpan(
            sanitization.id, 1, "replace", 1, 2, 1, 2, digest(b"b"), digest(b"X")
        ),
        SanitizationSpan(
            sanitization.id, 2, "copy", 2, 3, 2, 3, digest(b"c"), digest(b"c")
        ),
    ]
    persist_sanitization(db, sanitization, spans)
    mismatched = CareerFactRun(
        "7" * 32,
        second.id,
        sanitization.id,
        "sanitized",
        "model",
        "succeeded",
        "mismatch",
        digest(clean),
        3,
        None,
        None,
        "schema",
        "provider",
        "model",
        "prompt",
        "now",
    )
    with pytest.raises(ResumeValidationError, match="match the extraction"):
        persist_fact_run(db, mismatched)

    failed_sanitization = ResumeSanitizationRun(
        "8" * 32,
        first.id,
        None,
        None,
        None,
        "s",
        "p",
        "{}",
        "failed-san",
        "failed",
        "now",
    )
    with pytest.raises(ResumeValidationError, match="cannot contain artifacts"):
        persist_sanitization(db, failed_sanitization, spans)
    persist_sanitization(db, failed_sanitization)
    invalid_child = ResumeSanitizationRun(
        "4" * 32,
        first.id,
        failed_sanitization.id,
        "abc",
        digest(b"abc"),
        "s",
        "p",
        "{}",
        "invalid-lineage",
        "succeeded",
        "now",
    )
    with pytest.raises(ResumeValidationError, match="parent sanitization"):
        persist_sanitization(
            db,
            invalid_child,
            [
                SanitizationSpan(
                    invalid_child.id,
                    0,
                    "copy",
                    0,
                    3,
                    0,
                    3,
                    digest(b"abc"),
                    digest(b"abc"),
                )
            ],
        )

    failed_fact = CareerFactRun(
        "9" * 32,
        first.id,
        None,
        "extracted",
        "deterministic",
        "failed",
        "failed-fact",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    failed_candidate = CareerFactCandidate(
        "6" * 32,
        failed_fact.id,
        "skill",
        "x",
        '"X"',
        digest(b'"X"'),
        None,
        "deterministic",
    )
    with pytest.raises(ResumeValidationError, match="cannot contain artifacts"):
        persist_fact_run(db, failed_fact, [failed_candidate])
    assert db.execute("SELECT count(*) FROM career_fact_runs").fetchone()[0] == 0


def _persist_valid_m5(db):
    raw = "Skills: Python\n"
    run = ResumeExtractionRun(
        "9" * 32,
        "3" * 32,
        "resume.txt",
        "text/plain",
        3,
        "4" * 64,
        "text",
        "text",
        "succeeded",
        raw,
        digest(raw.encode()),
        0,
        "extractor",
        "valid-cache",
        "2026-01-01",
    )
    persist_extraction(db, run)
    fact, candidates, evidence = extract_deterministic_facts(
        raw, run.id, fact_run_id="8" * 32, created_at="2026-01-02"
    )
    persist_fact_run(db, fact, candidates, evidence)
    return fact, candidates, evidence


def test_m6_authority_digest_is_stable_and_prevalidation_is_strict(db) -> None:
    fact, _candidates, _evidence = _persist_valid_m5(db)
    first = load_fact_authority(db, fact.id)
    second = load_fact_authority(db, fact.id)
    assert first.digest == second.digest
    assert validate_recognized_fact_authority(db, fact.id).digest == first.digest


@pytest.mark.parametrize("mutation", ["candidate", "evidence", "run", "raw"])
def test_m6_authority_detects_persisted_mutation(db, mutation: str) -> None:
    fact, candidates, evidence = _persist_valid_m5(db)
    before = load_fact_authority(db, fact.id).digest
    if mutation == "candidate":
        db.execute(
            "UPDATE career_fact_candidates SET confidence=0.5 WHERE id=?",
            (candidates[0].id,),
        )
    elif mutation == "evidence":
        db.execute(
            "UPDATE career_fact_evidence SET slice_sha256=? "
            "WHERE candidate_id=? AND ordinal=?",
            ("0" * 64, evidence[0].candidate_id, evidence[0].ordinal),
        )
    elif mutation == "run":
        db.execute(
            "UPDATE career_fact_runs SET created_at='changed' WHERE id=?", (fact.id,)
        )
    else:
        db.execute(
            "UPDATE resume_extraction_runs SET raw_text='changed' WHERE id=?",
            (fact.extraction_run_id,),
        )
    db.commit()
    if mutation == "run":
        assert load_fact_authority(db, fact.id).digest != before
    else:
        with pytest.raises(ResumeValidationError):
            validate_recognized_fact_authority(db, fact.id)


def test_load_authority_does_not_rerun_extractor_or_pii(db, monkeypatch) -> None:
    fact, _candidates, _evidence = _persist_valid_m5(db)
    import careeros.database.resume_intelligence as repository

    monkeypatch.setattr(
        repository,
        "extract_deterministic_facts",
        lambda *_a, **_k: pytest.fail("extractor called"),
    )
    assert load_fact_authority(db, fact.id).digest


def test_m6_review_history_validates_all_timestamps_and_equal_time_ids(db) -> None:
    fact, candidates, _evidence = _persist_valid_m5(db)
    candidate_id = candidates[0].id
    stamp = "2026-09-26T15:42:13.123456+00:00"
    db.executemany(
        "INSERT INTO career_fact_review_events"
        "(candidate_id,event_kind,created_at) VALUES (?,?,?)",
        [(candidate_id, "approved", stamp), (candidate_id, "rejected", stamp)],
    )
    db.commit()
    assert latest_fact_review_record(db, candidate_id).event_kind == "rejected"
    db.execute(
        "UPDATE career_fact_review_events SET created_at='2026-01-01' "
        "WHERE candidate_id=?",
        (candidate_id,),
    )
    db.commit()
    with pytest.raises(ResumeValidationError):
        latest_fact_review_record(db, candidate_id)


def test_m6_review_history_records_page_uses_true_offset_and_count(db) -> None:
    fact, candidates, _evidence = _persist_valid_m5(db)
    candidate_id = candidates[0].id
    stamp = "2026-09-26T15:42:13.123456+00:00"
    db.executemany(
        "INSERT INTO career_fact_review_events"
        "(candidate_id,event_kind,created_at) VALUES (?,?,?)",
        [(candidate_id, "approved", stamp)] * 4,
    )
    db.commit()

    rows, total = fact_review_history_records_page(db, candidate_id, offset=1, limit=2)
    assert total == 4
    assert [row.id for row in rows] == [3, 2]
    rows, total = fact_review_history_records_page(db, candidate_id, offset=4, limit=2)
    assert rows == [] and total == 4
    del fact


def test_transaction_aware_review_append_and_generic_compatibility(db) -> None:
    fact, candidates, _evidence = _persist_valid_m5(db)
    event = CareerFactReviewEvent(
        candidates[0].id, "approved", "2026-09-26T15:42:13.123456+00:00"
    )
    db.execute("BEGIN IMMEDIATE")
    event_id = append_fact_review_in_transaction(db, event, validate_timestamp=True)
    db.commit()
    assert event_id > 0
    assert latest_fact_review(db, candidates[0].id).event_kind == "approved"
    append_fact_review(
        db, CareerFactReviewEvent(candidates[0].id, "rejected", "legacy")
    )
    assert latest_fact_review(db, candidates[0].id).event_kind == "rejected"


def test_invalid_edit_is_atomic_and_latest_fact_review(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    fact = CareerFactRun(
        "f" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        "fc",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    candidate = CareerFactCandidate(
        "1" * 32, fact.id, "skill", "x", '"X"', digest(b'"X"'), None, "deterministic"
    )
    persist_fact_run(
        db,
        fact,
        [candidate],
        [CareerFactEvidence(candidate.id, 0, "extracted", 0, 3, digest(b"abc"))],
    )
    with pytest.raises(ResumeValidationError):
        append_fact_review(
            db,
            CareerFactReviewEvent(
                candidate.id, "approved_edit", "same", '"bad"', "0" * 64
            ),
        )
    assert fact_candidates(db, fact.id)[0].canonical_value_json == '"X"'
    assert fact_review_history(db, candidate.id) == []
    append_fact_review(db, CareerFactReviewEvent(candidate.id, "approved", "same"))
    append_fact_review(db, CareerFactReviewEvent(candidate.id, "rejected", "same"))
    assert latest_fact_review(db, candidate.id).event_kind == "rejected"


def test_non_standard_json_constants_are_rejected(db) -> None:
    run = extraction()
    persist_extraction(db, run, [segment(run.id)])
    fact = CareerFactRun(
        "f" * 32,
        run.id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        "fc",
        digest(b"abc"),
        3,
        None,
        "extractor",
        "schema",
        None,
        None,
        None,
        "now",
    )
    candidate = CareerFactCandidate(
        "1" * 32,
        fact.id,
        "skill",
        "x",
        "NaN",
        digest(b"NaN"),
        None,
        "deterministic",
    )
    with pytest.raises(ResumeValidationError, match="valid JSON"):
        persist_fact_run(db, fact, [candidate])
    assert db.execute("SELECT count(*) FROM career_fact_runs").fetchone()[0] == 0
