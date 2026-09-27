"""Immutable persistence primitives for resume intelligence artifacts.

This module deliberately accepts already-computed artifacts.  It never reads
material files, invokes parsers/models, or performs external work.
"""

# SQL statements are intentionally kept readable.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, cast

from careeros.resume_intelligence.career_facts import (
    DETERMINISTIC_EXTRACTOR_FINGERPRINT,
    FACT_SCHEMA_FINGERPRINT,
    FACT_TYPES,
    TYPE_LIMITS,
    canonical_json,
    extract_deterministic_facts,
)
from careeros.resume_intelligence.coordinates import (
    TransformationMap,
    TransformationSpan,
    validate_range,
    validate_slice_hash,
)
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
from careeros.resume_intelligence.pii import (
    Disposition,
    PiiFinding,
    SanitizationResult,
    detect_pii,
    resolve_sanitization,
)
from careeros.resume_intelligence.sanitization_identity import (
    DEFAULT_POLICY_FINGERPRINT,
    SANITIZER_FINGERPRINT,
    override_policy_fingerprint,
    parse_override_manifest,
    sanitization_cache_fingerprint,
)

MAX_SANITIZATION_LINEAGE_DEPTH = 64
_CANONICAL_REVIEW_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$"
)


@dataclass(frozen=True)
class FactReviewRecord:
    """M6 review read including the database event identity."""

    id: int
    candidate_id: str
    event_kind: str
    created_at: str
    edited_value_json: str | None = None
    edited_value_sha256: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class FactAuthority:
    """Complete, transient M5 authority materialization and its digest."""

    run: CareerFactRun
    candidates: tuple[CareerFactCandidate, ...]
    evidence: tuple[CareerFactEvidence, ...]
    extraction_run_id: str
    extraction_raw_sha256: str
    extraction_raw_byte_size: int
    digest: str


def validate_recognized_fact_authority(
    c: sqlite3.Connection,
    run_id: str,
    *,
    candidate_limit: int = 1000,
    evidence_per_candidate_limit: int = 1000,
    evidence_total_limit: int = 10000,
) -> FactAuthority:
    """Fully validate a persisted deterministic M5 aggregate before a lock.

    This is deliberately separate from :func:`load_fact_authority`: the
    latter is safe inside the short profile write transaction and only
    re-materializes persisted authority, while this pre-lock operation may
    rerun the frozen deterministic extractor against the verified raw text.
    """
    authority = load_fact_authority(
        c,
        run_id,
        candidate_limit=candidate_limit,
        evidence_per_candidate_limit=evidence_per_candidate_limit,
        evidence_total_limit=evidence_total_limit,
        require_recognized=True,
    )
    expected_run, expected_candidates, expected_evidence = extract_deterministic_facts(
        c.execute(
            "SELECT raw_text FROM resume_extraction_runs WHERE id=?",
            (authority.extraction_run_id,),
        ).fetchone()[0],
        authority.extraction_run_id,
        fact_run_id=authority.run.id,
        created_at=authority.run.created_at,
    )
    expected_candidates = tuple(sorted(expected_candidates, key=lambda item: item.id))
    expected_evidence = tuple(
        sorted(expected_evidence, key=lambda item: (item.candidate_id, item.ordinal))
    )
    if (
        expected_run != authority.run
        or expected_candidates != authority.candidates
        or expected_evidence != authority.evidence
    ):
        raise ResumeValidationError(
            "persisted fact aggregate differs from M5 authority"
        )
    return authority


def validate_review_timestamp(value: str) -> str:
    """Require the exact UTC microsecond timestamp emitted by M6 services."""
    if (
        not isinstance(value, str)
        or _CANONICAL_REVIEW_TIMESTAMP.fullmatch(value) is None
    ):
        raise ResumeValidationError("review timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ResumeValidationError("review timestamp is invalid") from exc
    if parsed.tzinfo != UTC or parsed.isoformat(timespec="microseconds") != value:
        raise ResumeValidationError("review timestamp is not canonical UTC")
    return value


class ResumeRepositoryError(Exception):
    """Safe base class for repository failures."""


class ResumeNotFoundError(ResumeRepositoryError):
    pass


class ResumeValidationError(ResumeRepositoryError):
    pass


class ResumeConflictError(ResumeRepositoryError):
    pass


def _limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= 1000:
        raise ResumeValidationError("limit must be between 1 and 1000")
    return limit


@contextmanager
def _write(c: sqlite3.Connection) -> Iterator[None]:
    try:
        c.execute("BEGIN IMMEDIATE")
        yield
    except ResumeRepositoryError:
        c.rollback()
        raise
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        c.rollback()
        raise ResumeValidationError("repository operation failed") from exc
    except BaseException:
        c.rollback()
        raise
    else:
        c.commit()


def _row(c: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> sqlite3.Row | None:
    return cast(sqlite3.Row | None, c.execute(sql, args).fetchone())


def _require(row: sqlite3.Row | None, label: str) -> sqlite3.Row:
    if row is None:
        raise ResumeNotFoundError(f"{label} not found")
    return row


def _raw(run: sqlite3.Row) -> bytes:
    text = run["raw_text"]
    if text is None:
        raise ResumeValidationError("successful extraction artifact is unavailable")
    return str(text).encode("utf-8")


def _sanitized(run: sqlite3.Row) -> bytes:
    text = run["sanitized_text"]
    if text is None:
        raise ResumeValidationError("successful sanitization artifact is unavailable")
    return str(text).encode("utf-8")


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _canonical_json(value: str) -> tuple[str, str]:
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ResumeValidationError("canonical value is not valid JSON") from exc
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extraction_from(r: sqlite3.Row) -> ResumeExtractionRun:
    return ResumeExtractionRun(**dict(r))


def _segment_from(r: sqlite3.Row) -> ResumeTextSegment:
    return ResumeTextSegment(**dict(r))


def persist_extraction(
    c: sqlite3.Connection,
    run: ResumeExtractionRun,
    segments: Iterable[ResumeTextSegment] = (),
) -> ResumeExtractionRun:
    """Atomically persist one extraction run and its ordered text segments."""
    supplied = tuple(segments)
    with _write(c):
        material = _require(
            _row(
                c,
                "SELECT * FROM material_versions WHERE id=?",
                (run.material_version_id,),
            ),
            "material version",
        )
        if (run.source_byte_size, run.source_sha256) != (
            material["byte_size"],
            material["sha256"],
        ):
            raise ResumeValidationError(
                "extraction source metadata does not match material version"
            )
        if (run.source_name, run.source_media_type) != (
            material["display_filename"],
            material["declared_media_type"],
        ):
            raise ResumeValidationError(
                "extraction source metadata does not match material version"
            )
        if run.segment_count != len(supplied) or tuple(
            s.ordinal for s in supplied
        ) != tuple(range(len(supplied))):
            raise ResumeValidationError(
                "extraction segments must match contiguous segment count"
            )
        raw = run.raw_text.encode("utf-8") if run.raw_text is not None else None
        if run.outcome == "succeeded":
            if raw is None:
                raise ResumeValidationError("successful extraction requires raw text")
            if run.raw_text_sha256 != hashlib.sha256(raw).hexdigest():
                raise ResumeValidationError("raw text SHA-256 does not match artifact")
        elif supplied:
            raise ResumeValidationError("failed extraction cannot contain segments")
        if raw is not None:
            for segment in supplied:
                if segment.extraction_run_id != run.id:
                    raise ResumeValidationError("segment belongs to another extraction")
                validate_slice_hash(
                    raw,
                    segment.raw_start_byte,
                    segment.raw_end_byte,
                    segment.slice_sha256,
                )
            if (
                _row(
                    c,
                    "SELECT id FROM resume_extraction_runs WHERE cache_fingerprint=? AND outcome='succeeded'",
                    (run.cache_fingerprint,),
                )
                is not None
            ):
                raise ResumeConflictError(
                    "successful extraction cache identity already exists"
                )
        try:
            c.execute(
                """INSERT INTO resume_extraction_runs
                (id,material_version_id,source_name,source_media_type,source_byte_size,
                 source_sha256,source_format,detected_format,outcome,raw_text,
                 raw_text_sha256,segment_count,extractor_fingerprint,cache_fingerprint,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(run.__dict__.values()),
            )
            c.executemany(
                "INSERT INTO resume_text_segments VALUES (?,?,?,?,?,?)",
                [tuple(s.__dict__.values()) for s in supplied],
            )
        except sqlite3.IntegrityError as exc:
            raise ResumeValidationError("extraction aggregate is invalid") from exc
    return run


def get_extraction(c: sqlite3.Connection, run_id: str) -> ResumeExtractionRun:
    return _extraction_from(
        _require(
            _row(c, "SELECT * FROM resume_extraction_runs WHERE id=?", (run_id,)),
            "extraction run",
        )
    )


def list_extractions(
    c: sqlite3.Connection, material_version_id: str, *, limit: int = 100
) -> list[ResumeExtractionRun]:
    _limit(limit)
    rows = c.execute(
        "SELECT * FROM resume_extraction_runs WHERE material_version_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
        (material_version_id, limit),
    ).fetchall()
    return [_extraction_from(r) for r in rows]


def find_successful_extraction(
    c: sqlite3.Connection, cache_fingerprint: str
) -> ResumeExtractionRun | None:
    r = _row(
        c,
        "SELECT * FROM resume_extraction_runs WHERE cache_fingerprint=? AND outcome='succeeded'",
        (cache_fingerprint,),
    )
    return None if r is None else _extraction_from(r)


def extraction_segments(
    c: sqlite3.Connection, run_id: str, *, limit: int = 1000
) -> list[ResumeTextSegment]:
    _limit(limit)
    _require(
        _row(c, "SELECT id FROM resume_extraction_runs WHERE id=?", (run_id,)),
        "extraction run",
    )
    return [
        _segment_from(r)
        for r in c.execute(
            "SELECT * FROM resume_text_segments WHERE extraction_run_id=? ORDER BY ordinal LIMIT ?",
            (run_id, limit),
        ).fetchall()
    ]


def persist_sanitization(
    c: sqlite3.Connection,
    run: ResumeSanitizationRun,
    spans: Iterable[SanitizationSpan] = (),
    findings: Iterable[ResumePiiFinding] = (),
) -> ResumeSanitizationRun:
    supplied_spans, supplied_findings = tuple(spans), tuple(findings)
    canonical_manifest, _ = _canonical_json(run.override_manifest_json)
    if canonical_manifest != run.override_manifest_json:
        raise ResumeValidationError("override manifest is not canonical JSON")
    with _write(c):
        extraction = _require(
            _row(
                c,
                "SELECT * FROM resume_extraction_runs WHERE id=?",
                (run.extraction_run_id,),
            ),
            "extraction run",
        )
        if extraction["outcome"] != "succeeded":
            raise ResumeValidationError("sanitization requires a successful extraction")
        if run.parent_sanitization_run_id is not None:
            if run.parent_sanitization_run_id == run.id:
                raise ResumeValidationError("sanitization run cannot parent itself")
            if run.sanitizer_fingerprint == SANITIZER_FINGERPRINT:
                parent = validate_sanitization_authority(
                    c, run.parent_sanitization_run_id
                )
            else:
                parent = validate_sanitization(c, run.parent_sanitization_run_id)
            if parent.extraction_run_id != run.extraction_run_id:
                raise ResumeValidationError(
                    "parent sanitization belongs to another extraction"
                )
            if parent.outcome != "succeeded":
                raise ResumeValidationError("parent sanitization must have succeeded")
            if (
                _sanitization_lineage_length(c, run.parent_sanitization_run_id)
                >= MAX_SANITIZATION_LINEAGE_DEPTH
            ):
                raise ResumeValidationError(
                    "sanitization lineage exceeds bounded depth"
                )
        raw = _raw(extraction)
        # The extraction row is the sole coordinate authority.  Re-check its
        # immutable artifact before accepting a sanitization aggregate; callers
        # must never be able to bind spans to a later/current extraction.
        if extraction["raw_text_sha256"] != hashlib.sha256(raw).hexdigest():
            raise ResumeValidationError("extraction artifact integrity failure")
        _validate_m4_sanitization_identity(run, raw)
        clean = (
            run.sanitized_text.encode("utf-8")
            if run.sanitized_text is not None
            else None
        )
        if run.outcome == "succeeded":
            if clean is None:
                raise ResumeValidationError(
                    "successful sanitization requires sanitized text"
                )
            if run.sanitized_text_sha256 != hashlib.sha256(clean).hexdigest():
                raise ResumeValidationError(
                    "sanitized text SHA-256 does not match artifact"
                )
            tm = TransformationMap.from_spans(
                tuple(
                    TransformationSpan(
                        s.ordinal,
                        s.kind,
                        s.raw_start_byte,
                        s.raw_end_byte,
                        s.sanitized_start_byte,
                        s.sanitized_end_byte,
                        s.raw_slice_sha256,
                        s.sanitized_slice_sha256,
                        s.placeholder_key,
                    )
                    for s in supplied_spans
                ),
                raw_size=len(raw),
                sanitized_size=len(clean),
                raw_artifact=raw,
                sanitized_artifact=clean,
            )
            del tm
            if tuple(s.ordinal for s in supplied_spans) != tuple(
                range(len(supplied_spans))
            ):
                raise ResumeValidationError("sanitization spans must be contiguous")
            if tuple(f.ordinal for f in supplied_findings) != tuple(
                range(len(supplied_findings))
            ):
                raise ResumeValidationError("PII findings must be contiguous")
            for span in supplied_spans:
                if span.sanitization_run_id != run.id:
                    raise ResumeValidationError("span belongs to another sanitization")
            for finding in supplied_findings:
                if finding.sanitization_run_id != run.id:
                    raise ResumeValidationError(
                        "finding belongs to another sanitization"
                    )
                validate_range(
                    raw, finding.raw_start_byte, finding.raw_end_byte, allow_empty=False
                )
                if (
                    finding.sanitization_span_ordinal is not None
                    and finding.sanitization_span_ordinal >= len(supplied_spans)
                ):
                    raise ResumeValidationError(
                        "finding references an unknown sanitization span"
                    )
                if finding.sanitization_span_ordinal is not None:
                    span = supplied_spans[finding.sanitization_span_ordinal]
                    if finding.disposition == "keep":
                        raise ResumeValidationError(
                            "retained finding cannot reference a sanitization span"
                        )
                    if span.kind not in ("replace", "delete"):
                        raise ResumeValidationError(
                            "finding reference must target a replacement span"
                        )
                    if not (
                        finding.raw_start_byte < span.raw_end_byte
                        and span.raw_start_byte < finding.raw_end_byte
                    ):
                        raise ResumeValidationError(
                            "finding does not overlap its resolved sanitization span"
                        )
                elif finding.disposition == "replace":
                    raise ResumeValidationError(
                        "replacement finding must reference a span"
                    )
            if run.sanitizer_fingerprint == SANITIZER_FINGERPRINT:
                expected = _reconstruct_current_sanitization(
                    c, run, str(extraction["raw_text"])
                )
                _require_exact_sanitization_aggregate(
                    run, supplied_spans, supplied_findings, expected
                )
        elif supplied_spans or supplied_findings:
            raise ResumeValidationError("failed sanitization cannot contain artifacts")
        if (
            run.outcome == "succeeded"
            and _row(
                c,
                "SELECT id FROM resume_sanitization_runs WHERE cache_fingerprint=? AND outcome='succeeded'",
                (run.cache_fingerprint,),
            )
            is not None
        ):
            raise ResumeConflictError(
                "successful sanitization cache identity already exists"
            )
        try:
            c.execute(
                """INSERT INTO resume_sanitization_runs
                (id,extraction_run_id,parent_sanitization_run_id,sanitized_text,
                 sanitized_text_sha256,sanitizer_fingerprint,policy_fingerprint,
                 override_manifest_json,cache_fingerprint,outcome,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(run.__dict__.values()),
            )
            c.executemany(
                "INSERT INTO resume_sanitization_spans VALUES (?,?,?,?,?,?,?,?,?,?)",
                [tuple(s.__dict__.values()) for s in supplied_spans],
            )
            c.executemany(
                """INSERT INTO resume_pii_findings
                (sanitization_run_id,ordinal,category,rule_identity,confidence,
                 disposition,origin,raw_start_byte,raw_end_byte,sanitization_span_ordinal)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        f.sanitization_run_id,
                        f.ordinal,
                        f.category,
                        f.rule_identity,
                        f.confidence,
                        f.disposition,
                        f.origin,
                        f.raw_start_byte,
                        f.raw_end_byte,
                        f.sanitization_span_ordinal,
                    )
                    for f in supplied_findings
                ],
            )
        except sqlite3.IntegrityError as exc:
            raise ResumeValidationError("sanitization aggregate is invalid") from exc
    return run


def _sanitization_lineage_length(c: sqlite3.Connection, run_id: str) -> int:
    length = 0
    current_id: str | None = run_id
    while current_id is not None:
        length += 1
        row = _require(
            _row(
                c,
                "SELECT parent_sanitization_run_id FROM resume_sanitization_runs "
                "WHERE id=?",
                (current_id,),
            ),
            "sanitization run",
        )
        current_id = cast(str | None, row["parent_sanitization_run_id"])
    return length


def _validate_m4_sanitization_identity(run: ResumeSanitizationRun, raw: bytes) -> None:
    if run.sanitizer_fingerprint != SANITIZER_FINGERPRINT:
        return
    if run.parent_sanitization_run_id is None:
        if run.override_manifest_json != "{}":
            raise ResumeValidationError(
                "root sanitization cannot contain override provenance"
            )
        expected_policy = DEFAULT_POLICY_FINGERPRINT
    else:
        try:
            parse_override_manifest(run.override_manifest_json)
        except ValueError as exc:
            raise ResumeValidationError(
                "override manifest provenance is invalid"
            ) from exc
        expected_policy = override_policy_fingerprint(run.override_manifest_json)
    if run.policy_fingerprint != expected_policy:
        raise ResumeValidationError("sanitization policy identity is invalid")
    expected_cache = sanitization_cache_fingerprint(
        extraction_run_id=run.extraction_run_id,
        raw_text_sha256=hashlib.sha256(raw).hexdigest(),
        raw_text_byte_size=len(raw),
        policy_fingerprint=run.policy_fingerprint,
        parent_sanitization_run_id=run.parent_sanitization_run_id,
        override_manifest_json=run.override_manifest_json,
    )
    if run.cache_fingerprint != expected_cache:
        raise ResumeValidationError("sanitization cache identity is invalid")


def _reconstruct_current_sanitization(
    c: sqlite3.Connection, run: ResumeSanitizationRun, raw_text: str
) -> SanitizationResult:
    """Reconstruct the complete v2 authority from immutable inputs."""

    if run.sanitizer_fingerprint != SANITIZER_FINGERPRINT:
        raise ResumeValidationError("unsupported sanitization integrity authority")
    if run.parent_sanitization_run_id is None:
        findings = detect_pii(raw_text)
        dispositions: dict[int, Disposition] = {}
        replacements: dict[int, str] = {}
    else:
        parent_run = get_sanitization(c, run.parent_sanitization_run_id)
        if parent_run.sanitizer_fingerprint != SANITIZER_FINGERPRINT:
            raise ResumeValidationError(
                "unsupported parent sanitization integrity authority"
            )
        parent_findings = sanitization_findings(
            c, run.parent_sanitization_run_id, limit=1000
        )
        findings = tuple(
            PiiFinding(
                finding.category,
                finding.raw_start_byte,
                finding.raw_end_byte,
                finding.rule_identity,
                finding.confidence,
                finding.disposition,
            )
            for finding in parent_findings
        )
        try:
            actions = parse_override_manifest(run.override_manifest_json)
        except ValueError as exc:
            raise ResumeValidationError(
                "override manifest provenance is invalid"
            ) from exc
        dispositions = {}
        replacements = {}
        for entry in actions:
            ordinal = entry["finding_ordinal"]
            if ordinal >= len(findings):
                raise ResumeValidationError(
                    "override action references an unknown finding"
                )
            action = entry["action"]
            if action == "delete":
                dispositions[ordinal] = "replace"
                replacements[ordinal] = ""
            else:
                dispositions[ordinal] = action
                replacement = entry.get("replacement")
                if replacement is not None:
                    replacements[ordinal] = replacement
    try:
        return resolve_sanitization(
            raw_text,
            findings,
            sanitization_run_id=run.id,
            dispositions=dispositions,
            replacements=replacements,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ResumeValidationError(
            "sanitization authority reconstruction failed"
        ) from exc


def _require_exact_sanitization_aggregate(
    run: ResumeSanitizationRun,
    spans: tuple[SanitizationSpan, ...],
    findings: tuple[ResumePiiFinding, ...],
    expected: SanitizationResult,
) -> None:
    if (
        run.sanitized_text != expected.sanitized_text
        or spans != expected.spans
        or findings != expected.findings
    ):
        raise ResumeValidationError(
            "sanitization aggregate does not match reconstructed authority"
        )


def _validate_one_sanitization(
    c: sqlite3.Connection, run_id: str
) -> ResumeSanitizationRun:
    try:
        run = get_sanitization(c, run_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ResumeValidationError("sanitization artifact integrity failure") from exc
    canonical_manifest, _ = _canonical_json(run.override_manifest_json)
    if canonical_manifest != run.override_manifest_json:
        raise ResumeValidationError("override manifest is not canonical JSON")
    extraction = _require(
        _row(
            c,
            "SELECT * FROM resume_extraction_runs WHERE id=?",
            (run.extraction_run_id,),
        ),
        "extraction run",
    )
    if extraction["outcome"] != "succeeded":
        raise ResumeValidationError("sanitization requires a successful extraction")
    raw = _raw(extraction)
    if extraction["raw_text_sha256"] != hashlib.sha256(raw).hexdigest():
        raise ResumeValidationError("extraction artifact integrity failure")
    _validate_m4_sanitization_identity(run, raw)
    span_count = int(
        c.execute(
            "SELECT count(*) FROM resume_sanitization_spans WHERE sanitization_run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    finding_count = int(
        c.execute(
            "SELECT count(*) FROM resume_pii_findings WHERE sanitization_run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    if span_count > 1000 or finding_count > 1000:
        raise ResumeValidationError("sanitization aggregate exceeds bounded size")
    if run.outcome != "succeeded":
        if span_count or finding_count:
            raise ResumeValidationError("failed sanitization contains artifacts")
        return run
    clean = _sanitized(
        _require(
            _row(c, "SELECT * FROM resume_sanitization_runs WHERE id=?", (run_id,)),
            "sanitization run",
        )
    )
    try:
        spans = sanitization_spans(c, run_id, limit=1000)
        TransformationMap.from_spans(
            tuple(
                TransformationSpan(
                    s.ordinal,
                    s.kind,
                    s.raw_start_byte,
                    s.raw_end_byte,
                    s.sanitized_start_byte,
                    s.sanitized_end_byte,
                    s.raw_slice_sha256,
                    s.sanitized_slice_sha256,
                    s.placeholder_key,
                )
                for s in spans
            ),
            raw_size=len(raw),
            sanitized_size=len(clean),
            raw_artifact=raw,
            sanitized_artifact=clean,
        )
        findings = sanitization_findings(c, run_id, limit=1000)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ResumeValidationError("sanitization artifact integrity failure") from exc
    if tuple(f.ordinal for f in findings) != tuple(range(len(findings))):
        raise ResumeValidationError("PII findings must be contiguous")
    for finding in findings:
        validate_range(
            raw, finding.raw_start_byte, finding.raw_end_byte, allow_empty=False
        )
        if finding.sanitization_span_ordinal is not None:
            if finding.sanitization_span_ordinal >= len(spans):
                raise ResumeValidationError(
                    "finding references an unknown sanitization span"
                )
            span = spans[finding.sanitization_span_ordinal]
            if not (
                finding.raw_start_byte < span.raw_end_byte
                and span.raw_start_byte < finding.raw_end_byte
            ):
                raise ResumeValidationError(
                    "finding does not overlap its resolved sanitization span"
                )
            if finding.disposition == "keep":
                raise ResumeValidationError("retained finding references a span")
            if spans[finding.sanitization_span_ordinal].kind not in (
                "replace",
                "delete",
            ):
                raise ResumeValidationError(
                    "replacement finding must reference a replacement span"
                )
        elif finding.disposition == "replace":
            raise ResumeValidationError("replacement finding must reference a span")
    return run


def validate_sanitization(c: sqlite3.Connection, run_id: str) -> ResumeSanitizationRun:
    """Validate one persisted aggregate and its bounded immutable ancestry.

    Stored artifacts and maps, never current sanitizer code, are historical
    authority.  Every parent is validated independently and lineage cycles or
    cross-extraction edges fail closed.
    """

    lineage_ids: list[str] = []
    current_id: str | None = run_id
    seen: set[str] = set()
    for depth in range(MAX_SANITIZATION_LINEAGE_DEPTH + 1):
        if current_id is None:
            break
        if depth == MAX_SANITIZATION_LINEAGE_DEPTH:
            raise ResumeValidationError("sanitization lineage exceeds bounded depth")
        if current_id in seen:
            raise ResumeValidationError("sanitization lineage contains a cycle")
        seen.add(current_id)
        lineage_ids.append(current_id)
        row = _require(
            _row(
                c,
                "SELECT parent_sanitization_run_id FROM resume_sanitization_runs "
                "WHERE id=?",
                (current_id,),
            ),
            "sanitization run",
        )
        current_id = cast(str | None, row["parent_sanitization_run_id"])
    else:
        raise AssertionError("unreachable")

    requested: ResumeSanitizationRun | None = None
    expected_extraction_id: str | None = None
    lineage: list[ResumeSanitizationRun] = []
    for current_id in lineage_ids:
        current = _validate_one_sanitization(c, current_id)
        lineage.append(current)
        if requested is None:
            requested = current
            expected_extraction_id = current.extraction_run_id
        else:
            if current.outcome != "succeeded":
                raise ResumeValidationError("parent sanitization must have succeeded")
            if current.extraction_run_id != expected_extraction_id:
                raise ResumeValidationError(
                    "parent sanitization belongs to another extraction"
                )
    assert requested is not None
    _validate_sanitization_lineage(c, lineage)
    for current in reversed(lineage):
        if (
            current.sanitizer_fingerprint != SANITIZER_FINGERPRINT
            or current.outcome != "succeeded"
        ):
            continue
        extraction = _require(
            _row(
                c,
                "SELECT raw_text FROM resume_extraction_runs WHERE id=?",
                (current.extraction_run_id,),
            ),
            "extraction run",
        )
        raw_text = extraction["raw_text"]
        if raw_text is None:
            raise ResumeValidationError("successful extraction artifact is unavailable")
        expected = _reconstruct_current_sanitization(c, current, str(raw_text))
        _require_exact_sanitization_aggregate(
            current,
            tuple(sanitization_spans(c, current.id, limit=1000)),
            tuple(sanitization_findings(c, current.id, limit=1000)),
            expected,
        )
    return requested


def validate_sanitization_authority(
    c: sqlite3.Connection, run_id: str
) -> ResumeSanitizationRun:
    """Validate an artifact under a recognized deterministic M4 authority."""

    run = validate_sanitization(c, run_id)
    if run.sanitizer_fingerprint != SANITIZER_FINGERPRINT:
        raise ResumeValidationError("unsupported sanitization integrity authority")
    return run


def _validate_sanitization_lineage(
    c: sqlite3.Connection, lineage: list[ResumeSanitizationRun]
) -> None:
    """Validate persisted child findings against canonical parent actions."""

    for child, parent in zip(lineage, lineage[1:], strict=False):
        if child.sanitizer_fingerprint != SANITIZER_FINGERPRINT:
            continue
        try:
            actions = parse_override_manifest(child.override_manifest_json)
        except ValueError as exc:
            raise ResumeValidationError(
                "override manifest provenance is invalid"
            ) from exc
        child_findings = sanitization_findings(c, child.id, limit=1000)
        parent_findings = sanitization_findings(c, parent.id, limit=1000)
        if len(child_findings) != len(parent_findings):
            raise ResumeValidationError("override finding lineage is incomplete")
        by_ordinal = {entry["finding_ordinal"]: entry for entry in actions}
        if any(ordinal >= len(parent_findings) for ordinal in by_ordinal):
            raise ResumeValidationError("override action references an unknown finding")
        child_spans = sanitization_spans(c, child.id, limit=1000)
        child_text = child.sanitized_text
        if child_text is None:
            raise ResumeValidationError(
                "successful sanitization artifact is unavailable"
            )
        child_bytes = child_text.encode("utf-8")
        for ordinal, (child_finding, parent_finding) in enumerate(
            zip(child_findings, parent_findings, strict=True)
        ):
            if (
                child_finding.ordinal != parent_finding.ordinal
                or child_finding.category != parent_finding.category
                or child_finding.origin != parent_finding.origin
                or child_finding.rule_identity != parent_finding.rule_identity
                or child_finding.confidence != parent_finding.confidence
                or child_finding.raw_start_byte != parent_finding.raw_start_byte
                or child_finding.raw_end_byte != parent_finding.raw_end_byte
            ):
                raise ResumeValidationError("override finding lineage is invalid")
            entry = by_ordinal.get(ordinal)
            action = (
                entry["action"] if entry is not None else parent_finding.disposition
            )
            expected_disposition = "replace" if action == "delete" else action
            if child_finding.disposition != expected_disposition:
                raise ResumeValidationError("override action lineage is invalid")
            if entry is None:
                continue
            span_ordinal = child_finding.sanitization_span_ordinal
            if action == "delete":
                if span_ordinal is None or child_spans[span_ordinal].kind != "delete":
                    raise ResumeValidationError("delete override lineage is invalid")
            replacement = entry.get("replacement")
            if replacement is not None:
                if span_ordinal is None or child_spans[span_ordinal].kind != "replace":
                    raise ResumeValidationError(
                        "replacement override lineage is invalid"
                    )
                span = child_spans[span_ordinal]
                if child_bytes[
                    span.sanitized_start_byte : span.sanitized_end_byte
                ] != replacement.encode("utf-8"):
                    raise ResumeValidationError(
                        "replacement override lineage is invalid"
                    )


def get_sanitization(c: sqlite3.Connection, run_id: str) -> ResumeSanitizationRun:
    return ResumeSanitizationRun(
        **dict(
            _require(
                _row(c, "SELECT * FROM resume_sanitization_runs WHERE id=?", (run_id,)),
                "sanitization run",
            )
        )
    )


def sanitization_spans(
    c: sqlite3.Connection, run_id: str, *, limit: int = 1000
) -> list[SanitizationSpan]:
    _limit(limit)
    _require(
        _row(c, "SELECT id FROM resume_sanitization_runs WHERE id=?", (run_id,)),
        "sanitization run",
    )
    return [
        SanitizationSpan(**dict(r))
        for r in c.execute(
            "SELECT * FROM resume_sanitization_spans WHERE sanitization_run_id=? ORDER BY ordinal LIMIT ?",
            (run_id, limit),
        ).fetchall()
    ]


def sanitization_findings(
    c: sqlite3.Connection, run_id: str, *, limit: int = 1000
) -> list[ResumePiiFinding]:
    _limit(limit)
    _require(
        _row(c, "SELECT id FROM resume_sanitization_runs WHERE id=?", (run_id,)),
        "sanitization run",
    )
    return [
        ResumePiiFinding(**dict(r))
        for r in c.execute(
            "SELECT * FROM resume_pii_findings WHERE sanitization_run_id=? ORDER BY ordinal LIMIT ?",
            (run_id, limit),
        ).fetchall()
    ]


def list_sanitizations(
    c: sqlite3.Connection, extraction_run_id: str, *, limit: int = 100
) -> list[ResumeSanitizationRun]:
    _limit(limit)
    return [
        ResumeSanitizationRun(**dict(r))
        for r in c.execute(
            "SELECT * FROM resume_sanitization_runs WHERE extraction_run_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (extraction_run_id, limit),
        ).fetchall()
    ]


def find_successful_sanitization(
    c: sqlite3.Connection, cache_fingerprint: str
) -> ResumeSanitizationRun | None:
    r = _row(
        c,
        "SELECT * FROM resume_sanitization_runs WHERE cache_fingerprint=? AND outcome='succeeded'",
        (cache_fingerprint,),
    )
    return None if r is None else ResumeSanitizationRun(**dict(r))


def append_sanitization_review(
    c: sqlite3.Connection,
    event: SanitizationReviewEvent,
    *,
    require_recognized_authority: bool = False,
) -> int:
    with _write(c):
        _require(
            _row(
                c,
                "SELECT id FROM resume_sanitization_runs WHERE id=?",
                (event.sanitization_run_id,),
            ),
            "sanitization run",
        )
        # Review events are append-only, but repeating the same effective
        # decision is an idempotent retry rather than a second audit event.
        previous = c.execute(
            "SELECT id,event_kind FROM resume_sanitization_review_events "
            "WHERE sanitization_run_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (event.sanitization_run_id,),
        ).fetchone()
        if event.event_kind == "approved":
            # Approval is meaningful only for an intact exact artifact.  A
            # tampered aggregate is fail-closed and cannot be approved.
            validated = (
                validate_sanitization_authority(c, event.sanitization_run_id)
                if require_recognized_authority
                else validate_sanitization(c, event.sanitization_run_id)
            )
            if validated.outcome != "succeeded":
                raise ResumeValidationError("failed sanitization cannot be approved")
            unresolved = c.execute(
                "SELECT 1 FROM resume_pii_findings WHERE sanitization_run_id=? "
                "AND disposition IN ('pending','review') LIMIT 1",
                (event.sanitization_run_id,),
            ).fetchone()
            if unresolved is not None:
                raise ResumeValidationError(
                    "unresolved PII findings cannot be approved"
                )
        if (
            previous is not None
            and previous["event_kind"] == event.event_kind
            and event.note is None
        ):
            return int(previous["id"])
        cur = c.execute(
            "INSERT INTO resume_sanitization_review_events(sanitization_run_id,event_kind,note,created_at) VALUES (?,?,?,?)",
            (event.sanitization_run_id, event.event_kind, event.note, event.created_at),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def sanitization_external_use_approved(c: sqlite3.Connection, run_id: str) -> bool:
    """Return whether this exact immutable artifact is currently approved."""
    try:
        run = validate_sanitization_authority(c, run_id)
        if run.outcome != "succeeded":
            return False
        unresolved = c.execute(
            "SELECT 1 FROM resume_pii_findings WHERE sanitization_run_id=? "
            "AND disposition IN ('pending','review') LIMIT 1",
            (run_id,),
        ).fetchone()
        if unresolved is not None:
            return False
        latest = latest_sanitization_review(c, run_id)
        return latest is not None and latest.event_kind == "approved"
    except (ResumeRepositoryError, sqlite3.Error, ValueError, TypeError):
        return False


def sanitization_approval_state(c: sqlite3.Connection, run_id: str) -> str:
    """Return ``approved``, ``revoked`` or fail-closed ``not_approved``."""
    if not sanitization_external_use_approved(c, run_id):
        return "not_approved"
    return "approved"


def sanitization_review_history(
    c: sqlite3.Connection, run_id: str, *, limit: int = 100
) -> list[SanitizationReviewEvent]:
    _limit(limit)
    _require(
        _row(c, "SELECT id FROM resume_sanitization_runs WHERE id=?", (run_id,)),
        "sanitization run",
    )
    return [
        SanitizationReviewEvent(**dict(r))
        for r in c.execute(
            "SELECT sanitization_run_id,event_kind,created_at,note FROM resume_sanitization_review_events WHERE sanitization_run_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    ]


def latest_sanitization_review(
    c: sqlite3.Connection, run_id: str
) -> SanitizationReviewEvent | None:
    history = sanitization_review_history(c, run_id, limit=1)
    return history[0] if history else None


def persist_fact_run(
    c: sqlite3.Connection,
    run: CareerFactRun,
    candidates: Iterable[CareerFactCandidate] = (),
    evidence: Iterable[CareerFactEvidence] = (),
    *,
    reject_existing_successful_cache: bool = False,
) -> CareerFactRun:
    supplied_candidates, supplied_evidence = tuple(candidates), tuple(evidence)
    with _write(c):
        extraction = _require(
            _row(
                c,
                "SELECT * FROM resume_extraction_runs WHERE id=?",
                (run.extraction_run_id,),
            ),
            "extraction run",
        )
        if extraction["outcome"] != "succeeded":
            raise ResumeValidationError("fact run requires a successful extraction")
        artifact = _raw(extraction)
        if run.input_kind == "sanitized":
            sanitization = _require(
                _row(
                    c,
                    "SELECT * FROM resume_sanitization_runs WHERE id=?",
                    (run.sanitization_run_id,),
                ),
                "sanitization run",
            )
            if (
                sanitization["extraction_run_id"] != run.extraction_run_id
                or sanitization["outcome"] != "succeeded"
            ):
                raise ResumeValidationError(
                    "sanitized fact input must match the extraction"
                )
            artifact = _sanitized(sanitization)
        if run.outcome == "failed" and (supplied_candidates or supplied_evidence):
            raise ResumeValidationError("failed fact runs cannot contain artifacts")
        if (
            run.input_byte_size != len(artifact)
            or run.input_sha256 != hashlib.sha256(artifact).hexdigest()
        ):
            raise ResumeValidationError("fact input hash does not match artifact")
        if reject_existing_successful_cache and run.outcome == "succeeded":
            if (
                _row(
                    c,
                    "SELECT id FROM career_fact_runs "
                    "WHERE cache_fingerprint=? AND outcome='succeeded'",
                    (run.cache_fingerprint,),
                )
                is not None
            ):
                raise ResumeConflictError(
                    "successful fact cache identity already exists"
                )
        by_id = {candidate.id: candidate for candidate in supplied_candidates}
        if len(by_id) != len(supplied_candidates):
            raise ResumeValidationError("fact candidates must have unique identifiers")
        supplied_candidates = tuple(
            sorted(supplied_candidates, key=lambda item: item.id)
        )
        for candidate in supplied_candidates:
            if candidate.fact_run_id != run.id:
                raise ResumeValidationError("candidate belongs to another fact run")
            if candidate.origin != run.mode:
                raise ResumeValidationError("candidate origin does not match fact mode")
            canonical, digest = _canonical_json(candidate.canonical_value_json)
            if (
                digest != candidate.value_sha256
                or canonical != candidate.canonical_value_json
            ):
                raise ResumeValidationError("candidate value hash is inconsistent")
        evidence_ordinals: dict[str, list[int]] = {}
        for item in supplied_evidence:
            evidence_ordinals.setdefault(item.candidate_id, []).append(item.ordinal)
        if any(
            values != list(range(len(values))) for values in evidence_ordinals.values()
        ):
            raise ResumeValidationError("evidence ordinals must be contiguous")
        for item in supplied_evidence:
            if item.candidate_id not in by_id:
                raise ResumeValidationError("evidence references an unknown candidate")
            if item.coordinate_space != run.input_kind:
                raise ResumeValidationError(
                    "evidence coordinate space does not match fact input"
                )
            validate_slice_hash(
                artifact, item.start_byte, item.end_byte, item.slice_sha256
            )
        try:
            c.execute(
                """INSERT INTO career_fact_runs
                (id,extraction_run_id,sanitization_run_id,input_kind,mode,
                 extractor_fingerprint,schema_fingerprint,provider_fingerprint,
                 model_fingerprint,prompt_fingerprint,outcome,cache_fingerprint,
                 input_sha256,input_byte_size,token_count,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.id,
                    run.extraction_run_id,
                    run.sanitization_run_id,
                    run.input_kind,
                    run.mode,
                    run.extractor_fingerprint,
                    run.schema_fingerprint,
                    run.provider_fingerprint,
                    run.model_fingerprint,
                    run.prompt_fingerprint,
                    run.outcome,
                    run.cache_fingerprint,
                    run.input_sha256,
                    run.input_byte_size,
                    run.token_count,
                    run.created_at,
                ),
            )
            c.executemany(
                "INSERT INTO career_fact_candidates VALUES (?,?,?,?,?,?,?,?)",
                [tuple(x.__dict__.values()) for x in supplied_candidates],
            )
            c.executemany(
                "INSERT INTO career_fact_evidence VALUES (?,?,?,?,?,?)",
                [tuple(x.__dict__.values()) for x in supplied_evidence],
            )
        except sqlite3.IntegrityError as exc:
            raise ResumeValidationError("fact aggregate is invalid") from exc
    return run


def _aggregate_limit(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ResumeValidationError(f"{label} must be a positive integer")
    return value


def _fact_artifact(c: sqlite3.Connection, run: CareerFactRun) -> bytes:
    extraction = _require(
        _row(
            c,
            "SELECT * FROM resume_extraction_runs WHERE id=?",
            (run.extraction_run_id,),
        ),
        "extraction run",
    )
    if extraction["outcome"] != "succeeded":
        raise ResumeValidationError("fact run requires a successful extraction")
    artifact = _raw(extraction)
    if run.input_kind == "sanitized":
        sanitization = _require(
            _row(
                c,
                "SELECT * FROM resume_sanitization_runs WHERE id=?",
                (run.sanitization_run_id,),
            ),
            "sanitization run",
        )
        if (
            sanitization["extraction_run_id"] != run.extraction_run_id
            or sanitization["outcome"] != "succeeded"
        ):
            raise ResumeValidationError(
                "sanitized fact input must match the extraction"
            )
        artifact = _sanitized(sanitization)
    if (
        run.input_byte_size != len(artifact)
        or run.input_sha256 != hashlib.sha256(artifact).hexdigest()
    ):
        raise ResumeValidationError("fact input hash does not match artifact")
    return artifact


def load_fact_aggregate(
    c: sqlite3.Connection,
    run_id: str,
    *,
    candidate_limit: int = 1000,
    evidence_per_candidate_limit: int = 1000,
    evidence_total_limit: int = 10000,
) -> tuple[
    CareerFactRun,
    tuple[CareerFactCandidate, ...],
    tuple[CareerFactEvidence, ...],
]:
    """Load and validate one complete immutable fact aggregate.

    Unlike the presentation-oriented list helpers, this function never uses a
    silent SQL LIMIT.  It first checks aggregate cardinalities against the
    caller's bounds, then validates every stored row and exact evidence slice.
    """
    _aggregate_limit(candidate_limit, "candidate_limit")
    _aggregate_limit(evidence_per_candidate_limit, "evidence_per_candidate_limit")
    _aggregate_limit(evidence_total_limit, "evidence_total_limit")
    run_row = _require(
        _row(c, "SELECT * FROM career_fact_runs WHERE id=?", (run_id,)),
        "fact run",
    )
    try:
        run = CareerFactRun(**dict(run_row))
    except (KeyError, TypeError, ValueError) as exc:
        raise ResumeValidationError("fact run aggregate is invalid") from exc

    candidate_count = int(
        c.execute(
            "SELECT count(*) FROM career_fact_candidates WHERE fact_run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    if candidate_count > candidate_limit:
        raise ResumeValidationError("fact aggregate exceeds candidate limit")
    evidence_total = int(
        c.execute(
            "SELECT count(*) FROM career_fact_evidence e "
            "JOIN career_fact_candidates c ON c.id=e.candidate_id "
            "WHERE c.fact_run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    if evidence_total > evidence_total_limit:
        raise ResumeValidationError("fact aggregate exceeds evidence limit")
    if (
        c.execute(
            "SELECT 1 FROM career_fact_evidence e "
            "LEFT JOIN career_fact_candidates c ON c.id=e.candidate_id "
            "WHERE c.id IS NULL LIMIT 1"
        ).fetchone()
        is not None
    ):
        raise ResumeValidationError("fact aggregate contains orphan evidence")
    per_candidate = c.execute(
        "SELECT c.id, count(e.candidate_id) FROM career_fact_candidates c "
        "LEFT JOIN career_fact_evidence e ON e.candidate_id=c.id "
        "WHERE c.fact_run_id=? GROUP BY c.id HAVING count(e.candidate_id)>?",
        (run_id, evidence_per_candidate_limit),
    ).fetchone()
    if per_candidate is not None:
        raise ResumeValidationError("fact candidate exceeds evidence limit")

    try:
        candidates = tuple(
            CareerFactCandidate(**dict(row))
            for row in c.execute(
                "SELECT * FROM career_fact_candidates WHERE fact_run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        )
        evidence = tuple(
            CareerFactEvidence(**dict(row))
            for row in c.execute(
                "SELECT e.* FROM career_fact_evidence e "
                "JOIN career_fact_candidates c ON c.id=e.candidate_id "
                "WHERE c.fact_run_id=? ORDER BY e.candidate_id,e.ordinal",
                (run_id,),
            ).fetchall()
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResumeValidationError("fact aggregate is invalid") from exc

    candidate_ids = {candidate.id for candidate in candidates}
    if any(candidate.fact_run_id != run.id for candidate in candidates):
        raise ResumeValidationError("candidate belongs to another fact run")
    try:
        artifact = _fact_artifact(c, run)
    except (ResumeRepositoryError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ResumeRepositoryError):
            raise
        raise ResumeValidationError("fact artifact integrity failure") from exc
    for candidate in candidates:
        canonical, digest = _canonical_json(candidate.canonical_value_json)
        if (
            canonical != candidate.canonical_value_json
            or digest != candidate.value_sha256
        ):
            raise ResumeValidationError("candidate value hash is inconsistent")
    expected_ordinals: dict[str, int] = {}
    for item in evidence:
        if item.candidate_id not in candidate_ids:
            raise ResumeValidationError("evidence references an unknown candidate")
        if item.coordinate_space != run.input_kind:
            raise ResumeValidationError(
                "evidence coordinate space does not match fact input"
            )
        expected = expected_ordinals.setdefault(item.candidate_id, 0)
        if item.ordinal != expected:
            raise ResumeValidationError("evidence ordinals must be contiguous")
        expected_ordinals[item.candidate_id] = expected + 1
        try:
            validate_slice_hash(
                artifact, item.start_byte, item.end_byte, item.slice_sha256
            )
        except (TypeError, ValueError) as exc:
            raise ResumeValidationError("fact evidence integrity failure") from exc
    return run, candidates, evidence


def load_fact_authority(
    c: sqlite3.Connection,
    run_id: str,
    *,
    candidate_limit: int = 1000,
    evidence_per_candidate_limit: int = 1000,
    evidence_total_limit: int = 10000,
    require_recognized: bool = True,
) -> FactAuthority:
    """Materialize complete recognized M5 authority and a transient digest.

    The same function is intentionally usable before a profile lock and while
    an existing ``BEGIN IMMEDIATE`` is held.  It performs no writes and does
    not invoke extraction, parsing, or model code.
    """
    run, candidates, evidence = load_fact_aggregate(
        c,
        run_id,
        candidate_limit=candidate_limit,
        evidence_per_candidate_limit=evidence_per_candidate_limit,
        evidence_total_limit=evidence_total_limit,
    )
    extraction = _require(
        _row(
            c,
            "SELECT * FROM resume_extraction_runs WHERE id=?",
            (run.extraction_run_id,),
        ),
        "extraction run",
    )
    if extraction["outcome"] != "succeeded":
        raise ResumeValidationError("fact authority requires a successful extraction")
    raw = _raw(extraction)
    raw_sha = hashlib.sha256(raw).hexdigest()
    if extraction["raw_text_sha256"] != raw_sha:
        raise ResumeValidationError("extraction artifact integrity failure")
    if require_recognized:
        if (
            run.mode != "deterministic"
            or run.input_kind != "extracted"
            or run.sanitization_run_id is not None
            or run.extractor_fingerprint != DETERMINISTIC_EXTRACTOR_FINGERPRINT
            or run.schema_fingerprint != FACT_SCHEMA_FINGERPRINT
            or run.input_sha256 != raw_sha
            or run.input_byte_size != len(raw)
        ):
            raise ResumeValidationError("fact run is not recognized M5 authority")
        counts: dict[str, int] = {}
        for candidate in candidates:
            if candidate.fact_type not in FACT_TYPES:
                raise ResumeValidationError("fact candidate has an unknown M5 type")
            counts[candidate.fact_type] = counts.get(candidate.fact_type, 0) + 1
            if counts[candidate.fact_type] > TYPE_LIMITS[candidate.fact_type]:
                raise ResumeValidationError("fact candidate type bound exceeded")
    authority = {
        "extraction_run_id": run.extraction_run_id,
        "extraction_raw_sha256": raw_sha,
        "extraction_raw_byte_size": len(raw),
        "fact_run": {
            "id": run.id,
            "extraction_run_id": run.extraction_run_id,
            "sanitization_run_id": run.sanitization_run_id,
            "input_kind": run.input_kind,
            "mode": run.mode,
            "outcome": run.outcome,
            "cache_fingerprint": run.cache_fingerprint,
            "input_sha256": run.input_sha256,
            "input_byte_size": run.input_byte_size,
            "extractor_fingerprint": run.extractor_fingerprint,
            "schema_fingerprint": run.schema_fingerprint,
            "provider_fingerprint": run.provider_fingerprint,
            "model_fingerprint": run.model_fingerprint,
            "prompt_fingerprint": run.prompt_fingerprint,
            "token_count": run.token_count,
            "created_at": run.created_at,
        },
        "candidates": [
            {
                "id": item.id,
                "fact_run_id": item.fact_run_id,
                "fact_type": item.fact_type,
                "normalized_key": item.normalized_key,
                "canonical_value_json": item.canonical_value_json,
                "value_sha256": item.value_sha256,
                "confidence": item.confidence,
                "origin": item.origin,
            }
            for item in candidates
        ],
        "evidence": [
            {
                "candidate_id": item.candidate_id,
                "ordinal": item.ordinal,
                "coordinate_space": item.coordinate_space,
                "start_byte": item.start_byte,
                "end_byte": item.end_byte,
                "slice_sha256": item.slice_sha256,
            }
            for item in evidence
        ],
    }
    digest = hashlib.sha256(canonical_json(authority).encode("utf-8")).hexdigest()
    return FactAuthority(
        run,
        candidates,
        evidence,
        run.extraction_run_id,
        raw_sha,
        len(raw),
        digest,
    )


def get_fact_run(c: sqlite3.Connection, run_id: str) -> CareerFactRun:
    return CareerFactRun(
        **dict(
            _require(
                _row(c, "SELECT * FROM career_fact_runs WHERE id=?", (run_id,)),
                "fact run",
            )
        )
    )


def fact_candidates(
    c: sqlite3.Connection, run_id: str, *, limit: int = 1000
) -> list[CareerFactCandidate]:
    _limit(limit)
    _require(
        _row(c, "SELECT id FROM career_fact_runs WHERE id=?", (run_id,)), "fact run"
    )
    return [
        CareerFactCandidate(**dict(r))
        for r in c.execute(
            "SELECT * FROM career_fact_candidates WHERE fact_run_id=? ORDER BY id LIMIT ?",
            (run_id, limit),
        ).fetchall()
    ]


def fact_evidence(
    c: sqlite3.Connection, candidate_id: str, *, limit: int = 1000
) -> list[CareerFactEvidence]:
    _limit(limit)
    _require(
        _row(c, "SELECT id FROM career_fact_candidates WHERE id=?", (candidate_id,)),
        "fact candidate",
    )
    return [
        CareerFactEvidence(**dict(r))
        for r in c.execute(
            "SELECT * FROM career_fact_evidence WHERE candidate_id=? ORDER BY ordinal LIMIT ?",
            (candidate_id, limit),
        ).fetchall()
    ]


def list_fact_runs(
    c: sqlite3.Connection,
    extraction_run_id: str,
    *,
    sanitization_run_id: str | None = None,
    limit: int = 100,
) -> list[CareerFactRun]:
    _limit(limit)
    if sanitization_run_id is None:
        rows = c.execute(
            "SELECT * FROM career_fact_runs WHERE extraction_run_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (extraction_run_id, limit),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM career_fact_runs WHERE extraction_run_id=? AND sanitization_run_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (extraction_run_id, sanitization_run_id, limit),
        ).fetchall()
    return [CareerFactRun(**dict(r)) for r in rows]


def find_successful_fact_runs(
    c: sqlite3.Connection, cache_fingerprint: str, *, limit: int = 100
) -> list[CareerFactRun]:
    """Return successful fact runs for a non-unique schema-v13 cache identity."""
    _limit(limit)
    rows = c.execute(
        "SELECT * FROM career_fact_runs "
        "WHERE cache_fingerprint=? AND outcome='succeeded' "
        "ORDER BY created_at DESC,id DESC LIMIT ?",
        (cache_fingerprint, limit),
    ).fetchall()
    return [CareerFactRun(**dict(row)) for row in rows]


def append_fact_review(c: sqlite3.Connection, event: CareerFactReviewEvent) -> int:
    with _write(c):
        candidate = _require(
            _row(
                c,
                "SELECT * FROM career_fact_candidates WHERE id=?",
                (event.candidate_id,),
            ),
            "fact candidate",
        )
        if event.event_kind == "approved_edit":
            assert (
                event.edited_value_json is not None
                and event.edited_value_sha256 is not None
            )
            canonical, digest = _canonical_json(event.edited_value_json)
            if (
                canonical != event.edited_value_json
                or digest != event.edited_value_sha256
            ):
                raise ResumeValidationError(
                    "edited candidate value hash is inconsistent"
                )
        del candidate
        cur = c.execute(
            "INSERT INTO career_fact_review_events(candidate_id,event_kind,edited_value_json,edited_value_sha256,note,created_at) VALUES (?,?,?,?,?,?)",
            (
                event.candidate_id,
                event.event_kind,
                event.edited_value_json,
                event.edited_value_sha256,
                event.note,
                event.created_at,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


def fact_review_history(
    c: sqlite3.Connection, candidate_id: str, *, limit: int = 100
) -> list[CareerFactReviewEvent]:
    _limit(limit)
    _require(
        _row(c, "SELECT id FROM career_fact_candidates WHERE id=?", (candidate_id,)),
        "fact candidate",
    )
    return [
        CareerFactReviewEvent(**dict(r))
        for r in c.execute(
            "SELECT candidate_id,event_kind,created_at,edited_value_json,edited_value_sha256,note FROM career_fact_review_events WHERE candidate_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (candidate_id, limit),
        ).fetchall()
    ]


def latest_fact_review(
    c: sqlite3.Connection, candidate_id: str
) -> CareerFactReviewEvent | None:
    history = fact_review_history(c, candidate_id, limit=1)
    return history[0] if history else None


def fact_review_history_records(
    c: sqlite3.Connection, candidate_id: str, *, limit: int | None = None
) -> list[FactReviewRecord]:
    """Strict M6 review history, including event IDs."""
    if limit is not None:
        _limit(limit)
    _require(
        _row(c, "SELECT id FROM career_fact_candidates WHERE id=?", (candidate_id,)),
        "fact candidate",
    )
    query = (
        "SELECT id,candidate_id,event_kind,created_at,edited_value_json,edited_value_sha256,note "
        "FROM career_fact_review_events WHERE candidate_id=? ORDER BY created_at DESC,id DESC"
    )
    args: tuple[Any, ...] = (candidate_id,)
    if limit is not None:
        query += " LIMIT ?"
        args += (limit,)
    rows = c.execute(query, args).fetchall()
    records = []
    for row in rows:
        validate_review_timestamp(row["created_at"])
        records.append(FactReviewRecord(**dict(row)))
    return records


def fact_review_history_records_page(
    c: sqlite3.Connection,
    candidate_id: str,
    *,
    offset: int,
    limit: int,
) -> tuple[list[FactReviewRecord], int]:
    """Read one bounded, deterministically ordered review-history page."""
    if isinstance(offset, bool) or offset < 0:
        raise ResumeValidationError("offset must be non-negative")
    if isinstance(limit, bool) or limit < 1 or limit > 100:
        raise ResumeValidationError("limit must be between 1 and 100")
    _require(
        _row(c, "SELECT id FROM career_fact_candidates WHERE id=?", (candidate_id,)),
        "fact candidate",
    )
    total = int(
        c.execute(
            "SELECT count(*) FROM career_fact_review_events WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()[0]
    )
    rows = c.execute(
        "SELECT id,candidate_id,event_kind,created_at,edited_value_json,edited_value_sha256,note "
        "FROM career_fact_review_events WHERE candidate_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
        (candidate_id, limit, offset),
    ).fetchall()
    records: list[FactReviewRecord] = []
    for row in rows:
        validate_review_timestamp(row["created_at"])
        records.append(FactReviewRecord(**dict(row)))
    return records, total


def latest_fact_review_record(
    c: sqlite3.Connection, candidate_id: str
) -> FactReviewRecord | None:
    history = fact_review_history_records(c, candidate_id, limit=None)
    return history[0] if history else None


def append_fact_review_in_transaction(
    c: sqlite3.Connection,
    event: CareerFactReviewEvent,
    *,
    validate_timestamp: bool = False,
) -> int:
    """Append one review event without opening a nested transaction.

    Callers must already own the write transaction.  The generic
    ``append_fact_review`` remains unchanged for existing M2 callers.
    """
    if validate_timestamp:
        validate_review_timestamp(event.created_at)
    _require(
        _row(
            c, "SELECT * FROM career_fact_candidates WHERE id=?", (event.candidate_id,)
        ),
        "fact candidate",
    )
    if event.event_kind == "approved_edit":
        assert (
            event.edited_value_json is not None
            and event.edited_value_sha256 is not None
        )
        canonical, digest = _canonical_json(event.edited_value_json)
        if canonical != event.edited_value_json or digest != event.edited_value_sha256:
            raise ResumeValidationError("edited candidate value hash is inconsistent")
    cur = c.execute(
        "INSERT INTO career_fact_review_events(candidate_id,event_kind,edited_value_json,edited_value_sha256,note,created_at) VALUES (?,?,?,?,?,?)",
        (
            event.candidate_id,
            event.event_kind,
            event.edited_value_json,
            event.edited_value_sha256,
            event.note,
            event.created_at,
        ),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)
