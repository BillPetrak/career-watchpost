from hashlib import sha256

import pytest

from careeros.career_profile.models import (
    CareerProfileFact,
    CareerProfileFactSource,
    CareerProfileState,
    CareerProfileVersion,
)
from careeros.resume_intelligence.models import (
    CareerFactEvidence,
    CareerFactRun,
    ResumeExtractionRun,
    ResumePiiFinding,
    ResumeSanitizationRun,
)


def test_evidence_coordinate_space_is_closed_vocabulary() -> None:
    assert CareerFactEvidence("c", 0, "extracted", 0, 2, "a" * 64).end_byte == 2
    with pytest.raises(ValueError):
        CareerFactEvidence("c", 0, "raw", 0, 2, "a" * 64)  # type: ignore[arg-type]


def test_initial_profile_state_is_empty_and_valid() -> None:
    state = CareerProfileState(None, 0, 1)
    assert state.current_profile_version_id is None
    with pytest.raises(ValueError):
        CareerProfileState(None, 0, 0)

    version = CareerProfileVersion(
        id="a" * 32,
        version_number=1,
        parent_version_id=None,
        content_fingerprint="b" * 64,
        origin="approved_facts",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert version.content_fingerprint == "b" * 64
    assert CareerProfileFactSource("f", "c") == CareerProfileFactSource("f", "c")
    fact = CareerProfileFact(
        id="c" * 32,
        profile_version_id=version.id,
        logical_fact_id="skill:python",
        fact_type="skill",
        normalized_key="python",
        canonical_value_json='{"name":"Python"}',
        value_sha256="d" * 64,
        origin="approved_candidate",
        privacy_classification="public",
        sort_order=0,
    )
    assert fact.logical_fact_id == "skill:python"


def test_pii_ranges_are_non_empty_and_fact_inputs_are_pinned() -> None:
    with pytest.raises(ValueError):
        ResumePiiFinding("s", 0, "email", "rule", "r1", None, "pending", 2, 2)
    CareerFactRun(
        id="r",
        extraction_run_id="e",
        sanitization_run_id=None,
        input_kind="extracted",
        mode="deterministic",
        outcome="succeeded",
        cache_fingerprint="cache",
        input_sha256="a" * 64,
        input_byte_size=1,
        token_count=None,
        extractor_fingerprint="extractor",
        schema_fingerprint="schema",
        provider_fingerprint=None,
        model_fingerprint=None,
        prompt_fingerprint=None,
        created_at="t",
    )
    CareerFactRun(
        id="r",
        extraction_run_id="e",
        sanitization_run_id="s",
        input_kind="sanitized",
        mode="model",
        outcome="succeeded",
        cache_fingerprint="cache",
        input_sha256="a" * 64,
        input_byte_size=1,
        token_count=1,
        extractor_fingerprint=None,
        schema_fingerprint="schema",
        provider_fingerprint="provider",
        model_fingerprint="model",
        prompt_fingerprint="prompt",
        created_at="t",
    )
    with pytest.raises(ValueError):
        CareerFactRun(
            id="r",
            extraction_run_id="e",
            sanitization_run_id=None,
            input_kind="extracted",
            mode="model",
            outcome="succeeded",
            cache_fingerprint="cache",
            input_sha256="a" * 64,
            input_byte_size=1,
            token_count=None,
            extractor_fingerprint=None,
            schema_fingerprint="schema",
            provider_fingerprint="provider",
            model_fingerprint="model",
            prompt_fingerprint="prompt",
            created_at="t",
        )


def test_resume_artifact_formats_and_hashes_are_bound_to_text() -> None:
    raw_text = "Résumé"
    raw_hash = sha256(raw_text.encode("utf-8")).hexdigest()
    extraction = ResumeExtractionRun(
        id="a" * 32,
        material_version_id="b" * 32,
        source_name="resume.txt",
        source_media_type="text/plain",
        source_byte_size=len(raw_text.encode("utf-8")),
        source_sha256="c" * 64,
        source_format="text",
        detected_format="text",
        outcome="succeeded",
        raw_text=raw_text,
        raw_text_sha256=raw_hash,
        segment_count=1,
        extractor_fingerprint="extractor-v1",
        cache_fingerprint="cache-v1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert extraction.raw_text_sha256 == raw_hash
    with pytest.raises(ValueError, match="format"):
        ResumeExtractionRun(
            **{**extraction.__dict__, "source_format": "rtf"}  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="does not match"):
        ResumeExtractionRun(**{**extraction.__dict__, "raw_text_sha256": "d" * 64})

    sanitized_text = "[NAME]"
    sanitized_hash = sha256(sanitized_text.encode("utf-8")).hexdigest()
    sanitization = ResumeSanitizationRun(
        id="e" * 32,
        extraction_run_id=extraction.id,
        parent_sanitization_run_id=None,
        sanitized_text=sanitized_text,
        sanitized_text_sha256=sanitized_hash,
        sanitizer_fingerprint="sanitizer-v1",
        policy_fingerprint="policy-v1",
        override_manifest_json="{}",
        cache_fingerprint="cache-v2",
        outcome="succeeded",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert sanitization.sanitized_text_sha256 == sanitized_hash
    with pytest.raises(ValueError, match="does not match"):
        ResumeSanitizationRun(
            **{**sanitization.__dict__, "sanitized_text_sha256": "f" * 64}
        )
