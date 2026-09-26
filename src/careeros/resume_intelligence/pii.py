"""Deterministic, local PII detection and immutable sanitization maps.

This module operates only on the exact text supplied by its caller.  It does
not read materials, call a model, log document content, or perform network
work.  All offsets exposed here are UTF-8 byte offsets.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from .coordinates import (
    ByteRange,
    CoordinateError,
    TransformationMap,
    TransformationSpan,
    sha256_slice,
)
from .models import ResumePiiFinding, SanitizationSpan

MAX_TEXT_BYTES = 4 * 1024 * 1024
# A complete map can contain at most two spans per disjoint finding plus one.
# Keeping this below 500 guarantees the frozen repository's 1000-row bounded
# aggregate read can always materialize every span without truncation.
MAX_FINDINGS = 499
Disposition = Literal["pending", "replace", "keep", "review"]

_EMAIL = re.compile(
    r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w.-])"
)
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d .()/-]{6,}\d)(?!\w)")
_NAME_LABEL = re.compile(
    r"(?im)^\s*(?:full\s+name|preferred\s+name|candidate|name)\s*[:=-]\s*([^\r\n]+)"
)
_EMAIL_LABEL = re.compile(r"(?im)^\s*(?:e-?mail)\s*[:=-]\s*([^\r\n]+)")
_NAME_PUNCTUATION = frozenset(".'’ʼ-‐‑‒–")
_EMAIL_LOCAL_ASCII = frozenset(".!#$%&'*+/=?^_`{|}~-")


@dataclass(frozen=True)
class PiiFinding:
    """A detector finding before it is attached to a sanitization run."""

    category: str
    raw_start_byte: int
    raw_end_byte: int
    rule_identity: str
    confidence: float | None = 1.0
    disposition: Disposition = "pending"

    def __post_init__(self) -> None:
        if self.category not in {"name", "email", "phone"}:
            raise ValueError("unsupported PII category")
        ByteRange(self.raw_start_byte, self.raw_end_byte)
        if self.raw_start_byte == self.raw_end_byte:
            raise CoordinateError("PII findings cannot be empty")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("PII confidence must be between zero and one")


@dataclass(frozen=True)
class SanitizationResult:
    """Materialized sanitized artifact, map, and persisted-domain records."""

    sanitized_text: str
    transformation_map: TransformationMap
    spans: tuple[SanitizationSpan, ...]
    findings: tuple[ResumePiiFinding, ...]


def _byte_boundaries(text: str, boundaries: set[int]) -> dict[int, int]:
    """Convert only requested character boundaries to UTF-8 byte offsets."""
    result: dict[int, int] = {}
    byte_offset = 0
    for character_index, character in enumerate(text):
        if character_index in boundaries:
            result[character_index] = byte_offset
        byte_offset += len(character.encode("utf-8"))
    if len(text) in boundaries:
        result[len(text)] = byte_offset
    return result


def _finding(
    offsets: dict[int, int],
    category: str,
    start: int,
    end: int,
    rule: str,
    confidence: float | None,
    disposition: Disposition,
) -> PiiFinding:
    raw_start, raw_end = offsets[start], offsets[end]
    return PiiFinding(
        category,
        raw_start,
        raw_end,
        rule,
        confidence,
        disposition,
    )


def _unicode_kind(character: str) -> str:
    return unicodedata.category(character)[0]


def _valid_name_value(value: str) -> str | None:
    tokens = re.split(r"[ \t]+", value)
    if not 1 <= len(tokens) <= 5:
        return None
    for token in tokens:
        if not token or not any(_unicode_kind(char) == "L" for char in token):
            return None
        if any(
            _unicode_kind(char) not in {"L", "M"} and char not in _NAME_PUNCTUATION
            for char in token
        ):
            return None
    if len(tokens) >= 2 and all(
        _unicode_kind(char) in {"L", "M"} or char in ".'-"
        for token in tokens
        for char in token
    ):
        return "name-label-v1"
    return "name-label-unicode-v2"


def _valid_unicode_email(local: str, domain: str) -> bool:
    if (
        not local
        or not domain
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
    ):
        return False
    if any(
        _unicode_kind(char) not in {"L", "M", "N"} and char not in _EMAIL_LOCAL_ASCII
        for char in local
    ):
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if (
            not label
            or label.startswith("-")
            or label.endswith("-")
            or any(
                _unicode_kind(char) not in {"L", "M", "N"} and char != "-"
                for char in label
            )
        ):
            return False
    return any(_unicode_kind(char) == "L" for char in labels[-1])


def _unicode_email_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for marker in re.finditer("@", text):
        start = marker.start()
        while start > 0:
            char = text[start - 1]
            if (
                _unicode_kind(char) not in {"L", "M", "N"}
                and char not in _EMAIL_LOCAL_ASCII
            ):
                break
            start -= 1
        end = marker.end()
        while end < len(text):
            char = text[end]
            if _unicode_kind(char) not in {"L", "M", "N"} and char not in {"-", "."}:
                break
            end += 1
        local, domain = text[start : marker.start()], text[marker.end() : end]
        candidate = text[start:end]
        if any(ord(char) > 127 for char in candidate) and _valid_unicode_email(
            local, domain
        ):
            ranges.append((start, end))
    return ranges


def _trimmed_value(match: re.Match[str]) -> tuple[str, int, int]:
    captured = match.group(1)
    value = captured.strip()
    leading = len(captured) - len(captured.lstrip())
    start = match.start(1) + leading
    return value, start, start + len(value)


def detect_pii(
    text: str, *, max_findings: int = MAX_FINDINGS
) -> tuple[PiiFinding, ...]:
    """Find synthetic-safe name, email, and phone patterns deterministically."""

    if not isinstance(text, str):
        raise TypeError("PII detector input must be text")
    raw = text.encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        raise CoordinateError("PII detector input exceeds bounded size")
    if not isinstance(max_findings, int) or not 1 <= max_findings <= MAX_FINDINGS:
        raise ValueError("max_findings is outside the supported bound")
    matches: list[tuple[str, int, int, str, float | None, Disposition]] = []
    for match in _EMAIL.finditer(text):
        matches.append(
            ("email", match.start(), match.end(), "email-v1", 1.0, "replace")
        )
        if len(matches) > max_findings:
            raise CoordinateError("PII detector finding limit exceeded")
    for start, end in _unicode_email_ranges(text):
        matches.append(("email", start, end, "email-unicode-v2", 1.0, "replace"))
        if len(matches) > max_findings:
            raise CoordinateError("PII detector finding limit exceeded")
    for match in _PHONE.finditer(text):
        candidate = match.group(0)
        digits = sum(char.isdigit() for char in candidate)
        if 7 <= digits <= 15:
            matches.append(
                ("phone", match.start(), match.end(), "phone-v1", 1.0, "replace")
            )
            if len(matches) > max_findings:
                raise CoordinateError("PII detector finding limit exceeded")
    # The name detector is deliberately label-bound; it does not guess names
    # from arbitrary prose or maintain a personal-name dictionary.
    for match in _NAME_LABEL.finditer(text):
        value, value_start, value_end = _trimmed_value(match)
        rule = _valid_name_value(value)
        if rule is not None:
            matches.append(("name", value_start, value_end, rule, 1.0, "replace"))
        elif value:
            matches.append(
                (
                    "name",
                    value_start,
                    value_end,
                    "name-label-ambiguous-v2",
                    None,
                    "review",
                )
            )
        if len(matches) > max_findings:
            raise CoordinateError("PII detector finding limit exceeded")
    supported_email_ranges = {
        (start, end) for start, end in _unicode_email_ranges(text)
    }
    supported_email_ranges.update(
        (match.start(), match.end()) for match in _EMAIL.finditer(text)
    )
    for match in _EMAIL_LABEL.finditer(text):
        value, value_start, value_end = _trimmed_value(match)
        if value and (value_start, value_end) not in supported_email_ranges:
            matches.append(
                (
                    "email",
                    value_start,
                    value_end,
                    "email-label-ambiguous-v2",
                    None,
                    "review",
                )
            )
            if len(matches) > max_findings:
                raise CoordinateError("PII detector finding limit exceeded")
    boundaries = {
        position for _, start, end, _, _, _ in matches for position in (start, end)
    }
    offsets = _byte_boundaries(text, boundaries)
    findings = [
        _finding(offsets, category, start, end, rule, confidence, disposition)
        for category, start, end, rule, confidence, disposition in matches
    ]
    unique = {(f.category, f.raw_start_byte, f.raw_end_byte): f for f in findings}
    ordered = sorted(
        unique.values(),
        key=lambda f: (f.raw_start_byte, f.raw_end_byte, f.category, f.rule_identity),
    )
    if len(ordered) > max_findings:
        raise CoordinateError("PII detector finding limit exceeded")
    return tuple(ordered)


def _as_finding(item: PiiFinding | ResumePiiFinding) -> PiiFinding:
    if isinstance(item, PiiFinding):
        return item
    return PiiFinding(
        item.category,
        item.raw_start_byte,
        item.raw_end_byte,
        item.rule_identity,
        item.confidence,
        item.disposition,
    )


def resolve_sanitization(
    raw_text: str,
    findings: Iterable[PiiFinding | ResumePiiFinding],
    *,
    sanitization_run_id: str,
    dispositions: Mapping[int, Disposition] | None = None,
    replacements: Mapping[int, str] | None = None,
) -> SanitizationResult:
    """Resolve overlapping findings into a complete immutable transform map.

    ``keep`` is the only disposition that retains source text.  Pending and
    review findings fail closed to replacement.  ``replacements`` is keyed by
    detector-finding ordinal and is intended for local review overrides.
    """

    if not sanitization_run_id:
        raise ValueError("sanitization run identity is required")
    raw = raw_text.encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        raise CoordinateError("sanitization input exceeds bounded size")
    supplied = tuple(_as_finding(item) for item in findings)
    if len(supplied) > MAX_FINDINGS:
        raise CoordinateError("sanitization finding limit exceeded")
    valid_ordinals = set(range(len(supplied)))
    for overrides, label in (
        (dispositions or {}, "disposition"),
        (replacements or {}, "replacement"),
    ):
        if any(
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal not in valid_ordinals
            for ordinal in overrides
        ):
            raise ValueError(f"unknown sanitization {label} ordinal")
    if replacements is not None and any(
        not isinstance(replacement, str) for replacement in replacements.values()
    ):
        raise ValueError("sanitization replacement must be text")
    ordered = tuple(
        sorted(
            enumerate(supplied),
            key=lambda pair: (pair[1].raw_start_byte, pair[1].raw_end_byte, pair[0]),
        )
    )
    for _, finding in ordered:
        finding_range = ByteRange(finding.raw_start_byte, finding.raw_end_byte)
        finding_range.validate(raw, allow_empty=False)
    effective: list[tuple[int, PiiFinding, Disposition]] = []
    for original, finding in ordered:
        disposition = (dispositions or {}).get(original, finding.disposition)
        if disposition not in {"pending", "replace", "keep", "review"}:
            raise ValueError("invalid sanitization disposition")
        if disposition != "keep":
            effective.append((original, finding, disposition))
    # Collapse each strictly connected overlap component.  Internal finding
    # boundaries must not split a replacement: one component has one complete
    # source range and one persisted transformation.  Touching ranges remain
    # independent components under half-open semantics.
    groups: list[tuple[int, int, tuple[tuple[int, PiiFinding, Disposition], ...]]] = []
    for item in effective:
        start, end = item[1].raw_start_byte, item[1].raw_end_byte
        if groups and start < groups[-1][1]:
            previous_start, previous_end, members = groups[-1]
            groups[-1] = (previous_start, max(previous_end, end), (*members, item))
        else:
            groups.append((start, end, (item,)))
    output = bytearray()
    span_records: list[TransformationSpan] = []
    span_for_finding: dict[int, int] = {}
    operations: list[
        tuple[str, int, int, tuple[tuple[int, PiiFinding, Disposition], ...]]
    ] = []
    cursor = 0
    for start, end, members in groups:
        if cursor < start:
            operations.append(("copy", cursor, start, ()))
        operations.append(("transform", start, end, members))
        cursor = end
    if cursor < len(raw):
        operations.append(("copy", cursor, len(raw), ()))
    for operation, start, end, members in operations:
        kind: Literal["copy", "replace", "delete"]
        source = raw[start:end]
        if operation == "copy":
            replacement = source
            kind = "copy"
            key = None
        else:
            # Earliest detector finding owns the stable category/key; overlap
            # still resolves to one shared transformation.
            owner_index, owner, _ = members[0]
            explicit_values = {
                replacements[index]
                for index, _, _ in members
                if replacements is not None and index in replacements
            }
            if len(explicit_values) > 1:
                raise ValueError("conflicting replacements overlap one PII group")
            replacement_text = next(iter(explicit_values), None)
            if replacement_text is None:
                replacement_text = f"[PII:{owner.category}]"
            replacement = replacement_text.encode("utf-8")
            kind = "delete" if replacement_text == "" else "replace"
            if kind == "delete":
                replacement = b""
            key = f"pii-{owner.category}-{owner_index}" if kind == "replace" else None
            for index, _, _ in members:
                span_for_finding[index] = len(span_records)
        clean_start = len(output)
        if len(output) + len(replacement) > MAX_TEXT_BYTES:
            raise CoordinateError("sanitized text exceeds bounded size")
        output.extend(replacement)
        span_records.append(
            TransformationSpan(
                len(span_records),
                kind,
                start,
                end,
                clean_start,
                len(output),
                sha256_slice(raw, start, end),
                hashlib.sha256(replacement).hexdigest(),
                key,
            )
        )
    clean = bytes(output)
    mapping = TransformationMap.from_spans(
        span_records,
        raw_size=len(raw),
        sanitized_size=len(clean),
        raw_artifact=raw,
        sanitized_artifact=clean,
    )
    persisted_findings: list[ResumePiiFinding] = []
    for ordinal, (_, finding) in enumerate(ordered):
        disposition = (dispositions or {}).get(_, finding.disposition)
        persisted_findings.append(
            ResumePiiFinding(
                sanitization_run_id,
                ordinal,
                finding.category,
                "deterministic",
                finding.rule_identity,
                finding.confidence,
                disposition,
                finding.raw_start_byte,
                finding.raw_end_byte,
                None if disposition == "keep" else span_for_finding.get(_),
            )
        )
    persisted_spans = tuple(
        SanitizationSpan(
            sanitization_run_id,
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
        for span in span_records
    )
    return SanitizationResult(
        clean.decode("utf-8"), mapping, persisted_spans, tuple(persisted_findings)
    )


# Short aliases make the pure engine convenient without changing frozen models.
find_pii = detect_pii
sanitize = resolve_sanitization
