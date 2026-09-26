"""Focused M4 deterministic PII and transformation-map coverage."""

import pytest

from careeros.resume_intelligence.coordinates import ByteRange, CoordinateError
from careeros.resume_intelligence.pii import (
    MAX_FINDINGS,
    MAX_TEXT_BYTES,
    PiiFinding,
    detect_pii,
    resolve_sanitization,
)


def test_detector_uses_utf8_byte_coordinates_and_is_deterministic() -> None:
    text = "Name: Élodie Test\nemail: elodie@example.test\nPhone: +30 210 123 4567"
    first = detect_pii(text)
    assert first == detect_pii(text)
    assert [item.category for item in first] == ["name", "email", "phone"]
    raw = text.encode()
    assert (
        raw[first[0].raw_start_byte : first[0].raw_end_byte] == "Élodie Test".encode()
    )


def test_detector_is_label_bound_and_bounded() -> None:
    assert detect_pii("Ada wrote about a fictional address") == ()
    with pytest.raises(CoordinateError):
        detect_pii("x@example.test " * 600, max_findings=1)


@pytest.mark.parametrize(
    ("labelled", "expected", "rule"),
    [
        ("Name: Siobhán O’Connor", "Siobhán O’Connor", "name-label-unicode-v2"),
        ("Name: 王伟", "王伟", "name-label-unicode-v2"),
        ("Name: Anne‑Marie D’Arc", "Anne‑Marie D’Arc", "name-label-unicode-v2"),
        (
            "Email: δοκιμή@παράδειγμα.δοκιμή",
            "δοκιμή@παράδειγμα.δοκιμή",
            "email-unicode-v2",
        ),
    ],
)
def test_labelled_unicode_pii_uses_exact_utf8_byte_ranges(
    labelled: str, expected: str, rule: str
) -> None:
    findings = detect_pii(labelled)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.rule_identity == rule
    assert finding.disposition == "replace"
    assert labelled.encode()[finding.raw_start_byte : finding.raw_end_byte] == (
        expected.encode()
    )
    assert finding.raw_start_byte == len(labelled[: labelled.index(expected)].encode())


def test_ambiguous_labelled_pii_fails_closed_to_review() -> None:
    findings = detect_pii("Name: ???\nEmail: δοκιμή@localhost")
    assert [(finding.category, finding.disposition) for finding in findings] == [
        ("name", "review"),
        ("email", "review"),
    ]
    assert all(finding.confidence is None for finding in findings)


@pytest.mark.parametrize("value", ["123 456", "__ __", "A1 B2"])
def test_non_letter_name_labels_require_review(value: str) -> None:
    finding = detect_pii(f"Name: {value}")[0]
    assert finding.category == "name"
    assert finding.rule_identity == "name-label-ambiguous-v2"
    assert finding.disposition == "review"


def test_ascii_rules_and_overlap_resolution_remain_stable() -> None:
    findings = detect_pii("Name: Ada Fiction\nEmail: ada@example.test")
    assert [(finding.category, finding.rule_identity) for finding in findings] == [
        ("name", "name-label-v1"),
        ("email", "email-v1"),
    ]


def test_large_prefix_preserves_unicode_byte_boundary_without_offset_table() -> None:
    suffix = " é elodie@example.test"
    prefix = "a" * (MAX_TEXT_BYTES - len(suffix.encode()))
    finding = detect_pii(prefix + suffix)[0]
    assert finding.raw_start_byte == len(prefix.encode()) + len(" é ".encode())
    assert (prefix + suffix).encode()[
        finding.raw_start_byte : finding.raw_end_byte
    ] == b"elodie@example.test"


def test_overlapping_findings_share_one_replace_span_and_keep_has_no_reference() -> (
    None
):
    raw = "Élodie elodie@example.test"
    findings = (
        PiiFinding("name", 0, len("Élodie".encode()), "name-v1"),
        PiiFinding("email", len("Élodie ".encode()), len(raw.encode()), "email-v1"),
        PiiFinding("name", 0, len("Élodie ".encode()), "name-overlap"),
    )
    result = resolve_sanitization(
        raw,
        findings,
        sanitization_run_id="run-1",
        dispositions={2: "keep"},
    )
    assert result.sanitized_text == "[PII:name] [PII:email]"
    assert result.findings[1].sanitization_span_ordinal is None
    assert len(result.spans) == 3
    result.transformation_map.validate(
        raw_artifact=raw.encode(), sanitized_artifact=result.sanitized_text.encode()
    )


def test_partial_and_nested_overlaps_form_one_union_span() -> None:
    raw = "0123456789abcdef"
    findings = (
        PiiFinding("name", 2, 10, "partial-a"),
        PiiFinding("email", 8, 14, "partial-b"),
        PiiFinding("phone", 4, 6, "nested"),
    )
    result = resolve_sanitization(raw, findings, sanitization_run_id="run-overlap")
    transformed = [span for span in result.spans if span.kind == "replace"]
    assert [(span.raw_start_byte, span.raw_end_byte) for span in transformed] == [
        (2, 14)
    ]
    assert {finding.sanitization_span_ordinal for finding in result.findings} == {1}


def test_adjacent_findings_remain_separate_transformations() -> None:
    raw = "0123456789"
    findings = (
        PiiFinding("name", 2, 4, "adjacent-a"),
        PiiFinding("email", 4, 7, "adjacent-b"),
    )
    result = resolve_sanitization(raw, findings, sanitization_run_id="run-adjacent")
    assert [
        (span.kind, span.raw_start_byte, span.raw_end_byte) for span in result.spans
    ] == [
        ("copy", 0, 2),
        ("replace", 2, 4),
        ("replace", 4, 7),
        ("copy", 7, 10),
    ]
    assert [finding.sanitization_span_ordinal for finding in result.findings] == [1, 2]


def test_non_owner_overlap_override_drives_shared_replacement_or_delete() -> None:
    raw = "0123456789"
    findings = (
        PiiFinding("name", 2, 7, "owner"),
        PiiFinding("email", 5, 9, "reviewed"),
    )
    replaced = resolve_sanitization(
        raw,
        findings,
        sanitization_run_id="run-non-owner-replace",
        replacements={1: "[REVIEWED]"},
    )
    assert replaced.sanitized_text == "01[REVIEWED]9"
    deleted = resolve_sanitization(
        raw,
        findings,
        sanitization_run_id="run-non-owner-delete",
        replacements={1: ""},
    )
    assert deleted.sanitized_text == "019"


def test_conflicting_overlap_overrides_fail_closed() -> None:
    with pytest.raises(ValueError, match="conflicting replacements"):
        resolve_sanitization(
            "0123456789",
            (
                PiiFinding("name", 2, 7, "first"),
                PiiFinding("email", 5, 9, "second"),
            ),
            sanitization_run_id="run-conflict",
            replacements={0: "[A]", 1: "[B]"},
        )


def test_replacement_expansion_and_projection_remain_discontinuous() -> None:
    raw = "A elodie@example.test Z"
    finding = detect_pii(raw)[0]
    result = resolve_sanitization(
        raw,
        (finding,),
        sanitization_run_id="run-2",
        replacements={0: "[CONTACT_REMOVED]"},
    )
    source = raw.encode()
    query = ByteRange(0, len(source))
    projected = result.transformation_map.raw_to_sanitized(query)
    assert len(projected) == 3
    replacement = projected[1]
    assert result.transformation_map.sanitized_to_raw(replacement) == (
        ByteRange(finding.raw_start_byte, finding.raw_end_byte),
    )


def test_review_override_can_delete_and_tiling_catches_tampering() -> None:
    raw = "email: ada@example.test"
    finding = detect_pii(raw)[0]
    result = resolve_sanitization(
        raw,
        (finding,),
        sanitization_run_id="child",
        dispositions={0: "review"},
        replacements={0: ""},
    )
    assert result.sanitized_text == "email: "
    assert any(span.kind == "delete" for span in result.spans)
    with pytest.raises(CoordinateError):
        result.transformation_map.raw_to_sanitized(ByteRange(0, len(raw.encode()) + 1))


def test_invalid_finding_boundary_fails_closed() -> None:
    with pytest.raises(CoordinateError):
        resolve_sanitization(
            "é@example.test",
            (PiiFinding("email", 1, 15, "bad"),),
            sanitization_run_id="run-3",
        )


def test_unknown_overrides_and_oversized_replacement_fail_closed() -> None:
    finding = PiiFinding("name", 0, 1, "name")
    with pytest.raises(ValueError, match="unknown sanitization disposition"):
        resolve_sanitization(
            "x", (finding,), sanitization_run_id="unknown", dispositions={1: "keep"}
        )
    with pytest.raises(ValueError, match="replacement must be text"):
        resolve_sanitization(
            "x",
            (finding,),
            sanitization_run_id="wrong-type",
            replacements={0: 1},  # type: ignore[dict-item]
        )
    with pytest.raises(CoordinateError, match="sanitized text exceeds"):
        resolve_sanitization(
            "x",
            (finding,),
            sanitization_run_id="oversized",
            replacements={0: "x" * (MAX_TEXT_BYTES + 1)},
        )


def test_maximum_disjoint_findings_fit_complete_repository_map_bound() -> None:
    raw = " " + " ".join("x" for _ in range(MAX_FINDINGS)) + " "
    findings = tuple(
        PiiFinding("name", 1 + index * 2, 2 + index * 2, f"name-{index}")
        for index in range(MAX_FINDINGS)
    )
    result = resolve_sanitization(raw, findings, sanitization_run_id="max-findings")
    assert len(result.findings) == MAX_FINDINGS
    assert len(result.spans) == MAX_FINDINGS * 2 + 1
    assert len(result.spans) < 1000
    with pytest.raises(CoordinateError, match="finding limit"):
        resolve_sanitization(
            raw + "x",
            (
                *findings,
                PiiFinding("name", len(raw.encode()), len(raw.encode()) + 1, "x"),
            ),
            sanitization_run_id="too-many-findings",
        )
