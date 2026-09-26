"""Deterministic local M4 sanitization and exact-artifact approval.

This service consumes only an immutable M3 extraction artifact. It performs no
filesystem, network, model, or logging work; the M2 repository remains the sole
persistence authority.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

from careeros.database import resume_intelligence as repository
from careeros.resume_intelligence.models import (
    ResumePiiFinding,
    ResumeSanitizationRun,
    SanitizationReviewEvent,
    SanitizationSpan,
)

from .pii import MAX_TEXT_BYTES, PiiFinding, detect_pii, resolve_sanitization
from .sanitization_identity import (
    DEFAULT_POLICY_FINGERPRINT,
    SANITIZER_FINGERPRINT,
    OverrideAction,
    override_manifest,
    override_policy_fingerprint,
    sanitization_cache_fingerprint,
)


@dataclass(frozen=True)
class _Artifact:
    run_id: str
    text: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class _Computation:
    sanitized_text: str
    spans: tuple[SanitizationSpan, ...]
    findings: tuple[ResumePiiFinding, ...]


def _artifact(c: sqlite3.Connection, extraction_run_id: str) -> _Artifact:
    try:
        run = repository.get_extraction(c, extraction_run_id)
    except ValueError as exc:
        raise repository.ResumeValidationError(
            "extraction artifact integrity failure"
        ) from exc
    if run.outcome != "succeeded" or run.raw_text is None:
        raise repository.ResumeValidationError(
            "sanitization requires a successful extraction"
        )
    raw = run.raw_text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if run.raw_text_sha256 != digest:
        raise repository.ResumeValidationError("extraction artifact integrity failure")
    return _Artifact(run.id, run.raw_text, digest, len(raw))


def _cache_fingerprint(
    artifact: _Artifact,
    *,
    policy_fingerprint: str,
    parent_run_id: str | None,
    override_manifest_json: str,
) -> str:
    return sanitization_cache_fingerprint(
        extraction_run_id=artifact.run_id,
        raw_text_sha256=artifact.sha256,
        raw_text_byte_size=artifact.byte_size,
        policy_fingerprint=policy_fingerprint,
        parent_sanitization_run_id=parent_run_id,
        override_manifest_json=override_manifest_json,
    )


def _winner(
    c: sqlite3.Connection,
    *,
    cache_fingerprint: str,
    extraction_run_id: str,
    policy_fingerprint: str,
    parent_run_id: str | None,
    override_manifest_json: str,
) -> ResumeSanitizationRun | None:
    candidate = repository.find_successful_sanitization(c, cache_fingerprint)
    if candidate is None:
        return None
    if (
        candidate.extraction_run_id != extraction_run_id
        or candidate.sanitizer_fingerprint != SANITIZER_FINGERPRINT
        or candidate.policy_fingerprint != policy_fingerprint
        or candidate.parent_sanitization_run_id != parent_run_id
        or candidate.override_manifest_json != override_manifest_json
    ):
        raise repository.ResumeConflictError(
            "sanitization cache identity conflicts with requested artifact"
        )
    repository.validate_sanitization_authority(c, candidate.id)
    return candidate


def _persist(
    c: sqlite3.Connection,
    artifact: _Artifact,
    computation: _Computation,
    *,
    run_id: str,
    policy_fingerprint: str,
    cache_fingerprint: str,
    created_at: str,
    parent_run_id: str | None,
    override_manifest_json: str,
) -> ResumeSanitizationRun:
    run = ResumeSanitizationRun(
        run_id,
        artifact.run_id,
        parent_run_id,
        computation.sanitized_text,
        hashlib.sha256(computation.sanitized_text.encode("utf-8")).hexdigest(),
        SANITIZER_FINGERPRINT,
        policy_fingerprint,
        override_manifest_json,
        cache_fingerprint,
        "succeeded",
        created_at,
    )
    try:
        return repository.persist_sanitization(
            c, run, computation.spans, computation.findings
        )
    except repository.ResumeConflictError:
        winner = _winner(
            c,
            cache_fingerprint=cache_fingerprint,
            extraction_run_id=artifact.run_id,
            policy_fingerprint=policy_fingerprint,
            parent_run_id=parent_run_id,
            override_manifest_json=override_manifest_json,
        )
        if winner is None:
            raise
        return winner


def sanitize_extraction(
    c: sqlite3.Connection,
    extraction_run_id: str,
    *,
    created_at: str,
    run_id: str | None = None,
) -> ResumeSanitizationRun:
    """Detect, resolve, and persist one exact M3 artifact deterministically."""

    artifact = _artifact(c, extraction_run_id)
    manifest = "{}"
    cache = _cache_fingerprint(
        artifact,
        policy_fingerprint=DEFAULT_POLICY_FINGERPRINT,
        parent_run_id=None,
        override_manifest_json=manifest,
    )
    cached = _winner(
        c,
        cache_fingerprint=cache,
        extraction_run_id=artifact.run_id,
        policy_fingerprint=DEFAULT_POLICY_FINGERPRINT,
        parent_run_id=None,
        override_manifest_json=manifest,
    )
    if cached is not None:
        return cached
    child_id = run_id or uuid.uuid4().hex
    detected = detect_pii(artifact.text)
    resolved = resolve_sanitization(
        artifact.text,
        detected,
        sanitization_run_id=child_id,
    )
    return _persist(
        c,
        artifact,
        _Computation(resolved.sanitized_text, resolved.spans, resolved.findings),
        run_id=child_id,
        policy_fingerprint=DEFAULT_POLICY_FINGERPRINT,
        cache_fingerprint=cache,
        created_at=created_at,
        parent_run_id=None,
        override_manifest_json=manifest,
    )


def create_override(
    c: sqlite3.Connection,
    parent_run_id: str,
    actions: dict[int, OverrideAction],
    *,
    created_at: str,
    replacements: dict[int, str] | None = None,
    run_id: str | None = None,
) -> ResumeSanitizationRun:
    """Create an immutable child from persisted parent findings and typed actions."""

    parent = repository.validate_sanitization_authority(c, parent_run_id)
    if parent.outcome != "succeeded":
        raise repository.ResumeValidationError(
            "override requires a successful parent sanitization"
        )
    if not actions:
        raise repository.ResumeValidationError("override requires at least one action")
    parent_findings = repository.sanitization_findings(c, parent_run_id, limit=1000)
    if any(
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or ordinal < 0
        or ordinal >= len(parent_findings)
        for ordinal in actions
    ):
        raise repository.ResumeValidationError(
            "override action references an unknown finding"
        )
    allowed = {"pending", "replace", "keep", "review", "delete"}
    if any(action not in allowed for action in actions.values()):
        raise repository.ResumeValidationError("override action is invalid")
    replacement_values = dict(replacements or {})
    if any(ordinal not in actions for ordinal in replacement_values):
        raise repository.ResumeValidationError(
            "override replacement requires a matching action"
        )
    for ordinal, replacement in replacement_values.items():
        if not isinstance(replacement, str):
            raise repository.ResumeValidationError("override replacement must be text")
        if actions[ordinal] != "replace" or replacement == "":
            raise repository.ResumeValidationError(
                "custom replacement requires a non-empty replace action"
            )
        if len(replacement.encode("utf-8")) > MAX_TEXT_BYTES:
            raise repository.ResumeValidationError(
                "override replacement exceeds bounded size"
            )
    manifest = override_manifest(actions, replacement_values)
    if len(manifest) > 100_000:
        raise repository.ResumeValidationError("override manifest exceeds bounded size")
    policy = override_policy_fingerprint(manifest)
    artifact = _artifact(c, parent.extraction_run_id)
    cache = _cache_fingerprint(
        artifact,
        policy_fingerprint=policy,
        parent_run_id=parent.id,
        override_manifest_json=manifest,
    )
    cached = _winner(
        c,
        cache_fingerprint=cache,
        extraction_run_id=artifact.run_id,
        policy_fingerprint=policy,
        parent_run_id=parent.id,
        override_manifest_json=manifest,
    )
    if cached is not None:
        return cached
    child_id = run_id or uuid.uuid4().hex
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
    dispositions: dict[int, Literal["pending", "replace", "keep", "review"]] = {}
    effective_replacements = dict(replacement_values)
    for ordinal, action in actions.items():
        if action == "delete":
            dispositions[ordinal] = "replace"
            effective_replacements[ordinal] = ""
        else:
            dispositions[ordinal] = action
    resolved = resolve_sanitization(
        artifact.text,
        findings,
        sanitization_run_id=child_id,
        dispositions=dispositions,
        replacements=effective_replacements,
    )
    return _persist(
        c,
        artifact,
        _Computation(resolved.sanitized_text, resolved.spans, resolved.findings),
        run_id=child_id,
        policy_fingerprint=policy,
        cache_fingerprint=cache,
        created_at=created_at,
        parent_run_id=parent.id,
        override_manifest_json=manifest,
    )


def approve_external_use(
    c: sqlite3.Connection,
    run_id: str,
    *,
    created_at: str,
    note: str | None = None,
) -> int:
    """Approve exactly one intact artifact; approval is never inherited."""

    return repository.append_sanitization_review(
        c,
        SanitizationReviewEvent(run_id, "approved", created_at, note),
        require_recognized_authority=True,
    )


def revoke_external_use(
    c: sqlite3.Connection,
    run_id: str,
    *,
    created_at: str,
    note: str | None = None,
) -> int:
    return repository.append_sanitization_review(
        c, SanitizationReviewEvent(run_id, "revoked", created_at, note)
    )


def is_approved_for_external_use(c: sqlite3.Connection, run_id: str) -> bool:
    return repository.sanitization_external_use_approved(c, run_id)
