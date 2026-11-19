"""Frozen, deterministic M5 career-fact extraction.

The engine is intentionally structural.  It accepts the exact successful M3
text artifact and never consults sanitization, a model, or the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

from .coordinates import sha256_slice
from .models import CareerFactCandidate, CareerFactEvidence, CareerFactRun
from .pii import detect_pii

FACT_SCHEMA_VERSION = "careeros-career-fact-schema-v1"
FACT_SCHEMA_FINGERPRINT = (
    "f49c008962f1591f498708825f664d13434600a0a079c0557bc075e0217f9e3f"
)
DETERMINISTIC_EXTRACTOR_V1_VERSION = "careeros-m5-deterministic-career-facts-v1"
DETERMINISTIC_EXTRACTOR_V2_VERSION = "careeros-m5-deterministic-career-facts-v2"
DETERMINISTIC_EXTRACTOR_VERSION = "careeros-m5-deterministic-career-facts-v3"
DETERMINISTIC_EXTRACTOR_V1_FINGERPRINT = (
    "0882205196f757b28c54d6d7ce27824cf052b7b65c9642009def87b062be3e04"
)
DETERMINISTIC_EXTRACTOR_FINGERPRINT = hashlib.sha256(
    DETERMINISTIC_EXTRACTOR_VERSION.encode()
).hexdigest()
DETERMINISTIC_EXTRACTOR_V2_FINGERPRINT = hashlib.sha256(
    DETERMINISTIC_EXTRACTOR_V2_VERSION.encode()
).hexdigest()
INPUT_MAX_BYTES = 4 * 1024 * 1024
MAX_FIELD_BYTES = 512
MAX_NORMALIZED_KEY_LENGTH = 512
MAX_CANONICAL_VALUE_LENGTH = 100_000
MAX_CANDIDATES = 1000
MAX_EVIDENCE_PER_CANDIDATE = 1000
MAX_EVIDENCE_TOTAL = 10000
TYPE_LIMITS = {
    "identity.name": 8,
    "contact.email": 32,
    "contact.phone": 32,
    "portfolio_reference": 64,
    "skill": 256,
    "employment": 128,
    "education": 64,
    "certification": 128,
    "language": 64,
}
FACT_TYPES = tuple(TYPE_LIMITS)
FACT_TYPE_ORDER = (
    "identity.name",
    "contact.email",
    "contact.phone",
    "portfolio_reference",
    "skill",
    "employment",
    "education",
    "certification",
    "language",
)
_EDIT_FIELDS = {
    "identity.name": {"name"},
    "contact.email": {"email"},
    "contact.phone": {"phone"},
    "portfolio_reference": {"url"},
    "skill": {"name"},
    "employment": {"title", "company", "start_date", "end_date"},
    "education": {"degree", "institution", "start_date", "end_date"},
    "certification": {"name", "issuer", "date"},
    "language": {"name"},
}
_EDIT_REQUIRED_FIELDS = {
    "identity.name": {"name"},
    "contact.email": {"email"},
    "contact.phone": {"phone"},
    "portfolio_reference": {"url"},
    "skill": {"name"},
    "employment": {"title", "company"},
    "education": {"degree", "institution"},
    "certification": {"name"},
    "language": {"name"},
}
_NAME_PUNCTUATION = frozenset(".'’ʼ-‐‑‒–")
_EMAIL_LOCAL_ASCII = frozenset(".!#$%&'*+/=?^_`{|}~-")
_CANONICAL_SEPARATORS = (",", ":")
_DATE = r"(?:\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?)"
_HEADINGS = {
    "skills": "skill",
    "technical skills": "skill",
    "core skills": "skill",
    "languages": "language",
    "certifications": "certification",
    "certificates": "certification",
}
_LIST_HEADING = re.compile(
    r"(?i)^\s*(?P<heading>skills|technical\s+skills|core\s+skills|languages|"
    r"certifications|certificates)\s*(?:(?P<separator>[:=-])\s*(?P<value>.*))?$"
)
_ANY_LABEL = re.compile(r"(?i)^\s*\w[^:\n]{0,47}\s*[:=]\s*[^\r\n]+$")
_NAME_LABEL = re.compile(
    r"(?im)^\s*(?:full\s+name|preferred\s+name|candidate|name)\s*[:=-]\s*([^\r\n]+)"
)
_EMAIL_LABEL = re.compile(r"(?im)^\s*(?:e-?mail)\s*[:=-]\s*([^\r\n]+)")
_PHONE_LABEL = re.compile(r"(?im)^\s*(?:phone|mobile|telephone)\s*[:=-]\s*([^\r\n]+)")
_PORTFOLIO_LABEL = re.compile(
    r"(?im)^\s*(?:portfolio|website|personal\s+site|github)\s*[:=-]\s*([^\r\n]+)"
)
_SECTION = re.compile(
    r"(?im)^\s*(experience|employment|work\s+experience|professional\s+experience|"
    r"work\s+history|employment\s+history|education)\s*:?\s*$"
)
_SECTION_V1 = re.compile(
    r"(?im)^\s*(experience|employment|work\s+experience|education)\s*:?\s*$"
)
_BOUNDARY_HEADING = re.compile(
    r"(?i)^\s*(?:contact(?:\s+details)?|summary|professional\s+summary|"
    r"projects?|selected\s+projects|professional\s+experience|work\s+history|"
    r"employment\s+history|references|awards?|publications?|interests?|"
    r"volunteering|volunteer(?:ing)?)\s*:?\s*$"
)
_PROFICIENCY = re.compile(
    r"(?i)^(?:native|fluent|proficient|professional|conversational|basic|"
    r"beginner|intermediate|advanced)(?:\s+(?:working|level|fluency))?\.?$"
)
_CEFR = re.compile(r"(?i)\b(?:A1|A2|B1|B2|C1|C2)\b")


def _v2_annotation(value: str) -> bool:
    clean = value.strip()
    if _PROFICIENCY.fullmatch(clean):
        return True
    if re.fullmatch(r"(?i)(?:A1|A2|B1|B2|C1|C2)", clean):
        return True
    if not _CEFR.search(clean):
        return False
    return bool(
        re.fullmatch(
            r"(?i)[^()\n]+\b(?:A1|A2|B1|B2|C1|C2)\b(?:\s*\((?:Distinction|Befriedigend)\))?",
            clean,
        )
    )


def _v2_annotation_continuation(value: str) -> bool:
    clean = value.strip()
    if re.match(r"(?:[-*•·]|[oO]\s+)", clean):
        return False
    if re.search(r"\s+(?:\u2014|\u2013|-)\s+|:", clean):
        return False
    if _PROFICIENCY.fullmatch(clean) or re.fullmatch(
        r"(?i)(?:A1|A2|B1|B2|C1|C2)", clean
    ):
        return True
    if not re.search(
        r"(?i)\b(?:proficiency|zertifikat|certificate|qualification|esol)\b",
        clean,
    ):
        return False
    return _v2_annotation(clean)


class CareerFactError(ValueError):
    """Fail-closed M5 extraction or aggregate error."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
        allow_nan=False,
    )


def _k(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"[\s\-_,;:|/]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _email_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _phone_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    plus = value.startswith("+")
    digits = "".join(char for char in value if char.isdigit())
    return ("+" if plus else "") + digits


def _date(value: str) -> str | None:
    match = re.fullmatch(_DATE, value.strip())
    if not match:
        return None
    return match.group(0)


def _url(value: str) -> str | None:
    value = value.strip()
    match = re.fullmatch(r"(?i)https?://[^\s<>\"']+", value)
    if not match:
        return None
    try:
        parts = urlsplit(value)
        host_value = parts.hostname
        username = parts.username
        password = parts.password
        port = parts.port
    except ValueError:
        return None
    if not host_value or username is not None or password is not None:
        return None
    host = host_value.casefold()
    netloc = host
    if port is not None and not (
        (parts.scheme.casefold() == "http" and port == 80)
        or (parts.scheme.casefold() == "https" and port == 443)
    ):
        netloc += f":{port}"
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit(
        (
            parts.scheme.casefold(),
            netloc,
            path,
            parts.query,
            "",
        )
    )


def _validate_display_fields(occurrence: _Occurrence) -> None:
    for value in occurrence.value.values():
        if not isinstance(value, str) or not value:
            raise CareerFactError("M5 candidate field is invalid")
        if len(value.encode("utf-8")) > MAX_FIELD_BYTES:
            raise CareerFactError("M5 candidate field bound exceeded")


def _byte_offsets(text: str) -> array[int]:
    offsets = array("I", [0])
    for char in text:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    return offsets


@dataclass(frozen=True)
class _Occurrence:
    fact_type: str
    value: dict[str, Any]
    spans: tuple[tuple[int, int], ...]


def _line_spans(text: str) -> Iterable[tuple[str, int, int]]:
    cursor = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        yield body, cursor, cursor + len(body)
        cursor += len(line)
    if not text or text.endswith(("\n", "\r")):
        return


def _trim_span(value: str, start: int) -> tuple[str, int, int]:
    left = len(value) - len(value.lstrip())
    clean = value.strip()
    return clean, start + left, start + left + len(clean)


def _split_list_items(value: str, start: int) -> list[tuple[str, int, int]]:
    items: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[^,;|•·]+", value):
        raw = match.group(0)
        leading = len(raw) - len(raw.lstrip())
        clean = raw.strip()
        item_start = start + match.start() + leading
        if clean.startswith(("-", "*")):
            decorated = clean[1:]
            decoration_space = len(decorated) - len(decorated.lstrip())
            clean = decorated.strip()
            item_start += 1 + decoration_space
        if clean:
            items.append((clean, item_start, item_start + len(clean)))
    return items


def _is_recognized_heading(line: str) -> bool:
    return any(
        pattern.fullmatch(line) is not None
        for pattern in (
            _LIST_HEADING,
            _SECTION,
            _ANY_LABEL,
            _NAME_LABEL,
            _EMAIL_LABEL,
            _PHONE_LABEL,
            _PORTFOLIO_LABEL,
            _BOUNDARY_HEADING,
        )
    )


def _is_recognized_heading_v1(line: str) -> bool:
    return any(
        pattern.fullmatch(line) is not None
        for pattern in (
            _LIST_HEADING,
            _SECTION_V1,
            _ANY_LABEL,
            _NAME_LABEL,
            _EMAIL_LABEL,
            _PHONE_LABEL,
            _PORTFOLIO_LABEL,
        )
    )


def _label_occurrences(
    text: str, offsets: array[int], findings: tuple[Any, ...]
) -> list[_Occurrence]:
    occurrences: list[_Occurrence] = []
    for match in _NAME_LABEL.finditer(text):
        value, start, end = _trim_span(match.group(1), match.start(1))
        finding = next(
            (
                f
                for f in findings
                if f.category == "name"
                and f.raw_start_byte == offsets[start]
                and f.raw_end_byte == offsets[end]
            ),
            None,
        )
        if value and finding is not None and finding.disposition == "replace":
            occurrences.append(
                _Occurrence(
                    "identity.name", {"name": value}, ((offsets[start], offsets[end]),)
                )
            )
    supported_emails = {
        (f.raw_start_byte, f.raw_end_byte)
        for f in findings
        if f.category == "email" and f.disposition == "replace"
    }
    for match in _EMAIL_LABEL.finditer(text):
        value, start, end = _trim_span(match.group(1), match.start(1))
        span = (offsets[start], offsets[end])
        if value and span in supported_emails:
            occurrences.append(_Occurrence("contact.email", {"email": value}, (span,)))
    for pattern, fact_type, field in ((_PHONE_LABEL, "contact.phone", "phone"),):
        for match in pattern.finditer(text):
            value, start, end = _trim_span(match.group(1), match.start(1))
            span = (offsets[start], offsets[end])
            if value and any(
                f.category == "phone"
                and f.raw_start_byte == span[0]
                and f.raw_end_byte == span[1]
                for f in findings
            ):
                occurrences.append(_Occurrence(fact_type, {field: value}, (span,)))
    for match in _PORTFOLIO_LABEL.finditer(text):
        value, start, end = _trim_span(match.group(1), match.start(1))
        url = _url(value)
        if url is not None:
            occurrences.append(
                _Occurrence(
                    "portfolio_reference",
                    {"url": value},
                    ((offsets[start], offsets[end]),),
                )
            )
    return occurrences


def _list_occurrences_v1(text: str, offsets: array[int]) -> list[_Occurrence]:
    out: list[_Occurrence] = []
    lines = list(_line_spans(text))
    for index, (line, start, _end) in enumerate(lines):
        heading_match = _LIST_HEADING.fullmatch(line)
        if heading_match is None:
            continue
        heading = _HEADINGS[_k(heading_match.group("heading"))]
        if heading == "certification":
            continue
        item_lines: list[tuple[str, int]] = []
        first = heading_match.group("value") or ""
        first_start = (
            start + heading_match.start("value") if first else start + len(line)
        )
        if first.strip():
            item_lines.append((first, first_start))
        for next_line, next_start, _ in lines[index + 1 :]:
            if _is_recognized_heading_v1(next_line):
                break
            if not next_line.strip():
                break
            item_lines.append((next_line, next_start))
        for item, item_start in item_lines:
            for clean, p0, p1 in _split_list_items(item, item_start):
                if heading == "language" and re.search(r"[()[\]:]", clean):
                    continue
                out.append(
                    _Occurrence(heading, {"name": clean}, ((offsets[p0], offsets[p1]),))
                )
    return out


def _decorated_item(
    value: str, start: int, *, allow_letter_bullet: bool = False
) -> tuple[str, int, int]:
    """Remove only unmistakable list decoration, retaining O-prefixed names."""
    clean, p0, p1 = _trim_span(value, start)
    match = re.match(r"[oO](?=\s+)", clean) if allow_letter_bullet else None
    if match:
        clean, p0, p1 = _trim_span(clean[match.end() :], p0 + match.end())
    elif clean.startswith(("-", "*", "•", "·")):
        clean, p0, p1 = _trim_span(clean[1:], p0 + 1)
    return clean, p0, p1


def _overlaps_pii(
    start: int, end: int, findings: tuple[Any, ...], offsets: array[int]
) -> bool:
    return any(
        finding.category in {"email", "phone"}
        and finding.raw_start_byte < offsets[end]
        and offsets[start] < finding.raw_end_byte
        for finding in findings
    )


def _language_item(
    value: str, start: int, *, allow_letter_bullet: bool = False
) -> tuple[str, int, int] | None:
    clean, p0, p1 = _decorated_item(
        value, start, allow_letter_bullet=allow_letter_bullet
    )
    if any(marker in clean for marker in "[]"):
        return None
    # A descriptor alone is never a language.
    if _PROFICIENCY.fullmatch(clean) or re.fullmatch(
        r"(?i)(?:A1|A2|B1|B2|C1|C2|Proficiency|Distinction|Befriedigend)", clean
    ):
        return None
    # Split only explicit proficiency delimiters. A colon is also accepted.
    match = re.match(r"^(.+?)\s*(?::|\u2014|\u2013|\s+-\s+)\s*(.+?)\s*$", clean)
    if match and _v2_annotation(match.group(2)):
        name = match.group(1).strip()
        name_offset = p0 + len(match.group(1)) - len(match.group(1).lstrip())
        return name, name_offset, name_offset + len(name)
    if match:
        return None
    if _v2_annotation(clean):
        return None
    if re.search(r"\s+-\S|\S-\s+", clean):
        return None
    return clean, p0, p1


def _list_occurrences_v2(
    text: str,
    offsets: array[int],
    findings: tuple[Any, ...],
    *,
    assemble_wrapped_skills: bool = False,
) -> list[_Occurrence]:
    out: list[_Occurrence] = []
    lines = list(_line_spans(text))
    for index, (line, start, _end) in enumerate(lines):
        heading_match = _LIST_HEADING.fullmatch(line)
        if heading_match is None:
            continue
        heading = _HEADINGS[_k(heading_match.group("heading"))]
        if heading == "certification":
            continue
        entries: list[tuple[str, int]] = []
        first = heading_match.group("value") or ""
        if first.strip():
            entries.append((first, start + heading_match.start("value")))
        cursor = index + 1
        while cursor < len(lines):
            next_line, next_start, _ = lines[cursor]
            if heading == "language":
                is_boundary = (
                    _LIST_HEADING.fullmatch(next_line) is not None
                    or _SECTION.fullmatch(next_line) is not None
                    or _BOUNDARY_HEADING.fullmatch(next_line) is not None
                )
                if _ANY_LABEL.fullmatch(next_line) is not None:
                    label_value = next_line.split(":", 1)[-1].strip()
                    is_language_annotation = _v2_annotation(label_value)
                    is_boundary = is_boundary or not is_language_annotation
            else:
                is_boundary = _is_recognized_heading(next_line)
            if not next_line.strip() or is_boundary:
                break
            entries.append((next_line, next_start))
            cursor += 1
        if heading == "language":
            for entry_index, (entry, entry_start) in enumerate(entries):
                letter_bullet = bool(re.match(r"\s*[oO]\s+", entry))
                annotation_delimiter = bool(
                    re.search(r"\s+(?:\u2014|\u2013|-)\s+", entry)
                    or (":" in entry and "(" not in entry.split(":", 1)[0])
                )
                delimiter_match = re.search(r"\s+(?:\u2014|\u2013|-)\s+|:", entry)
                left_has_parenthesis = bool(
                    delimiter_match and "(" in entry[: delimiter_match.start()]
                )
                if (
                    "(" in entry and (not annotation_delimiter or left_has_parenthesis)
                ) or "[" in entry:
                    continue
                # Comma-separated shared descriptor is unambiguous.
                shared = re.match(r"^\s*(.+?),\s*(.+?)\s*:\s*(.+?)\s*$", entry)
                if shared and _v2_annotation(shared.group(3)):
                    names = [shared.group(1), *shared.group(2).split(",")]
                    parts = []
                    search_at = 0
                    for name in names:
                        clean_name = name.strip()
                        local = entry.find(clean_name, search_at)
                        parts.append(
                            (
                                clean_name,
                                entry_start + local,
                                entry_start + local + len(clean_name),
                            )
                        )
                        search_at = local + len(clean_name)
                else:
                    parts = _split_list_items(entry, entry_start)
                for raw, p0, _p1 in parts:
                    if raw.rstrip().endswith(":"):
                        following = (
                            entries[entry_index + 1][0].strip()
                            if entry_index + 1 < len(entries)
                            else ""
                        )
                        if not _v2_annotation_continuation(following):
                            continue
                        raw = raw.rstrip()[:-1].rstrip()
                    item = _language_item(raw, p0, allow_letter_bullet=letter_bullet)
                    if item is None:
                        continue
                    name, n0, n1 = item
                    if not name or _overlaps_pii(n0, n1, findings, offsets):
                        continue
                    out.append(
                        _Occurrence(
                            "language", {"name": name}, ((offsets[n0], offsets[n1]),)
                        )
                    )
                # A following indented descriptor belongs to the prior bullet;
                # it is consumed structurally and never emitted.
            continue
        inline_value = bool(first.strip())
        entry_index = 0
        while entry_index < len(entries):
            entry, entry_start = entries[entry_index]
            letter_bullet = bool(re.match(r"\s*[oO]\s+", entry))
            explicit_item_structure = bool(
                re.match(r"\s*(?:[-*•·])", entry)
                or re.match(r"\s*[oO]\s+", entry)
                or re.search(r"[,;|•·]", entry)
            )
            item_parts = _split_list_items(entry, entry_start)
            wrapped = False
            suppress_ambiguous = False
            continuation: tuple[str, int, int] | None = None
            bullet_match = re.match(r"^(?P<indent>\s*)(?:[-*•·]|[oO])\s+", entry)
            bullet_indent = (
                len(bullet_match.group("indent")) if bullet_match is not None else None
            )
            if (
                assemble_wrapped_skills
                and bullet_indent is not None
                and len(item_parts) == 1
            ):
                next_index = entry_index + 1
                if next_index < len(entries):
                    next_entry, next_start = entries[next_index]
                    next_indent = len(next_entry) - len(next_entry.lstrip())
                    next_is_bullet = bool(
                        re.match(r"\s*(?:[-*•·]|[oO])\s+", next_entry)
                    )
                    clean_next = next_entry.strip()
                    continuation_start = next_start + (
                        len(next_entry) - len(next_entry.lstrip())
                    )
                    continuation_end = next_start + len(next_entry.rstrip())
                    safe_continuation = (
                        not next_is_bullet
                        and not _is_recognized_heading(next_entry)
                        and next_indent > bullet_indent
                        and bool(clean_next)
                        and len(clean_next.split()) == 1
                        and not re.search(r"[,;|•·:]", clean_next)
                        and not clean_next.endswith((".", ";", "!", "?"))
                        and not _overlaps_pii(
                            continuation_start,
                            continuation_end,
                            findings,
                            offsets,
                        )
                    )
                    potential_continuation = (
                        not next_is_bullet and next_indent > bullet_indent
                    )
                    enclosed = next_index + 1 == len(entries) or bool(
                        re.match(r"\s*(?:[-*•·]|[oO])\s+", entries[next_index + 1][0])
                    )
                    if safe_continuation and enclosed:
                        continuation = (
                            clean_next,
                            continuation_start,
                            continuation_end,
                        )
                        wrapped = True
                    elif potential_continuation:
                        # A visually wrapped bullet that cannot be interpreted
                        # conservatively is omitted as one unit.  Do not leak
                        # either fragment as an independent skill.
                        suppress_ambiguous = True
            for raw, p0, _p1 in item_parts:
                if suppress_ambiguous:
                    break
                clean, n0, n1 = _decorated_item(
                    raw, p0, allow_letter_bullet=letter_bullet
                )
                if not clean or _overlaps_pii(n0, n1, findings, offsets):
                    continue
                # Keep genuine multiword names, but omit obvious prose.
                if (
                    not explicit_item_structure
                    and not inline_value
                    and len(clean.split()) > 1
                ):
                    continue
                if len(clean.split()) > 10 or clean.endswith((".", ";")):
                    continue
                spans = [(offsets[n0], offsets[n1])]
                if wrapped and continuation is not None:
                    continuation_text, continuation_start, continuation_end = (
                        continuation
                    )
                    clean = f"{clean} {continuation_text}"
                    spans.append(
                        (offsets[continuation_start], offsets[continuation_end])
                    )
                out.append(_Occurrence("skill", {"name": clean}, tuple(spans)))
            advance = 2 if (wrapped or suppress_ambiguous) else 1
            if suppress_ambiguous and bullet_indent is not None:
                while entry_index + advance < len(entries):
                    following = entries[entry_index + advance][0]
                    following_indent = len(following) - len(following.lstrip())
                    if following_indent <= bullet_indent:
                        break
                    if re.match(r"\s*(?:[-*•·]|[oO])\s+", following):
                        break
                    advance += 1
            entry_index += advance
            inline_value = False
    return out


def _composite_occurrences(
    text: str, offsets: array[int], *, v1: bool = False
) -> list[_Occurrence]:
    out: list[_Occurrence] = []
    lines = list(_line_spans(text))
    section: str | None = None
    for line, start, _end in lines:
        match = (_SECTION_V1 if v1 else _SECTION).match(line)
        if match:
            section = (
                "employment"
                if _k(match.group(1))
                in {
                    "experience",
                    "employment",
                    "work experience",
                    "professional experience",
                    "work history",
                    "employment history",
                }
                else "education"
            )
            continue
        if (_is_recognized_heading_v1 if v1 else _is_recognized_heading)(line):
            section = None
            continue
        if (
            section is None
            or not line.strip()
            or line.lstrip().startswith(("-", "•")) is False
        ):
            continue
        value = line.lstrip(" -*•\t").strip()
        value_start = start + line.find(value)
        date_range = re.search(
            rf"\s+(?:\||—|–|-)\s*(?P<start>{_DATE})"
            rf"(?:\s*[-–—]\s*(?P<end>{_DATE}))?\s*$",
            value,
        )
        date_start = [
            (value_start + date_range.start(group), value_start + date_range.end(group))
            for group in ("start", "end")
            if date_range is not None and date_range.group(group) is not None
        ]
        body = value
        if date_range:
            body = value[: date_range.start()].strip()
        pieces = re.split(r"\s+(?:at|\||—|–)\s+", body, maxsplit=1)
        if len(pieces) != 2 or not all(pieces):
            continue
        left, right = (p.strip() for p in pieces)
        if re.search(r"\s+(?:\||—|–)\s+", right) or re.search(rf"\b{_DATE}\b", right):
            continue
        title_local = line.find(left)
        company_local = line.find(right, title_local + len(left))
        if title_local < 0 or company_local < 0:
            continue
        title_start = start + title_local
        company_start = start + company_local
        val: dict[str, Any] = {
            "title" if section == "employment" else "degree": left,
            "company" if section == "employment" else "institution": right,
        }
        spans = [
            (offsets[title_start], offsets[title_start + len(left)]),
            (offsets[company_start], offsets[company_start + len(right)]),
        ]
        if date_range:
            val["start_date"] = _date(date_range.group("start"))
            if date_range.group("end") is not None:
                val["end_date"] = _date(date_range.group("end"))
            spans.extend((offsets[a], offsets[b]) for a, b in date_start)
        out.append(_Occurrence(section, val, tuple(spans)))
    return out


def _certification_occurrences(
    text: str, offsets: array[int], *, v1: bool = False
) -> list[_Occurrence]:
    lines = list(_line_spans(text))
    out: list[_Occurrence] = []
    for index, (line, line_start, _end) in enumerate(lines):
        heading_match = _LIST_HEADING.fullmatch(line)
        if (
            heading_match is None
            or _HEADINGS[_k(heading_match.group("heading"))] != "certification"
        ):
            continue
        item_lines: list[tuple[str, int]] = []
        first = heading_match.group("value") or ""
        if first.strip():
            item_lines.append((first, line_start + heading_match.start("value")))
        for next_line, next_start, _ in lines[index + 1 :]:
            if not next_line.strip() or (
                _is_recognized_heading_v1 if v1 else _is_recognized_heading
            )(next_line):
                break
            item_lines.append((next_line, next_start))
        for item, item_start in item_lines:
            for value, value_start, _value_end in _split_list_items(item, item_start):
                _append_certification(out, value, value_start, offsets)
    return out


def _append_certification(
    output: list[_Occurrence],
    value: str,
    value_start: int,
    offsets: array[int],
) -> None:
    date_match = re.search(rf"(?:\s+(?:—|–|-)\s*)?(?P<date>{_DATE})\s*$", value)
    delimiter = re.search(r"\s+(?:—|–|at)\s+", value)
    name_end = len(value)
    issuer: str | None = None
    if delimiter:
        name_end = delimiter.start()
        issuer = value[delimiter.end() :]
    if date_match:
        if date_match.start("date") < name_end:
            name_end = date_match.start()
        date_value = date_match.group("date")
        if issuer and date_value in issuer:
            issuer = issuer[: issuer.find(date_value)].strip(" ,—–-")
    name = value[:name_end].strip(" ,—–-")
    if not name:
        return
    semantic: dict[str, Any] = {"name": name}
    spans = [(offsets[value_start], offsets[value_start + len(name)])]
    if issuer and issuer.strip():
        issuer = issuer.strip(" ,—–-")
        semantic["issuer"] = issuer
        issuer_start = value_start + value.find(issuer, len(name))
        spans.append((offsets[issuer_start], offsets[issuer_start + len(issuer)]))
    if date_match:
        semantic["date"] = date_match.group("date")
        spans.append(
            (
                offsets[value_start + date_match.start("date")],
                offsets[value_start + date_match.end("date")],
            )
        )
    output.append(_Occurrence("certification", semantic, tuple(spans)))


def _canonical_semantic(fact_type: str, value: dict[str, Any]) -> dict[str, Any]:
    semantic: dict[str, Any] = {}
    for field, display in value.items():
        if display is None:
            continue
        normalized: str | None
        if field == "email":
            normalized = _email_key(display)
        elif field == "phone":
            normalized = _phone_key(display)
        elif field == "url":
            normalized = _url(display)
        elif field.endswith("date") or (
            fact_type == "certification" and field == "date"
        ):
            normalized = _date(display)
        else:
            normalized = _k(display)
        if not normalized:
            raise CareerFactError("M5 candidate normalization failed closed")
        semantic[field] = normalized
    return semantic


def _normalized_key(fact_type: str, semantic: dict[str, Any]) -> str:
    key = f"{fact_type}:{canonical_json(semantic)}"
    if not 1 <= len(key) <= MAX_NORMALIZED_KEY_LENGTH:
        raise CareerFactError("M5 normalized key bound exceeded")
    return key


def validate_candidate_value(fact_type: str, value_json: str) -> tuple[str, str, str]:
    """Validate an M5-shaped edited value without structural discovery.

    This is the small public seam used by M6.  It applies the same field,
    date, URL, and normalization rules as deterministic M5 extraction, but
    never inspects résumé text or evidence.
    """
    if fact_type not in _EDIT_FIELDS:
        raise CareerFactError("unknown M5 fact type")
    if not isinstance(value_json, str):
        raise CareerFactError("edited value must be JSON text")

    def reject_constant(value: str) -> NoReturn:
        raise ValueError(value)

    try:
        parsed = json.loads(value_json, parse_constant=reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CareerFactError("edited value is not valid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) - _EDIT_FIELDS[fact_type]:
        raise CareerFactError("edited value has the wrong M5 shape")
    if set(parsed) != {key for key, value in parsed.items() if value is not None}:
        raise CareerFactError("edited value contains null fields")
    if not _EDIT_REQUIRED_FIELDS[fact_type].issubset(parsed):
        raise CareerFactError("edited value has the wrong M5 shape")
    for field, value in parsed.items():
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or "\n" in value
            or "\r" in value
        ):
            raise CareerFactError("edited value field is invalid")
        if len(value.encode("utf-8")) > MAX_FIELD_BYTES:
            raise CareerFactError("edited value field bound exceeded")
        if field.endswith("date") or (fact_type == "certification" and field == "date"):
            if _date(value) is None:
                raise CareerFactError("edited value date is invalid")
        elif field == "email":
            local, separator, domain = value.rpartition("@")
            labels = domain.split(".")
            if (
                not separator
                or not local
                or not domain
                or local.startswith(".")
                or local.endswith(".")
                or ".." in local
                or any(
                    unicodedata.category(char)[0] not in {"L", "M", "N"}
                    and char not in _EMAIL_LOCAL_ASCII
                    for char in local
                )
                or len(labels) < 2
                or any(
                    not label
                    or label.startswith("-")
                    or label.endswith("-")
                    or any(
                        unicodedata.category(char)[0] not in {"L", "M", "N"}
                        and char != "-"
                        for char in label
                    )
                    for label in labels
                )
                or not any(unicodedata.category(char)[0] == "L" for char in labels[-1])
            ):
                raise CareerFactError("edited value email is invalid")
        elif field == "phone":
            digits = sum(char.isdigit() for char in value)
            if not re.fullmatch(r"\+?\d[\d .()/-]*\d", value) or not 7 <= digits <= 15:
                raise CareerFactError("edited value phone is invalid")
        elif field == "name" and fact_type == "identity.name":
            tokens = re.split(r"[ \t]+", value)
            if not 1 <= len(tokens) <= 5 or any(
                not token
                or not any(unicodedata.category(char)[0] == "L" for char in token)
                or any(
                    unicodedata.category(char)[0] not in {"L", "M"}
                    and char not in _NAME_PUNCTUATION
                    for char in token
                )
                for token in tokens
            ):
                raise CareerFactError("edited value name is invalid")
        elif field == "url" and _url(value) is None:
            raise CareerFactError("edited value URL is invalid")
    if "end_date" in parsed and "start_date" not in parsed:
        raise CareerFactError("end_date requires start_date")
    canonical = canonical_json(parsed)
    if canonical != value_json:
        raise CareerFactError("edited value is not canonical JSON")
    if not 1 <= len(canonical) <= MAX_CANONICAL_VALUE_LENGTH:
        raise CareerFactError("edited value canonical bound exceeded")
    semantic = _canonical_semantic(fact_type, parsed)
    key = _normalized_key(fact_type, semantic)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest(), key


def _candidate_id(
    extraction_run_id: str,
    fact_type: str,
    key: str,
    semantic: dict[str, Any],
    input_size: int,
    input_sha256: str,
    extractor_fingerprint: str = DETERMINISTIC_EXTRACTOR_FINGERPRINT,
) -> str:
    identity = {
        "extraction_run_id": extraction_run_id,
        "extractor_fingerprint": extractor_fingerprint,
        "fact_type": fact_type,
        "input_byte_size": input_size,
        "input_sha256": input_sha256,
        "mode": "deterministic",
        "normalized_key": key,
        "schema_fingerprint": FACT_SCHEMA_FINGERPRINT,
        "semantic_value": semantic,
        "version": 1,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:32]


def extract_deterministic_facts(
    raw_text: str,
    extraction_run_id: str,
    *,
    fact_run_id: str | None = None,
    created_at: str | None = None,
    extractor_fingerprint: str = DETERMINISTIC_EXTRACTOR_FINGERPRINT,
) -> tuple[
    CareerFactRun, tuple[CareerFactCandidate, ...], tuple[CareerFactEvidence, ...]
]:
    if not isinstance(raw_text, str) or not extraction_run_id:
        raise CareerFactError("M5 requires extracted text and extraction run ID")
    artifact = raw_text.encode("utf-8")
    if len(artifact) > INPUT_MAX_BYTES:
        raise CareerFactError("M5 input exceeds bound")
    input_sha = hashlib.sha256(artifact).hexdigest()
    run_id = fact_run_id or uuid.uuid4().hex
    now = created_at or datetime.now(UTC).isoformat()
    cache_identity = {
        "extraction_run_id": extraction_run_id,
        "input_sha256": input_sha,
        "input_byte_size": len(artifact),
        "input_kind": "extracted",
        "mode": "deterministic",
        "extractor_fingerprint": extractor_fingerprint,
        "schema_fingerprint": FACT_SCHEMA_FINGERPRINT,
        "version": 1,
    }
    cache = hashlib.sha256(canonical_json(cache_identity).encode()).hexdigest()
    run = CareerFactRun(
        run_id,
        extraction_run_id,
        None,
        "extracted",
        "deterministic",
        "succeeded",
        cache,
        input_sha,
        len(artifact),
        None,
        extractor_fingerprint,
        FACT_SCHEMA_FINGERPRINT,
        None,
        None,
        None,
        now,
    )
    offsets = _byte_offsets(raw_text)
    try:
        findings = detect_pii(raw_text)
    except (TypeError, ValueError) as exc:
        raise CareerFactError("M5 structural detector failed closed") from exc
    if extractor_fingerprint not in {
        DETERMINISTIC_EXTRACTOR_V1_FINGERPRINT,
        DETERMINISTIC_EXTRACTOR_V2_FINGERPRINT,
        DETERMINISTIC_EXTRACTOR_FINGERPRINT,
    }:
        raise CareerFactError("unknown M5 extractor fingerprint")
    legacy = extractor_fingerprint == DETERMINISTIC_EXTRACTOR_V1_FINGERPRINT
    current_v3 = extractor_fingerprint == DETERMINISTIC_EXTRACTOR_FINGERPRINT
    occurrences = (
        _label_occurrences(raw_text, offsets, findings)
        + (
            _list_occurrences_v1(raw_text, offsets)
            if legacy
            else _list_occurrences_v2(
                raw_text,
                offsets,
                findings,
                assemble_wrapped_skills=current_v3,
            )
        )
        + _composite_occurrences(raw_text, offsets, v1=legacy)
    )
    occurrences.extend(_certification_occurrences(raw_text, offsets, v1=legacy))
    grouped: dict[tuple[str, str], list[_Occurrence]] = {}
    for occurrence in occurrences:
        _validate_display_fields(occurrence)
        semantic = _canonical_semantic(occurrence.fact_type, occurrence.value)
        key = _normalized_key(occurrence.fact_type, semantic)
        grouped.setdefault((occurrence.fact_type, key), []).append(occurrence)
    if len(grouped) > MAX_CANDIDATES or any(
        sum(1 for key in grouped if key[0] == fact_type) > limit
        for fact_type, limit in TYPE_LIMITS.items()
    ):
        raise CareerFactError("M5 candidate bound exceeded")
    candidates: list[CareerFactCandidate] = []
    evidence: list[CareerFactEvidence] = []
    for (fact_type, key), items in grouped.items():
        items.sort(key=lambda item: min(item.spans))
        semantic = _canonical_semantic(fact_type, items[0].value)
        candidate_id = _candidate_id(
            extraction_run_id,
            fact_type,
            key,
            semantic,
            len(artifact),
            input_sha,
            extractor_fingerprint,
        )
        display_value = {
            key: value for key, value in items[0].value.items() if value is not None
        }
        value_json = canonical_json(display_value)
        if not 1 <= len(value_json) <= MAX_CANONICAL_VALUE_LENGTH:
            raise CareerFactError("M5 canonical value bound exceeded")
        candidates.append(
            CareerFactCandidate(
                candidate_id,
                run_id,
                fact_type,
                key,
                value_json,
                hashlib.sha256(value_json.encode()).hexdigest(),
                1.0,
                "deterministic",
            )
        )
        spans = sorted(
            {
                (a, b, sha256_slice(artifact, a, b))
                for item in items
                for a, b in item.spans
            }
        )
        if len(spans) > MAX_EVIDENCE_PER_CANDIDATE:
            raise CareerFactError("M5 evidence bound exceeded")
        evidence.extend(
            CareerFactEvidence(candidate_id, ordinal, "extracted", a, b, digest)
            for ordinal, (a, b, digest) in enumerate(spans)
        )
    candidates.sort(
        key=lambda item: (
            FACT_TYPE_ORDER.index(item.fact_type),
            item.normalized_key,
            item.canonical_value_json,
            item.id,
        )
    )
    evidence.sort(key=lambda item: (item.candidate_id, item.ordinal))
    if len(evidence) > MAX_EVIDENCE_TOTAL:
        raise CareerFactError("M5 evidence bound exceeded")
    return run, tuple(candidates), tuple(evidence)


deterministic_extract = extract_deterministic_facts
extract_career_facts = extract_deterministic_facts


def extract_deterministic_facts_v1(
    raw_text: str,
    extraction_run_id: str,
    *,
    fact_run_id: str | None = None,
    created_at: str | None = None,
) -> tuple[
    CareerFactRun, tuple[CareerFactCandidate, ...], tuple[CareerFactEvidence, ...]
]:
    return extract_deterministic_facts(
        raw_text,
        extraction_run_id,
        fact_run_id=fact_run_id,
        created_at=created_at,
        extractor_fingerprint=DETERMINISTIC_EXTRACTOR_V1_FINGERPRINT,
    )


def extract_deterministic_facts_v2(
    raw_text: str,
    extraction_run_id: str,
    *,
    fact_run_id: str | None = None,
    created_at: str | None = None,
) -> tuple[
    CareerFactRun, tuple[CareerFactCandidate, ...], tuple[CareerFactEvidence, ...]
]:
    return extract_deterministic_facts(
        raw_text,
        extraction_run_id,
        fact_run_id=fact_run_id,
        created_at=created_at,
        extractor_fingerprint=DETERMINISTIC_EXTRACTOR_V2_FINGERPRINT,
    )


def run_deterministic_facts(
    connection: Any, extraction_run_id: str, *, created_at: str | None = None
) -> tuple[
    CareerFactRun, tuple[CareerFactCandidate, ...], tuple[CareerFactEvidence, ...]
]:
    """Execute M5 against one successful M3 run with cache revalidation.

    The repository interface is ``get_extraction``, ``find_successful_fact_runs``,
    ``load_fact_aggregate``, and ``persist_fact_run``.  Persistence is the sole
    owner of transactions; a concurrent identical winner is recovered by a
    second complete cache scan.
    """
    from careeros.database import resume_intelligence as repository

    extraction = repository.get_extraction(connection, extraction_run_id)
    if extraction.outcome != "succeeded" or extraction.raw_text is None:
        raise CareerFactError("M5 requires a successful extraction")
    raw = extraction.raw_text
    raw_bytes = raw.encode("utf-8")
    input_sha = hashlib.sha256(raw_bytes).hexdigest()
    cache_identity = {
        "extraction_run_id": extraction_run_id,
        "input_sha256": input_sha,
        "input_byte_size": len(raw_bytes),
        "input_kind": "extracted",
        "mode": "deterministic",
        "extractor_fingerprint": DETERMINISTIC_EXTRACTOR_FINGERPRINT,
        "schema_fingerprint": FACT_SCHEMA_FINGERPRINT,
        "version": 1,
    }
    cache = hashlib.sha256(canonical_json(cache_identity).encode()).hexdigest()

    def winner() -> (
        tuple[
            CareerFactRun,
            tuple[CareerFactCandidate, ...],
            tuple[CareerFactEvidence, ...],
        ]
        | None
    ):
        for candidate_run in repository.find_successful_fact_runs(
            connection, cache, limit=1000
        ):
            try:
                loaded = repository.load_fact_aggregate(
                    connection,
                    candidate_run.id,
                    candidate_limit=MAX_CANDIDATES,
                    evidence_per_candidate_limit=MAX_EVIDENCE_PER_CANDIDATE,
                    evidence_total_limit=MAX_EVIDENCE_TOTAL,
                )
                loaded_run, loaded_candidates, loaded_evidence = loaded
                expected = extract_deterministic_facts(
                    raw,
                    extraction_run_id,
                    fact_run_id=loaded_run.id,
                    created_at=loaded_run.created_at,
                )
                expected = (
                    expected[0],
                    tuple(sorted(expected[1], key=lambda item: item.id)),
                    tuple(
                        sorted(
                            expected[2],
                            key=lambda item: (item.candidate_id, item.ordinal),
                        )
                    ),
                )
                if loaded == expected:
                    return (
                        loaded_run,
                        tuple(
                            sorted(
                                loaded_candidates,
                                key=lambda item: (
                                    FACT_TYPE_ORDER.index(item.fact_type),
                                    item.normalized_key,
                                    item.canonical_value_json,
                                    item.id,
                                ),
                            )
                        ),
                        loaded_evidence,
                    )
            except (
                repository.ResumeRepositoryError,
                CareerFactError,
                ValueError,
                KeyError,
                TypeError,
            ):
                continue
        return None

    cached = winner()
    if cached is not None:
        return cached
    try:
        result = extract_deterministic_facts(
            raw, extraction_run_id, created_at=created_at
        )
    except CareerFactError:
        failed = CareerFactRun(
            uuid.uuid4().hex,
            extraction_run_id,
            None,
            "extracted",
            "deterministic",
            "failed",
            cache,
            input_sha,
            len(raw_bytes),
            None,
            DETERMINISTIC_EXTRACTOR_FINGERPRINT,
            FACT_SCHEMA_FINGERPRINT,
            None,
            None,
            None,
            created_at or datetime.now(UTC).isoformat(),
        )
        repository.persist_fact_run(connection, failed, (), ())
        raise
    try:
        repository.persist_fact_run(
            connection, *result, reject_existing_successful_cache=True
        )
    except repository.ResumeConflictError:
        # An identical concurrent winner is authoritative; all other failures
        # remain failures and are not silently converted into cache hits.
        recovered = winner()
        if recovered is not None:
            return recovered
        raise
    return result


__all__ = [
    "FACT_SCHEMA_VERSION",
    "FACT_SCHEMA_FINGERPRINT",
    "DETERMINISTIC_EXTRACTOR_VERSION",
    "DETERMINISTIC_EXTRACTOR_FINGERPRINT",
    "DETERMINISTIC_EXTRACTOR_V2_VERSION",
    "DETERMINISTIC_EXTRACTOR_V2_FINGERPRINT",
    "DETERMINISTIC_EXTRACTOR_V1_VERSION",
    "DETERMINISTIC_EXTRACTOR_V1_FINGERPRINT",
    "CareerFactError",
    "canonical_json",
    "extract_deterministic_facts",
    "extract_deterministic_facts_v1",
    "extract_deterministic_facts_v2",
    "deterministic_extract",
    "extract_career_facts",
    "run_deterministic_facts",
]
