"""Immutable persistence primitives for resume intelligence artifacts.

This module deliberately accepts already-computed artifacts.  It never reads
material files, invokes parsers/models, or performs external work.
"""

# SQL statements are intentionally kept readable.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, NoReturn, cast

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
            parent = _require(
                _row(
                    c,
                    "SELECT * FROM resume_sanitization_runs WHERE id=?",
                    (run.parent_sanitization_run_id,),
                ),
                "parent sanitization",
            )
            if parent["extraction_run_id"] != run.extraction_run_id:
                raise ResumeValidationError(
                    "parent sanitization belongs to another extraction"
                )
            if parent["outcome"] != "succeeded":
                raise ResumeValidationError("parent sanitization must have succeeded")
        raw = _raw(extraction)
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
                validate_slice_hash(
                    raw,
                    finding.raw_start_byte,
                    finding.raw_end_byte,
                    hashlib.sha256(
                        raw[finding.raw_start_byte : finding.raw_end_byte]
                    ).hexdigest(),
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
                    if not (
                        finding.raw_start_byte < span.raw_end_byte
                        and span.raw_start_byte < finding.raw_end_byte
                    ):
                        raise ResumeValidationError(
                            "finding does not overlap its resolved sanitization span"
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
    c: sqlite3.Connection, event: SanitizationReviewEvent
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
        cur = c.execute(
            "INSERT INTO resume_sanitization_review_events(sanitization_run_id,event_kind,note,created_at) VALUES (?,?,?,?)",
            (event.sanitization_run_id, event.event_kind, event.note, event.created_at),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)


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
