"""Pure domain contracts for resume extraction and sanitization."""

from .coordinates import (
    ByteRange,
    CoordinateError,
    TransformationMap,
    TransformationSpan,
    sha256_slice,
    validate_sha256,
    validate_slice_hash,
)
from .models import (
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

__all__ = [
    "ByteRange",
    "CoordinateError",
    "CareerFactCandidate",
    "CareerFactEvidence",
    "CareerFactReviewEvent",
    "CareerFactRun",
    "ResumeExtractionRun",
    "ResumePiiFinding",
    "ResumeSanitizationRun",
    "ResumeTextSegment",
    "SanitizationReviewEvent",
    "SanitizationSpan",
    "TransformationMap",
    "TransformationSpan",
    "sha256_slice",
    "validate_sha256",
    "validate_slice_hash",
]
