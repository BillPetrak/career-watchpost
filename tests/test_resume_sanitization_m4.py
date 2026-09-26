from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from careeros.database import resume_intelligence as repository
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.resume_intelligence.coordinates import (
    ByteRange,
    TransformationMap,
    TransformationSpan,
)
from careeros.resume_intelligence.models import (
    ResumeExtractionRun,
    ResumeSanitizationRun,
    ResumeTextSegment,
)
from careeros.resume_intelligence.pii import detect_pii, resolve_sanitization
from careeros.resume_intelligence.sanitization import (
    DEFAULT_POLICY_FINGERPRINT,
    SANITIZER_FINGERPRINT,
    approve_external_use,
    create_override,
    is_approved_for_external_use,
    revoke_external_use,
    sanitize_extraction,
)
from careeros.resume_intelligence.sanitization_identity import (
    override_manifest,
    override_policy_fingerprint,
    sanitization_cache_fingerprint,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def db(tmp_path: Path):
    connection = connect(tmp_path / "m4.sqlite3")
    initialize_schema(connection)
    connection.execute("INSERT INTO material_store VALUES (1, ?, ?)", ("1" * 32, "now"))
    connection.execute(
        "INSERT INTO materials VALUES (?, ?, 'resume', 1, 'active', NULL, ?, ?)",
        ("2" * 32, "Fictional Resume", "now", "now"),
    )
    connection.execute(
        "INSERT INTO material_versions VALUES (?, ?, 1, ?, ?, 3, ?, ?, ?)",
        ("3" * 32, "2" * 32, "resume.txt", "text/plain", "4" * 64, "5" * 64, "now"),
    )
    raw = "Name: Zoë Fiction\nEmail: zoe@example.test\n"
    run = ResumeExtractionRun(
        "a" * 32,
        "3" * 32,
        "resume.txt",
        "text/plain",
        3,
        "4" * 64,
        "text",
        "text",
        "succeeded",
        raw,
        _sha(raw.encode()),
        1,
        "m3",
        "extract",
        "now",
    )
    connection.commit()
    repository.persist_extraction(
        connection,
        run,
        [
            ResumeTextSegment(
                run.id, 0, 0, len(raw.encode()), "text", _sha(raw.encode())
            )
        ],
    )
    yield connection, run, raw
    connection.close()


def _mapping(connection, run_id: str) -> TransformationMap:
    run = repository.get_sanitization(connection, run_id)
    extraction = repository.get_extraction(connection, run.extraction_run_id)
    assert extraction.raw_text is not None and run.sanitized_text is not None
    spans = repository.sanitization_spans(connection, run_id)
    return TransformationMap.from_spans(
        (
            TransformationSpan(
                span.ordinal,
                span.kind,
                span.raw_start_byte,
                span.raw_end_byte,
                span.sanitized_start_byte,
                span.sanitized_end_byte,
                span.raw_slice_sha256,
                span.sanitized_slice_sha256,
                span.placeholder_key,
            )
            for span in spans
        ),
        raw_size=len(extraction.raw_text.encode()),
        sanitized_size=len(run.sanitized_text.encode()),
        raw_artifact=extraction.raw_text.encode(),
        sanitized_artifact=run.sanitized_text.encode(),
    )


def _persist_text_extraction(
    connection, raw: str, *, marker: str
) -> ResumeExtractionRun:
    run = ResumeExtractionRun(
        marker * 32,
        "3" * 32,
        "resume.txt",
        "text/plain",
        3,
        "4" * 64,
        "text",
        "text",
        "succeeded",
        raw,
        _sha(raw.encode()),
        1,
        "m3",
        f"extract-{marker}",
        "now",
    )
    repository.persist_extraction(
        connection,
        run,
        [
            ResumeTextSegment(
                run.id, 0, 0, len(raw.encode()), "text", _sha(raw.encode())
            )
        ],
    )
    return run


def test_supported_service_derives_exact_identity_and_reuses_cache(db) -> None:
    connection, extraction, raw = db
    first = sanitize_extraction(
        connection, extraction.id, created_at="2026-01-02", run_id="d" * 32
    )
    second = sanitize_extraction(
        connection, extraction.id, created_at="2026-01-03", run_id="e" * 32
    )
    assert first.id == second.id
    assert first.sanitizer_fingerprint == SANITIZER_FINGERPRINT
    assert first.policy_fingerprint == DEFAULT_POLICY_FINGERPRINT
    assert len(first.cache_fingerprint) == 64
    assert first.sanitized_text != raw
    assert (
        connection.execute("SELECT count(*) FROM resume_sanitization_runs").fetchone()[
            0
        ]
        == 1
    )


def test_conflict_winner_must_match_the_complete_derived_identity(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, extraction, _ = db
    real_persist = repository.persist_sanitization

    def conflicting_persist(connection, run, spans, findings):
        wrong = replace(run, sanitizer_fingerprint="wrong")
        real_persist(connection, wrong, spans, findings)
        raise repository.ResumeConflictError("injected race")

    monkeypatch.setattr(repository, "persist_sanitization", conflicting_persist)
    with pytest.raises(repository.ResumeConflictError, match="cache identity"):
        sanitize_extraction(
            connection, extraction.id, created_at="now", run_id="d" * 32
        )


def test_override_derives_manifest_policy_cache_and_never_inherits_approval(db) -> None:
    connection, extraction, _ = db
    parent = sanitize_extraction(
        connection, extraction.id, created_at="parent", run_id="d" * 32
    )
    approve_external_use(connection, parent.id, created_at="2026-01-02")
    child = create_override(
        connection,
        parent.id,
        {0: "keep", 1: "delete"},
        created_at="child",
        run_id="e" * 32,
    )
    assert json.loads(child.override_manifest_json) == {
        "actions": [
            {"action": "keep", "finding_ordinal": 0},
            {"action": "delete", "finding_ordinal": 1},
        ],
        "version": 1,
    }
    assert child.policy_fingerprint != parent.policy_fingerprint
    assert child.cache_fingerprint != parent.cache_fingerprint
    assert child.parent_sanitization_run_id == parent.id
    assert not is_approved_for_external_use(connection, child.id)
    retry = create_override(
        connection,
        parent.id,
        {1: "delete", 0: "keep"},
        created_at="retry",
        run_id="f" * 32,
    )
    assert retry.id == child.id


def test_approval_revocation_and_idempotence_share_authority_order(db) -> None:
    connection, extraction, _ = db
    run = sanitize_extraction(
        connection, extraction.id, created_at="run", run_id="d" * 32
    )
    approved_id = approve_external_use(
        connection, run.id, created_at="2026-01-02T00:00:00+00:00"
    )
    assert is_approved_for_external_use(connection, run.id)
    # Inserted later but historically earlier: frozen created_at/id authority
    # still treats the approval as latest.
    revoke_external_use(connection, run.id, created_at="2026-01-01T00:00:00+00:00")
    assert is_approved_for_external_use(connection, run.id)
    assert (
        approve_external_use(connection, run.id, created_at="2026-01-02T00:00:00+00:00")
        == approved_id
    )
    revoke_external_use(connection, run.id, created_at="2026-01-03T00:00:00+00:00")
    assert not is_approved_for_external_use(connection, run.id)
    noted = revoke_external_use(
        connection,
        run.id,
        created_at="2026-01-04T00:00:00+00:00",
        note="fictional audit note",
    )
    assert noted != approved_id
    assert len(repository.sanitization_review_history(connection, run.id)) == 4


def test_unresolved_override_cannot_be_approved(db) -> None:
    connection, extraction, _ = db
    parent = sanitize_extraction(
        connection, extraction.id, created_at="parent", run_id="d" * 32
    )
    child = create_override(
        connection,
        parent.id,
        {0: "review"},
        created_at="child",
        run_id="e" * 32,
    )
    with pytest.raises(repository.ResumeValidationError, match="unresolved"):
        approve_external_use(connection, child.id, created_at="approval")
    assert not is_approved_for_external_use(connection, child.id)


def test_unicode_pii_is_removed_before_exact_artifact_can_be_approved(db) -> None:
    connection, _, _ = db
    raw = "Name: Siobhán O’Connor\nName: 王伟\nEmail: δοκιμή@παράδειγμα.δοκιμή\n"
    extraction = _persist_text_extraction(connection, raw, marker="b")
    run = sanitize_extraction(
        connection, extraction.id, created_at="run", run_id="c" * 32
    )
    assert run.sanitized_text is not None
    assert "Siobhán" not in run.sanitized_text
    assert "王伟" not in run.sanitized_text
    assert "δοκιμή" not in run.sanitized_text
    approve_external_use(connection, run.id, created_at="approved")
    assert is_approved_for_external_use(connection, run.id)


def test_ambiguous_label_requires_typed_resolution_before_approval(db) -> None:
    connection, _, _ = db
    raw = "Email: δοκιμή@localhost\n"
    extraction = _persist_text_extraction(connection, raw, marker="b")
    root = sanitize_extraction(
        connection, extraction.id, created_at="root", run_id="c" * 32
    )
    finding = repository.sanitization_findings(connection, root.id)[0]
    assert finding.disposition == "review"
    assert not is_approved_for_external_use(connection, root.id)
    with pytest.raises(repository.ResumeValidationError, match="unresolved"):
        approve_external_use(connection, root.id, created_at="rejected")

    child = create_override(
        connection,
        root.id,
        {finding.ordinal: "replace"},
        created_at="child",
        run_id="d" * 32,
    )
    approve_external_use(connection, child.id, created_at="approved")
    assert is_approved_for_external_use(connection, child.id)


@pytest.mark.parametrize(
    "tamper",
    ["raw", "sanitized_hash", "span_hash", "map_gap", "finding", "manifest"],
)
def test_integrity_tampering_fails_closed(db, tamper: str) -> None:
    connection, extraction, _ = db
    run = sanitize_extraction(
        connection, extraction.id, created_at="run", run_id="d" * 32
    )
    approve_external_use(connection, run.id, created_at="approved")
    if tamper == "raw":
        connection.execute(
            "UPDATE resume_extraction_runs SET raw_text=raw_text || 'x' WHERE id=?",
            (extraction.id,),
        )
    elif tamper == "sanitized_hash":
        connection.execute(
            "UPDATE resume_sanitization_runs SET sanitized_text_sha256=? WHERE id=?",
            ("0" * 64, run.id),
        )
    elif tamper == "span_hash":
        connection.execute(
            "UPDATE resume_sanitization_spans SET raw_slice_sha256=? "
            "WHERE sanitization_run_id=? AND ordinal=1",
            ("0" * 64, run.id),
        )
    elif tamper == "map_gap":
        connection.execute(
            "UPDATE resume_sanitization_spans "
            "SET sanitized_start_byte=sanitized_start_byte+1 "
            "WHERE sanitization_run_id=? AND ordinal=1",
            (run.id,),
        )
    elif tamper == "finding":
        connection.execute(
            "UPDATE resume_pii_findings SET disposition='keep' "
            "WHERE sanitization_run_id=? AND ordinal=0",
            (run.id,),
        )
    else:
        connection.execute(
            "UPDATE resume_sanitization_runs SET override_manifest_json=? WHERE id=?",
            ('{"actions":[],"version":1}', run.id),
        )
    connection.commit()
    assert not is_approved_for_external_use(connection, run.id)
    with pytest.raises(repository.ResumeValidationError):
        approve_external_use(connection, run.id, created_at="retry")
    with pytest.raises(repository.ResumeValidationError):
        repository.validate_sanitization(connection, run.id)


@pytest.mark.parametrize(
    "tamper",
    [
        "category",
        "rule_identity",
        "deleted_finding",
        "extra_finding",
        "range",
        "ordinal",
        "span_reference",
    ],
)
def test_complete_finding_set_is_integrity_bound_to_approval(db, tamper: str) -> None:
    connection, extraction, _ = db
    run = sanitize_extraction(
        connection, extraction.id, created_at="run", run_id="d" * 32
    )
    approve_external_use(connection, run.id, created_at="approved")
    assert repository.validate_sanitization_authority(connection, run.id) == run

    if tamper == "category":
        connection.execute(
            "UPDATE resume_pii_findings SET category='phone' "
            "WHERE sanitization_run_id=? AND ordinal=0",
            (run.id,),
        )
    elif tamper == "rule_identity":
        connection.execute(
            "UPDATE resume_pii_findings SET rule_identity='spoofed-rule' "
            "WHERE sanitization_run_id=? AND ordinal=0",
            (run.id,),
        )
    elif tamper == "deleted_finding":
        connection.execute(
            "DELETE FROM resume_pii_findings WHERE sanitization_run_id=? AND ordinal=1",
            (run.id,),
        )
    elif tamper == "extra_finding":
        connection.execute(
            """
            INSERT INTO resume_pii_findings(
                sanitization_run_id,ordinal,category,rule_identity,confidence,
                disposition,origin,raw_start_byte,raw_end_byte,
                sanitization_span_ordinal
            )
            SELECT sanitization_run_id,2,category,rule_identity,confidence,
                   disposition,origin,raw_start_byte,raw_end_byte,
                   sanitization_span_ordinal
            FROM resume_pii_findings
            WHERE sanitization_run_id=? AND ordinal=1
            """,
            (run.id,),
        )
    elif tamper == "range":
        connection.execute(
            "UPDATE resume_pii_findings SET raw_end_byte=raw_end_byte-1 "
            "WHERE sanitization_run_id=? AND ordinal=0",
            (run.id,),
        )
    elif tamper == "ordinal":
        connection.execute(
            "UPDATE resume_pii_findings SET ordinal=2 "
            "WHERE sanitization_run_id=? AND ordinal=1",
            (run.id,),
        )
    else:
        connection.execute(
            "UPDATE resume_pii_findings SET sanitization_span_ordinal=3 "
            "WHERE sanitization_run_id=? AND ordinal=0",
            (run.id,),
        )
    connection.commit()

    assert not is_approved_for_external_use(connection, run.id)
    with pytest.raises(repository.ResumeValidationError):
        repository.validate_sanitization_authority(connection, run.id)
    with pytest.raises(repository.ResumeValidationError):
        approve_external_use(connection, run.id, created_at="retry")


def test_unknown_detector_fingerprint_fails_closed_for_approval(db) -> None:
    connection, extraction, _ = db
    run = sanitize_extraction(
        connection, extraction.id, created_at="run", run_id="d" * 32
    )
    approve_external_use(connection, run.id, created_at="approved")
    connection.execute(
        "UPDATE resume_sanitization_runs SET sanitizer_fingerprint=? WHERE id=?",
        ("unsupported-detector-version", run.id),
    )
    connection.commit()
    assert not is_approved_for_external_use(connection, run.id)
    with pytest.raises(repository.ResumeValidationError, match="unsupported"):
        approve_external_use(connection, run.id, created_at="retry")


def test_override_child_is_reconstructed_and_child_tamper_fails_closed(db) -> None:
    connection, extraction, _ = db
    parent = sanitize_extraction(
        connection, extraction.id, created_at="parent", run_id="d" * 32
    )
    child = create_override(
        connection,
        parent.id,
        {0: "keep", 1: "delete"},
        created_at="child",
        run_id="e" * 32,
    )
    assert repository.validate_sanitization_authority(connection, child.id) == child
    approve_external_use(connection, child.id, created_at="approved")
    connection.execute(
        "UPDATE resume_pii_findings SET rule_identity='spoofed-child-rule' "
        "WHERE sanitization_run_id=? AND ordinal=1",
        (child.id,),
    )
    connection.commit()
    assert not is_approved_for_external_use(connection, child.id)


def test_parent_corruption_and_multi_run_cycle_fail_child_approval(db) -> None:
    connection, extraction, _ = db
    parent = sanitize_extraction(
        connection, extraction.id, created_at="parent", run_id="d" * 32
    )
    child = create_override(
        connection,
        parent.id,
        {0: "keep"},
        created_at="child",
        run_id="e" * 32,
    )
    approve_external_use(connection, child.id, created_at="approved")
    connection.execute(
        "UPDATE resume_sanitization_runs SET sanitized_text='tampered' WHERE id=?",
        (parent.id,),
    )
    connection.commit()
    assert not is_approved_for_external_use(connection, child.id)

    connection.execute(
        "UPDATE resume_sanitization_runs SET sanitized_text='[restored]' WHERE id=?",
        (parent.id,),
    )
    # Restore the original parent text/hash from a deterministic fresh computation.
    original = resolve_sanitization(
        repository.get_extraction(connection, extraction.id).raw_text or "",
        detect_pii(repository.get_extraction(connection, extraction.id).raw_text or ""),
        sanitization_run_id=parent.id,
        dispositions={0: "replace", 1: "replace"},
    )
    connection.execute(
        "UPDATE resume_sanitization_runs SET sanitized_text=?,sanitized_text_sha256=? "
        "WHERE id=?",
        (original.sanitized_text, _sha(original.sanitized_text.encode()), parent.id),
    )
    connection.execute(
        "UPDATE resume_sanitization_runs SET parent_sanitization_run_id=? WHERE id=?",
        (child.id, parent.id),
    )
    connection.commit()
    assert not is_approved_for_external_use(connection, child.id)
    with pytest.raises(repository.ResumeValidationError, match="cycle"):
        repository.validate_sanitization(connection, child.id)


def test_canonical_manifest_must_match_persisted_child_actions(db) -> None:
    connection, extraction, raw = db
    parent = sanitize_extraction(
        connection, extraction.id, created_at="parent", run_id="d" * 32
    )
    child = create_override(
        connection,
        parent.id,
        {0: "keep"},
        created_at="child",
        run_id="e" * 32,
    )
    approve_external_use(connection, child.id, created_at="approved")
    spoofed = override_manifest({0: "review"}, {})
    spoofed_policy = override_policy_fingerprint(spoofed)
    spoofed_cache = sanitization_cache_fingerprint(
        extraction_run_id=extraction.id,
        raw_text_sha256=_sha(raw.encode()),
        raw_text_byte_size=len(raw.encode()),
        policy_fingerprint=spoofed_policy,
        parent_sanitization_run_id=parent.id,
        override_manifest_json=spoofed,
    )
    connection.execute(
        "UPDATE resume_sanitization_runs "
        "SET override_manifest_json=?,policy_fingerprint=?,cache_fingerprint=? "
        "WHERE id=?",
        (spoofed, spoofed_policy, spoofed_cache, child.id),
    )
    connection.commit()
    assert not is_approved_for_external_use(connection, child.id)
    with pytest.raises(repository.ResumeValidationError, match="action lineage"):
        repository.validate_sanitization(connection, child.id)


def test_post_approval_unresolved_finding_fails_closed(db) -> None:
    connection, extraction, _ = db
    run = sanitize_extraction(
        connection, extraction.id, created_at="run", run_id="d" * 32
    )
    approve_external_use(connection, run.id, created_at="approved")
    connection.execute(
        "UPDATE resume_pii_findings SET disposition='pending' "
        "WHERE sanitization_run_id=? AND ordinal=0",
        (run.id,),
    )
    connection.commit()
    with pytest.raises(repository.ResumeValidationError, match="reconstructed"):
        repository.validate_sanitization(connection, run.id)
    assert not is_approved_for_external_use(connection, run.id)


def test_self_parent_cross_extraction_and_excessive_depth_fail_closed(db) -> None:
    connection, extraction, raw = db
    root = sanitize_extraction(
        connection, extraction.id, created_at="root", run_id="d" * 32
    )
    connection.execute(
        "UPDATE resume_sanitization_runs SET parent_sanitization_run_id=id WHERE id=?",
        (root.id,),
    )
    connection.commit()
    with pytest.raises(repository.ResumeValidationError, match="cycle"):
        repository.validate_sanitization(connection, root.id)

    connection.execute(
        "UPDATE resume_sanitization_runs "
        "SET parent_sanitization_run_id=NULL WHERE id=?",
        (root.id,),
    )
    connection.commit()
    other = replace(extraction, id="b" * 32, cache_fingerprint="other-extraction")
    repository.persist_extraction(
        connection,
        other,
        [
            ResumeTextSegment(
                other.id, 0, 0, len(raw.encode()), "text", _sha(raw.encode())
            )
        ],
    )
    child = create_override(
        connection, root.id, {0: "keep"}, created_at="child", run_id="e" * 32
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    other_cache = sanitization_cache_fingerprint(
        extraction_run_id=other.id,
        raw_text_sha256=_sha(raw.encode()),
        raw_text_byte_size=len(raw.encode()),
        policy_fingerprint=DEFAULT_POLICY_FINGERPRINT,
        parent_sanitization_run_id=None,
        override_manifest_json="{}",
    )
    connection.execute(
        "UPDATE resume_sanitization_runs "
        "SET extraction_run_id=?,cache_fingerprint=? WHERE id=?",
        (other.id, other_cache, root.id),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(repository.ResumeValidationError, match="another extraction"):
        repository.validate_sanitization(connection, child.id)


def test_excessive_lineage_depth_is_rejected_before_child_persistence(db) -> None:
    connection, extraction, _ = db
    current = sanitize_extraction(
        connection, extraction.id, created_at="root", run_id="d" * 32
    )
    for index in range(1, repository.MAX_SANITIZATION_LINEAGE_DEPTH):
        current = create_override(
            connection,
            current.id,
            {0: "keep"},
            created_at=f"child-{index}",
            run_id=f"{index:032x}",
        )
    before = connection.execute(
        "SELECT count(*) FROM resume_sanitization_runs"
    ).fetchone()[0]
    with pytest.raises(repository.ResumeValidationError, match="bounded depth"):
        create_override(
            connection,
            current.id,
            {0: "keep"},
            created_at="too-deep",
            run_id="f" * 32,
        )
    assert (
        connection.execute("SELECT count(*) FROM resume_sanitization_runs").fetchone()[
            0
        ]
        == before
    )


def test_persisted_multibyte_projection_and_delete_crossing(db) -> None:
    connection, extraction, raw = db
    parent = sanitize_extraction(
        connection, extraction.id, created_at="parent", run_id="d" * 32
    )
    findings = repository.sanitization_findings(connection, parent.id)
    name = next(finding for finding in findings if finding.category == "name")
    mapping = _mapping(connection, parent.id)
    replacement = mapping.raw_to_sanitized(
        ByteRange(name.raw_start_byte, name.raw_end_byte)
    )
    assert len(replacement) == 1
    assert mapping.sanitized_to_raw(replacement[0]) == (
        ByteRange(name.raw_start_byte, name.raw_end_byte),
    )
    assert (
        raw.encode()[name.raw_start_byte : name.raw_end_byte] == "Zoë Fiction".encode()
    )

    child = create_override(
        connection,
        parent.id,
        {name.ordinal: "delete"},
        created_at="child",
        run_id="e" * 32,
    )
    child_map = _mapping(connection, child.id)
    deleted = next(span for span in child_map.spans if span.kind == "delete")
    boundary = deleted.sanitized_start_byte
    projected = child_map.sanitized_to_raw(ByteRange(boundary - 1, boundary + 1))
    assert deleted.raw_range in projected


def test_persist_rejects_keep_reference_and_unknown_replacement_ordinal(db) -> None:
    connection, extraction, raw = db
    findings = detect_pii(raw)
    for suffix, mutation in (("b", "keep"), ("c", "unknown")):
        run_id = suffix * 32
        resolved = resolve_sanitization(
            raw,
            findings,
            sanitization_run_id=run_id,
            dispositions={0: "replace", 1: "replace"},
        )
        bad_findings = list(resolved.findings)
        if mutation == "keep":
            bad_findings[0] = replace(bad_findings[0], disposition="keep")
        else:
            bad_findings[0] = replace(
                bad_findings[0], sanitization_span_ordinal=len(resolved.spans)
            )
        run = ResumeSanitizationRun(
            run_id,
            extraction.id,
            None,
            resolved.sanitized_text,
            _sha(resolved.sanitized_text.encode()),
            SANITIZER_FINGERPRINT,
            DEFAULT_POLICY_FINGERPRINT,
            "{}",
            f"bad-{suffix}",
            "succeeded",
            "now",
        )
        with pytest.raises(repository.ResumeValidationError):
            repository.persist_sanitization(
                connection, run, resolved.spans, bad_findings
            )
