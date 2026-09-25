"""Persistence-neutral resume intelligence vocabulary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from .coordinates import (
    ByteRange,
    CoordinateError,
    TransformationSpan,
    validate_sha256,
)

CoordinateSpace = Literal["extracted", "sanitized"]
ResumeFormat = Literal["pdf", "docx", "text", "unknown"]
RunOutcome = Literal["succeeded", "failed"]
SanitizationDisposition = Literal["pending", "replace", "keep", "review"]
FactMode = Literal["deterministic", "model"]
FactOrigin = Literal["deterministic", "model"]
SanitizationReviewEventKind = Literal["approved", "revoked"]
FactReviewEventKind = Literal["approved", "rejected", "approved_edit"]


@dataclass(frozen=True)
class ResumeExtractionRun:
    id: str
    material_version_id: str
    source_name: str
    source_media_type: str
    source_byte_size: int
    source_sha256: str
    source_format: ResumeFormat
    detected_format: ResumeFormat
    outcome: RunOutcome
    raw_text: str | None
    raw_text_sha256: str | None
    segment_count: int
    extractor_fingerprint: str
    cache_fingerprint: str
    created_at: str

    def __post_init__(self) -> None:
        if self.outcome not in ("succeeded", "failed"):
            raise ValueError("invalid extraction outcome")
        if self.source_format not in ("pdf", "docx", "text", "unknown"):
            raise ValueError("invalid declared resume format")
        if self.detected_format not in ("pdf", "docx", "text", "unknown"):
            raise ValueError("invalid detected resume format")
        validate_sha256(self.source_sha256)
        if self.source_byte_size < 1 or self.segment_count < 0:
            raise ValueError("invalid extraction source size or segment count")
        succeeded = self.outcome == "succeeded"
        if succeeded != (
            self.raw_text is not None and self.raw_text_sha256 is not None
        ):
            raise ValueError("extraction outcome and raw artifact must agree")
        if self.raw_text_sha256 is not None:
            validate_sha256(self.raw_text_sha256)
            assert self.raw_text is not None
            if (
                hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()
                != self.raw_text_sha256
            ):
                raise ValueError("raw text SHA-256 does not match the artifact")


@dataclass(frozen=True)
class ResumeSanitizationRun:
    id: str
    extraction_run_id: str
    parent_sanitization_run_id: str | None
    sanitized_text: str | None
    sanitized_text_sha256: str | None
    sanitizer_fingerprint: str
    policy_fingerprint: str
    override_manifest_json: str
    cache_fingerprint: str
    outcome: RunOutcome
    created_at: str

    def __post_init__(self) -> None:
        if self.outcome not in ("succeeded", "failed"):
            raise ValueError("invalid sanitization outcome")
        succeeded = self.outcome == "succeeded"
        if succeeded != (
            self.sanitized_text is not None and self.sanitized_text_sha256 is not None
        ):
            raise ValueError("sanitization outcome and artifact must agree")
        if self.sanitized_text_sha256 is not None:
            validate_sha256(self.sanitized_text_sha256)
            assert self.sanitized_text is not None
            if (
                hashlib.sha256(self.sanitized_text.encode("utf-8")).hexdigest()
                != self.sanitized_text_sha256
            ):
                raise ValueError("sanitized text SHA-256 does not match the artifact")


@dataclass(frozen=True)
class ResumePiiFinding:
    sanitization_run_id: str
    ordinal: int
    category: str
    origin: str
    rule_identity: str
    confidence: float | None
    disposition: SanitizationDisposition
    raw_start_byte: int
    raw_end_byte: int
    sanitization_span_ordinal: int | None = None

    def __post_init__(self) -> None:
        if (
            self.ordinal < 0
            or self.raw_start_byte < 0
            or self.raw_end_byte <= self.raw_start_byte
        ):
            raise CoordinateError("invalid PII finding ordinal or range")
        if self.disposition not in ("pending", "replace", "keep", "review"):
            raise ValueError("invalid sanitization disposition")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("PII confidence must be between zero and one")
        if (
            self.sanitization_span_ordinal is not None
            and self.sanitization_span_ordinal < 0
        ):
            raise ValueError("resolved sanitization span ordinal must be non-negative")


@dataclass(frozen=True)
class SanitizationReviewEvent:
    sanitization_run_id: str
    event_kind: SanitizationReviewEventKind
    created_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        if self.event_kind not in ("approved", "revoked"):
            raise ValueError("invalid sanitization review event kind")


@dataclass(frozen=True)
class CareerFactRun:
    id: str
    extraction_run_id: str
    sanitization_run_id: str | None
    input_kind: CoordinateSpace
    mode: FactMode
    outcome: RunOutcome
    cache_fingerprint: str
    input_sha256: str
    input_byte_size: int
    token_count: int | None
    extractor_fingerprint: str | None
    schema_fingerprint: str
    provider_fingerprint: str | None
    model_fingerprint: str | None
    prompt_fingerprint: str | None
    created_at: str

    def __post_init__(self) -> None:
        if self.outcome not in ("succeeded", "failed"):
            raise ValueError("invalid career fact outcome")
        if self.mode not in ("deterministic", "model"):
            raise ValueError("invalid career fact mode")
        if self.input_kind not in ("extracted", "sanitized"):
            raise ValueError("invalid career fact input kind")
        if self.mode == "deterministic" and self.input_kind != "extracted":
            raise CoordinateError("deterministic fact runs require extracted input")
        if self.mode == "model" and self.input_kind != "sanitized":
            raise CoordinateError("model fact runs require sanitized input")
        if self.input_kind == "extracted" and self.sanitization_run_id is not None:
            raise CoordinateError("extracted fact runs cannot pair a sanitization run")
        if self.input_kind == "sanitized" and self.sanitization_run_id is None:
            raise CoordinateError("sanitized fact runs require a sanitization run")
        validate_sha256(self.input_sha256)
        if self.input_byte_size < 0 or (
            self.token_count is not None and self.token_count < 0
        ):
            raise ValueError("fact input and token counts must be non-negative")


@dataclass(frozen=True)
class CareerFactReviewEvent:
    candidate_id: str
    event_kind: FactReviewEventKind
    created_at: str
    edited_value_json: str | None = None
    edited_value_sha256: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.event_kind not in ("approved", "rejected", "approved_edit"):
            raise ValueError("invalid career fact review event kind")
        has_edit = (
            self.edited_value_json is not None or self.edited_value_sha256 is not None
        )
        if self.event_kind == "approved_edit" and not (
            self.edited_value_json is not None and self.edited_value_sha256 is not None
        ):
            raise ValueError("edit approval requires edited value and hash")
        if self.event_kind != "approved_edit" and has_edit:
            raise ValueError("edited values are valid only for edit approval")
        if self.edited_value_sha256 is not None:
            validate_sha256(self.edited_value_sha256)


@dataclass(frozen=True)
class ResumeTextSegment:
    extraction_run_id: str
    ordinal: int
    raw_start_byte: int
    raw_end_byte: int
    source_locator: str
    slice_sha256: str

    def __post_init__(self) -> None:
        if self.ordinal < 0:
            raise CoordinateError("segment ordinal must be non-negative")
        ByteRange(self.raw_start_byte, self.raw_end_byte)
        validate_sha256(self.slice_sha256)


@dataclass(frozen=True)
class SanitizationSpan:
    sanitization_run_id: str
    ordinal: int
    kind: Literal["copy", "replace", "delete"]
    raw_start_byte: int
    raw_end_byte: int
    sanitized_start_byte: int
    sanitized_end_byte: int
    raw_slice_sha256: str
    sanitized_slice_sha256: str
    placeholder_key: str | None = None

    def __post_init__(self) -> None:
        TransformationSpan(
            self.ordinal,
            self.kind,
            self.raw_start_byte,
            self.raw_end_byte,
            self.sanitized_start_byte,
            self.sanitized_end_byte,
            self.raw_slice_sha256,
            self.sanitized_slice_sha256,
            self.placeholder_key,
        ).validate_shape()
        validate_sha256(self.raw_slice_sha256)
        validate_sha256(self.sanitized_slice_sha256)


@dataclass(frozen=True)
class CareerFactEvidence:
    candidate_id: str
    ordinal: int
    coordinate_space: CoordinateSpace
    start_byte: int
    end_byte: int
    slice_sha256: str

    def __post_init__(self) -> None:
        if self.coordinate_space not in ("extracted", "sanitized"):
            raise CoordinateError("coordinate space must be extracted or sanitized")
        if self.ordinal < 0:
            raise CoordinateError("evidence ordinal must be non-negative")
        ByteRange(self.start_byte, self.end_byte)
        validate_sha256(self.slice_sha256)


@dataclass(frozen=True)
class CareerFactCandidate:
    id: str
    fact_run_id: str
    fact_type: str
    normalized_key: str
    canonical_value_json: str
    value_sha256: str
    confidence: float | None
    origin: FactOrigin

    def __post_init__(self) -> None:
        if self.origin not in ("deterministic", "model"):
            raise ValueError("invalid career fact origin")
        validate_sha256(self.value_sha256)
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("candidate confidence must be between zero and one")
