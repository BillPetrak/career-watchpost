from hashlib import sha256

import pytest

from careeros.resume_intelligence.coordinates import (
    ByteRange,
    CoordinateError,
    TransformationMap,
    TransformationSpan,
    sha256_slice,
)
from careeros.resume_intelligence.models import ResumePiiFinding


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def span(
    ordinal: int, kind: str, raw: bytes, clean: bytes, raw_start: int, clean_start: int
) -> TransformationSpan:
    return TransformationSpan(
        ordinal,
        kind,
        raw_start,
        raw_start + len(raw),
        clean_start,
        clean_start + len(clean),
        digest(raw),
        digest(clean),
    )  # type: ignore[arg-type]


def test_utf8_byte_ranges_and_boundaries() -> None:
    artifact = "Aé🙂Z".encode()
    assert ByteRange(1, 3).slice(artifact) == "é".encode()
    assert sha256_slice(artifact, 3, 7) == digest("🙂".encode())
    with pytest.raises(CoordinateError):
        ByteRange(2, 3).validate(artifact)
    assert ByteRange(3, 3).validate(artifact).length == 0
    with pytest.raises(CoordinateError):
        ByteRange(0, 1).validate(b"\xff")
    with pytest.raises(CoordinateError):
        ByteRange(0, 0).validate(artifact, allow_empty=False)


def test_map_projects_replacements_deletions_and_discontinuous_ranges() -> None:
    raw = b"abcSECRETdefXYZ"
    clean = b"abc[REDACTED]def"
    spans = (
        span(0, "copy", b"abc", b"abc", 0, 0),
        span(1, "replace", b"SECRET", b"[REDACTED]", 3, 3),
        span(2, "copy", b"def", b"def", 9, 13),
        span(3, "delete", b"XYZ", b"", 12, 16),
    )
    mapping = TransformationMap.from_spans(
        spans,
        raw_size=len(raw),
        sanitized_size=len(clean),
        raw_artifact=raw,
        sanitized_artifact=clean,
    )
    assert mapping.raw_to_sanitized(ByteRange(0, len(raw))) == (
        ByteRange(0, 3),
        ByteRange(3, 13),
        ByteRange(13, 16),
        ByteRange(16, 16),
    )
    assert mapping.sanitized_to_raw(ByteRange(2, 15)) == (
        ByteRange(2, 3),
        ByteRange(3, 9),
        ByteRange(9, 11),
    )


def test_map_rejects_gaps_and_hash_mismatch() -> None:
    with pytest.raises(CoordinateError):
        TransformationMap.from_spans(
            (span(0, "copy", b"a", b"a", 1, 0),), raw_size=1, sanitized_size=1
        )
    bad = span(0, "copy", b"a", b"a", 0, 0)
    bad = TransformationSpan(**{**bad.__dict__, "raw_slice_sha256": "0" * 64})
    with pytest.raises(CoordinateError):
        TransformationMap.from_spans(
            (bad,),
            raw_size=1,
            sanitized_size=1,
            raw_artifact=b"a",
            sanitized_artifact=b"a",
        )
    with pytest.raises(CoordinateError, match="ordinals"):
        TransformationMap.from_spans(
            (span(1, "copy", b"a", b"a", 0, 0),),
            raw_size=1,
            sanitized_size=1,
        )
    with pytest.raises(CoordinateError, match="length differs"):
        TransformationMap.from_spans(
            (span(0, "copy", b"a", b"a", 0, 0),),
            raw_size=1,
            sanitized_size=1,
            raw_artifact=b"aa",
        )
    with pytest.raises(CoordinateError, match="slice SHA-256"):
        TransformationMap.from_spans(
            (
                TransformationSpan(
                    0,
                    "copy",
                    0,
                    1,
                    0,
                    1,
                    digest(b"a"),
                    digest(b"a"),
                ),
            ),
            raw_size=1,
            sanitized_size=1,
            raw_artifact=b"a",
            sanitized_artifact=b"b",
        )
    with pytest.raises(CoordinateError, match="length differs"):
        TransformationMap.from_spans(
            (span(0, "copy", b"a", b"a", 0, 0),),
            raw_size=1,
            sanitized_size=1,
            sanitized_artifact=b"aa",
        )
    with pytest.raises(CoordinateError):
        TransformationMap.from_spans(
            (TransformationSpan(0, "copy", 0, 1, 0, 1, "A" * 64, "a" * 64),),
            raw_size=1,
            sanitized_size=1,
        )
    with pytest.raises(CoordinateError, match="source and result hashes differ"):
        TransformationMap.from_spans(
            (TransformationSpan(0, "copy", 0, 1, 0, 1, digest(b"a"), digest(b"b")),),
            raw_size=1,
            sanitized_size=1,
        )


def test_shorter_longer_replacements_and_deletion_boundary() -> None:
    raw, clean = b"abCDEfghij", b"abXfghij"
    mapping = TransformationMap.from_spans(
        (
            span(0, "copy", b"ab", b"ab", 0, 0),
            span(1, "replace", b"CDE", b"X", 2, 2),
            span(2, "copy", b"fghij", b"fghij", 5, 3),
        ),
        raw_size=len(raw),
        sanitized_size=len(clean),
        raw_artifact=raw,
        sanitized_artifact=clean,
    )
    assert mapping.sanitized_to_raw(ByteRange(2, 3)) == (ByteRange(2, 5),)
    raw2, clean2 = b"abCDEfghij", b"abLONGfghij"
    longer = TransformationMap.from_spans(
        (
            span(0, "copy", b"ab", b"ab", 0, 0),
            span(1, "replace", b"CDE", b"LONG", 2, 2),
            span(2, "copy", b"fghij", b"fghij", 5, 6),
        ),
        raw_size=len(raw2),
        sanitized_size=len(clean2),
        raw_artifact=raw2,
        sanitized_artifact=clean2,
    )
    assert longer.raw_to_sanitized(ByteRange(2, 5)) == (ByteRange(2, 6),)
    deleted = TransformationMap.from_spans(
        (
            span(0, "copy", b"ab", b"ab", 0, 0),
            span(1, "delete", b"CD", b"", 2, 2),
            span(2, "copy", b"ef", b"ef", 4, 2),
        ),
        raw_size=6,
        sanitized_size=4,
    )
    assert deleted.sanitized_to_raw(ByteRange(1, 3)) == (
        ByteRange(1, 2),
        ByteRange(2, 4),
        ByteRange(4, 5),
    )
    assert deleted.sanitized_to_raw(ByteRange(2, 4)) == (ByteRange(4, 6),)


def test_half_open_deletion_boundaries_and_zero_length_queries() -> None:
    mapping = TransformationMap.from_spans(
        (
            span(0, "copy", b"ab", b"ab", 0, 0),
            span(1, "delete", b"CD", b"", 2, 2),
            span(2, "copy", b"ef", b"ef", 4, 2),
        ),
        raw_size=6,
        sanitized_size=4,
    )

    # A deletion is included only when its sanitized boundary is strictly
    # inside the half-open query. Merely touching either endpoint excludes it.
    assert mapping.sanitized_to_raw(ByteRange(1, 3)) == (
        ByteRange(1, 2),
        ByteRange(2, 4),
        ByteRange(4, 5),
    )
    assert mapping.sanitized_to_raw(ByteRange(2, 3)) == (ByteRange(4, 5),)
    assert mapping.sanitized_to_raw(ByteRange(1, 2)) == (ByteRange(1, 2),)
    assert mapping.sanitized_to_raw(ByteRange(0, 1)) == (ByteRange(0, 1),)
    assert mapping.sanitized_to_raw(ByteRange(3, 4)) == (ByteRange(5, 6),)
    assert mapping.sanitized_to_raw(ByteRange(2, 2)) == ()
    assert mapping.raw_to_sanitized(ByteRange(2, 2)) == ()

    mixed = TransformationMap.from_spans(
        (
            span(0, "copy", b"a", b"a", 0, 0),
            span(1, "replace", b"BC", b"[X]", 1, 1),
            span(2, "delete", b"DE", b"", 3, 4),
            span(3, "copy", b"f", b"f", 5, 4),
        ),
        raw_size=6,
        sanitized_size=5,
    )
    assert mixed.sanitized_to_raw(ByteRange(0, 5)) == (
        ByteRange(0, 1),
        ByteRange(1, 3),
        ByteRange(3, 5),
        ByteRange(5, 6),
    )


def test_repeated_placeholders_and_adjacent_operations_are_ordinal_based() -> None:
    mapping = TransformationMap.from_spans(
        (
            span(0, "copy", b"A", b"A", 0, 0),
            span(1, "replace", b"xx", b"xx", 1, 1),
            span(2, "copy", b"B", b"B", 3, 3),
            span(3, "replace", b"xx", b"xx", 4, 4),
            span(4, "copy", b"C", b"C", 6, 6),
        ),
        raw_size=7,
        sanitized_size=7,
    )
    assert mapping.raw_to_sanitized(ByteRange(1, 6)) == (
        ByteRange(1, 3),
        ByteRange(3, 4),
        ByteRange(4, 6),
    )


def test_identity_overlapping_findings_and_independent_override_maps() -> None:
    identity = TransformationMap.from_spans(
        (span(0, "copy", b"abcdef", b"abcdef", 0, 0),),
        raw_size=6,
        sanitized_size=6,
        raw_artifact=b"abcdef",
        sanitized_artifact=b"abcdef",
    )
    assert identity.raw_to_sanitized(ByteRange(1, 5)) == (ByteRange(1, 5),)

    findings = (
        ResumePiiFinding("s", 0, "name", "rule", "name", 1.0, "replace", 1, 4),
        ResumePiiFinding("s", 1, "email", "rule", "email", 0.9, "replace", 3, 6),
    )
    assert findings[0].raw_end_byte > findings[1].raw_start_byte
    resolved = TransformationMap.from_spans(
        (
            span(0, "copy", b"a", b"a", 0, 0),
            span(1, "replace", b"bcdef", b"[PII]", 1, 1),
        ),
        raw_size=6,
        sanitized_size=6,
    )
    assert tuple(item.raw_range for item in resolved.spans) == (
        ByteRange(0, 1),
        ByteRange(1, 6),
    )

    child = TransformationMap.from_spans(
        (
            span(0, "copy", b"abc", b"abc", 0, 0),
            span(1, "replace", b"def", b"[X]", 3, 3),
        ),
        raw_size=6,
        sanitized_size=6,
    )
    assert child.spans != resolved.spans
    assert identity.spans == (span(0, "copy", b"abcdef", b"abcdef", 0, 0),)
