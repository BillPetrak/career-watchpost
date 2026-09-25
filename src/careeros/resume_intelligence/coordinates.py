"""UTF-8 byte-coordinate and sanitization transformation contracts.

Coordinates in CareerOS are offsets into one immutable byte artifact.  This
module intentionally has no database or filesystem dependencies.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

TransformKind = Literal["copy", "replace", "delete"]
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class CoordinateError(ValueError):
    """A coordinate, hash, or transformation-map invariant is invalid."""


@dataclass(frozen=True, order=True)
class ByteRange:
    """A zero-based, half-open range of UTF-8 bytes."""

    start_byte: int
    end_byte: int

    def __post_init__(self) -> None:
        if self.start_byte < 0 or self.end_byte < 0:
            raise CoordinateError("byte offsets must be non-negative")
        if self.start_byte > self.end_byte:
            raise CoordinateError("range start must not exceed range end")

    @property
    def length(self) -> int:
        return self.end_byte - self.start_byte

    def validate(self, artifact: bytes, *, allow_empty: bool = True) -> ByteRange:
        validate_range(
            artifact, self.start_byte, self.end_byte, allow_empty=allow_empty
        )
        return self

    def slice(self, artifact: bytes) -> bytes:
        self.validate(artifact)
        return artifact[self.start_byte : self.end_byte]


def _is_utf8_boundary(artifact: bytes, offset: int) -> bool:
    return offset == 0 or offset == len(artifact) or (artifact[offset] & 0xC0) != 0x80


def validate_range(
    artifact: bytes,
    start_byte: int,
    end_byte: int,
    *,
    allow_empty: bool = True,
) -> ByteRange:
    """Validate bounds and UTF-8 code-point boundaries for a byte range."""

    result = ByteRange(start_byte, end_byte)
    if end_byte > len(artifact):
        raise CoordinateError("range exceeds artifact byte length")
    if not allow_empty and result.length == 0:
        raise CoordinateError("empty ranges are not permitted here")
    # Decode the complete artifact first: malformed stored text cannot have a
    # meaningful code-point coordinate, even when the requested slice is ASCII.
    try:
        artifact.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CoordinateError("artifact is not valid UTF-8") from error
    if not _is_utf8_boundary(artifact, start_byte) or not _is_utf8_boundary(
        artifact, end_byte
    ):
        raise CoordinateError("range must begin and end on UTF-8 code-point boundaries")
    return result


def sha256_slice(artifact: bytes, start_byte: int, end_byte: int) -> str:
    """Return the lowercase SHA-256 digest of one validated artifact slice."""

    validate_range(artifact, start_byte, end_byte)
    return hashlib.sha256(artifact[start_byte:end_byte]).hexdigest()


def validate_slice_hash(
    artifact: bytes, start_byte: int, end_byte: int, expected_sha256: str
) -> str:
    """Validate and return an expected exact slice hash."""

    validate_sha256(expected_sha256)
    actual = sha256_slice(artifact, start_byte, end_byte)
    if actual != expected_sha256:
        raise CoordinateError("slice SHA-256 does not match the artifact")
    return actual


def validate_sha256(value: str) -> str:
    """Validate and return a lowercase SHA-256 hexadecimal digest."""

    if _SHA256_PATTERN.fullmatch(value) is None:
        raise CoordinateError("SHA-256 must be 64 lowercase hexadecimal characters")
    return value


@dataclass(frozen=True)
class TransformationSpan:
    """One ordered raw-to-sanitized operation."""

    ordinal: int
    kind: TransformKind
    raw_start_byte: int
    raw_end_byte: int
    sanitized_start_byte: int
    sanitized_end_byte: int
    raw_slice_sha256: str
    sanitized_slice_sha256: str
    placeholder_key: str | None = None

    @property
    def raw_range(self) -> ByteRange:
        return ByteRange(self.raw_start_byte, self.raw_end_byte)

    @property
    def sanitized_range(self) -> ByteRange:
        return ByteRange(self.sanitized_start_byte, self.sanitized_end_byte)

    def validate_shape(self) -> None:
        _ = self.raw_range
        _ = self.sanitized_range
        if self.kind not in ("copy", "replace", "delete"):
            raise CoordinateError("unknown transformation kind")
        raw_len = self.raw_end_byte - self.raw_start_byte
        clean_len = self.sanitized_end_byte - self.sanitized_start_byte
        if self.ordinal < 0:
            raise CoordinateError("transformation ordinal must be non-negative")
        if raw_len < 0 or clean_len < 0:
            raise CoordinateError("transformation ranges must be ordered")
        if self.kind == "copy" and (raw_len == 0 or raw_len != clean_len):
            raise CoordinateError("copy spans must be non-empty and equal length")
        if self.kind == "replace" and (raw_len == 0 or clean_len == 0):
            raise CoordinateError("replace spans must have non-empty source/result")
        if self.kind == "delete" and (raw_len == 0 or clean_len != 0):
            raise CoordinateError(
                "delete spans must have non-empty source and empty result"
            )
        if self.kind != "replace" and self.placeholder_key is not None:
            raise CoordinateError("placeholder keys are valid only for replacements")


@dataclass(frozen=True)
class TransformationMap:
    """A complete, ordered, independently materialized transformation map."""

    spans: tuple[TransformationSpan, ...]
    raw_size: int
    sanitized_size: int

    @classmethod
    def from_spans(
        cls,
        spans: Iterable[TransformationSpan],
        *,
        raw_size: int,
        sanitized_size: int,
        raw_artifact: bytes | None = None,
        sanitized_artifact: bytes | None = None,
    ) -> TransformationMap:
        result = cls(tuple(spans), raw_size, sanitized_size)
        result.validate(
            raw_artifact=raw_artifact, sanitized_artifact=sanitized_artifact
        )
        return result

    def validate(
        self,
        *,
        raw_artifact: bytes | None = None,
        sanitized_artifact: bytes | None = None,
    ) -> None:
        if self.raw_size < 0 or self.sanitized_size < 0:
            raise CoordinateError("artifact sizes must be non-negative")
        if raw_artifact is not None and len(raw_artifact) != self.raw_size:
            raise CoordinateError("raw artifact length differs from declared size")
        if (
            sanitized_artifact is not None
            and len(sanitized_artifact) != self.sanitized_size
        ):
            raise CoordinateError(
                "sanitized artifact length differs from declared size"
            )
        raw_cursor = clean_cursor = 0
        previous_ordinal = -1
        for span in self.spans:
            span.validate_shape()
            validate_sha256(span.raw_slice_sha256)
            validate_sha256(span.sanitized_slice_sha256)
            if (
                span.kind == "copy"
                and span.raw_slice_sha256 != span.sanitized_slice_sha256
            ):
                raise CoordinateError("copy span source and result hashes differ")
            if span.ordinal != previous_ordinal + 1:
                raise CoordinateError(
                    "transformation ordinals must be contiguous from zero"
                )
            previous_ordinal = span.ordinal
            if (
                span.raw_start_byte != raw_cursor
                or span.sanitized_start_byte != clean_cursor
            ):
                raise CoordinateError(
                    "transformation spans must tile both artifacts gap-free"
                )
            if (
                span.raw_end_byte > self.raw_size
                or span.sanitized_end_byte > self.sanitized_size
            ):
                raise CoordinateError("transformation span exceeds artifact size")
            if raw_artifact is not None:
                validate_slice_hash(
                    raw_artifact,
                    span.raw_start_byte,
                    span.raw_end_byte,
                    span.raw_slice_sha256,
                )
                validate_range(raw_artifact, span.raw_start_byte, span.raw_end_byte)
            if sanitized_artifact is not None:
                validate_slice_hash(
                    sanitized_artifact,
                    span.sanitized_start_byte,
                    span.sanitized_end_byte,
                    span.sanitized_slice_sha256,
                )
                validate_range(
                    sanitized_artifact,
                    span.sanitized_start_byte,
                    span.sanitized_end_byte,
                )
            if (
                span.kind == "copy"
                and raw_artifact is not None
                and sanitized_artifact is not None
            ):
                if (
                    raw_artifact[span.raw_start_byte : span.raw_end_byte]
                    != sanitized_artifact[
                        span.sanitized_start_byte : span.sanitized_end_byte
                    ]
                ):
                    raise CoordinateError("copy span source and result bytes differ")
            raw_cursor, clean_cursor = span.raw_end_byte, span.sanitized_end_byte
        if raw_cursor != self.raw_size or clean_cursor != self.sanitized_size:
            raise CoordinateError(
                "transformation spans must completely tile both artifacts"
            )

    def raw_to_sanitized(self, query: ByteRange) -> tuple[ByteRange, ...]:
        """Project a raw query, retaining multiple discontinuous results."""

        if query.end_byte > self.raw_size:
            raise CoordinateError("query exceeds raw artifact size")
        result: list[ByteRange] = []
        for span in self.spans:
            overlap_start = max(query.start_byte, span.raw_start_byte)
            overlap_end = min(query.end_byte, span.raw_end_byte)
            if overlap_start >= overlap_end:
                continue
            if span.kind == "copy":
                delta = overlap_start - span.raw_start_byte
                result.append(
                    ByteRange(
                        span.sanitized_start_byte + delta,
                        span.sanitized_start_byte + delta + overlap_end - overlap_start,
                    )
                )
            elif span.kind == "replace":
                result.append(span.sanitized_range)
            else:
                result.append(
                    ByteRange(span.sanitized_start_byte, span.sanitized_start_byte)
                )
        return tuple(result)

    def sanitized_to_raw(self, query: ByteRange) -> tuple[ByteRange, ...]:
        """Project a sanitized query, retaining multiple discontinuous results."""

        if query.end_byte > self.sanitized_size:
            raise CoordinateError("query exceeds sanitized artifact size")
        result: list[ByteRange] = []
        for span in self.spans:
            overlap_start = max(query.start_byte, span.sanitized_start_byte)
            overlap_end = min(query.end_byte, span.sanitized_end_byte)
            if (
                span.kind == "delete"
                and query.start_byte < span.sanitized_start_byte < query.end_byte
            ):
                result.append(span.raw_range)
                continue
            if overlap_start >= overlap_end:
                continue
            if span.kind == "copy":
                delta = overlap_start - span.sanitized_start_byte
                result.append(
                    ByteRange(
                        span.raw_start_byte + delta,
                        span.raw_start_byte + delta + overlap_end - overlap_start,
                    )
                )
            else:
                result.append(span.raw_range)
        return tuple(result)

    # Explicit names are useful at call sites where the direction is not
    # obvious, while the shorter methods remain convenient for pure callers.
    def project_raw_to_sanitized(self, query: ByteRange) -> tuple[ByteRange, ...]:
        return self.raw_to_sanitized(query)

    def project_sanitized_to_raw(self, query: ByteRange) -> tuple[ByteRange, ...]:
        return self.sanitized_to_raw(query)
