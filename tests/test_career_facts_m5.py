from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from careeros.database.connection import connect
from careeros.database.resume_intelligence import (
    ResumeConflictError,
    ResumeValidationError,
    persist_extraction,
    validate_recognized_fact_authority,
)
from careeros.database.schema import initialize_schema
from careeros.resume_intelligence.career_facts import (
    DETERMINISTIC_EXTRACTOR_FINGERPRINT,
    DETERMINISTIC_EXTRACTOR_V1_FINGERPRINT,
    FACT_SCHEMA_FINGERPRINT,
    FACT_TYPE_ORDER,
    INPUT_MAX_BYTES,
    MAX_FIELD_BYTES,
    MAX_NORMALIZED_KEY_LENGTH,
    CareerFactError,
    canonical_json,
    extract_deterministic_facts,
    extract_deterministic_facts_v1,
    run_deterministic_facts,
)
from careeros.resume_intelligence.models import ResumeExtractionRun, ResumeTextSegment


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def db(tmp_path: Path):
    connection = connect(tmp_path / "m5.sqlite3")
    initialize_schema(connection)
    connection.execute("INSERT INTO material_store VALUES (1, ?, ?)", ("1" * 32, "now"))
    connection.execute(
        "INSERT INTO materials VALUES (?, ?, 'resume', 1, 'active', NULL, ?, ?)",
        ("2" * 32, "Resume", "now", "now"),
    )
    yield connection
    connection.close()


def persist_source(db, text: str, run_id: str = "a" * 32) -> ResumeExtractionRun:
    raw = text.encode("utf-8")
    db.execute(
        "INSERT INTO material_versions VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            "3" * 32,
            "2" * 32,
            "resume.txt",
            "text/plain",
            len(raw),
            digest(raw),
            "5" * 64,
            "now",
        ),
    )
    db.commit()
    run = ResumeExtractionRun(
        run_id,
        "3" * 32,
        "resume.txt",
        "text/plain",
        len(raw),
        digest(raw),
        "text",
        "text",
        "succeeded",
        text,
        digest(raw),
        1,
        "extractor",
        "extract-cache",
        "now",
    )
    persist_extraction(
        db, run, [ResumeTextSegment(run.id, 0, 0, len(raw), "test", digest(raw))]
    )
    return run


def test_structural_facts_and_non_authoritative_labelled_email() -> None:
    text = (
        "Name: Zoë Example\nEmail: referee@example.com\nPhone: +30 210 123 4567\n"
        "Portfolio: https://Example.com/work\nSkills:\nPython, SQL\n"
        "Languages:\nEnglish | Ελληνικά\nExperience:\n"
        "- Engineer at Fictional Labs — 2020-01 - 2022\n"
        "Education:\n- MSc at Example University — 2018-2020\n"
        "Certifications:\n- AWS Solutions Architect — Amazon — 2024\n"
    )
    run, candidates, evidence = extract_deterministic_facts(text, "e" * 32)
    assert run.extractor_fingerprint == DETERMINISTIC_EXTRACTOR_FINGERPRINT
    assert {candidate.fact_type for candidate in candidates} >= {
        "identity.name",
        "contact.email",
        "contact.phone",
        "portfolio_reference",
        "skill",
        "language",
        "employment",
        "education",
        "certification",
    }
    assert all(candidate.confidence == 1.0 for candidate in candidates)
    assert all(candidate.origin == "deterministic" for candidate in candidates)
    assert all(item.coordinate_space == "extracted" for item in evidence)
    raw = text.encode()
    assert all(
        hashlib.sha256(raw[item.start_byte : item.end_byte]).hexdigest()
        == item.slice_sha256
        for item in evidence
    )


def test_unlabelled_email_is_omitted_and_extraction_id_changes_candidate_id() -> None:
    text = "Email: person@example.com\n"
    _, candidates_a, _ = extract_deterministic_facts(text, "a" * 32)
    _, candidates_b, _ = extract_deterministic_facts(text, "b" * 32)
    assert any(candidate.fact_type == "contact.email" for candidate in candidates_a)
    assert [candidate.id for candidate in candidates_a] != [
        candidate.id for candidate in candidates_b
    ]


def test_email_gate_requires_complete_labelled_supported_value() -> None:
    text = (
        "person@example.com\n"
        "Email: prose person@example.com\n"
        "E-mail = first@example.com second@example.com\n"
        "Email: referee@example.com\n"
    )
    _, candidates, _ = extract_deterministic_facts(text, "d" * 32)
    emails = [
        candidate for candidate in candidates if candidate.fact_type == "contact.email"
    ]
    assert [candidate.canonical_value_json for candidate in emails] == [
        '{"email":"referee@example.com"}'
    ]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Email: person@example.com", "person@example.com"),
        ("E-mail = person@example.com", "person@example.com"),
        ("Email: δοκιμή@παράδειγμα.δοκιμή", "δοκιμή@παράδειγμα.δοκιμή"),
        ("Email: referee@example.com", "referee@example.com"),
    ],
)
def test_labelled_email_is_exact_non_authoritative_evidence(
    line: str, expected: str
) -> None:
    text = f"Résumé heading\n{line}\n"
    _, candidates, evidence = extract_deterministic_facts(text, "9" * 32)
    candidate = next(item for item in candidates if item.fact_type == "contact.email")
    candidate_evidence = [
        item for item in evidence if item.candidate_id == candidate.id
    ]
    assert len(candidate_evidence) == 1
    item = candidate_evidence[0]
    assert text.encode()[item.start_byte : item.end_byte] == expected.encode()


def test_ambiguous_m4_email_label_and_generic_finding_do_not_create_contact() -> None:
    text = "δοκιμή@localhost\nEmail: δοκιμή@localhost\nOther: valid@example.com\n"
    _, candidates, _ = extract_deterministic_facts(text, "8" * 32)
    assert not any(item.fact_type == "contact.email" for item in candidates)


def test_normalization_keeps_display_and_merges_only_exact_semantics() -> None:
    text = (
        "Phone: +30 (210) 123-4567\nPhone: +30 210 123 4567\nSkills: Python, Python 3\n"
    )
    _, candidates, evidence = extract_deterministic_facts(text, "d" * 32)
    phones = [
        candidate for candidate in candidates if candidate.fact_type == "contact.phone"
    ]
    skills = [candidate for candidate in candidates if candidate.fact_type == "skill"]
    assert len(phones) == 1
    assert phones[0].canonical_value_json == '{"phone":"+30 (210) 123-4567"}'
    assert len([item for item in evidence if item.candidate_id == phones[0].id]) == 2
    assert len(skills) == 2


def test_d_k_and_u_normalization_preserve_non_equivalent_technology_names() -> None:
    text = (
        "Skills: Data-Science, data_science, DATA / SCIENCE, C, C++, C#, "
        "JavaScript, JS\n"
        "Portfolio: HTTPS://Example.COM:443/work/#profile\n"
        "Website: https://example.com/work\n"
    )
    _, candidates, evidence = extract_deterministic_facts(text, "7" * 32)
    skills = [item for item in candidates if item.fact_type == "skill"]
    assert [json.loads(item.canonical_value_json)["name"] for item in skills] == [
        "C",
        "C#",
        "C++",
        "Data-Science",
        "JavaScript",
        "JS",
    ]
    data_science = next(
        item
        for item in skills
        if json.loads(item.canonical_value_json)["name"] == "Data-Science"
    )
    assert len([item for item in evidence if item.candidate_id == data_science.id]) == 3
    portfolios = [
        item for item in candidates if item.fact_type == "portfolio_reference"
    ]
    assert len(portfolios) == 1
    assert json.loads(portfolios[0].canonical_value_json)["url"].startswith("HTTPS://")


def test_candidate_identity_excludes_fact_run_and_evidence_details() -> None:
    text = "Skills: Python, Python\n"
    _, candidates_a, evidence_a = extract_deterministic_facts(
        text, "a" * 32, fact_run_id="1" * 32, created_at="one"
    )
    _, candidates_b, evidence_b = extract_deterministic_facts(
        text, "a" * 32, fact_run_id="2" * 32, created_at="two"
    )
    assert [candidate.id for candidate in candidates_a] == [
        candidate.id for candidate in candidates_b
    ]
    assert [candidate.fact_run_id for candidate in candidates_a] == ["1" * 32]
    assert [candidate.fact_run_id for candidate in candidates_b] == ["2" * 32]
    assert len(evidence_a) == len(evidence_b) == 2
    _, changed, _ = extract_deterministic_facts("Skills: Python 3\n", "a" * 32)
    assert changed[0].id != candidates_a[0].id

    candidate = candidates_a[0]
    semantic = {"name": "python"}
    identity = {
        "extraction_run_id": "a" * 32,
        "extractor_fingerprint": DETERMINISTIC_EXTRACTOR_FINGERPRINT,
        "fact_type": "skill",
        "input_byte_size": len(text.encode()),
        "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "mode": "deterministic",
        "normalized_key": candidate.normalized_key,
        "schema_fingerprint": FACT_SCHEMA_FINGERPRINT,
        "semantic_value": semantic,
        "version": 1,
    }
    assert (
        candidate.id
        == hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:32]
    )
    assert "evidence" not in identity and "confidence" not in identity


def test_date_precision_and_multibyte_evidence_are_exact() -> None:
    text = (
        "Name: Zoë Example\n"
        "Experience:\n- Engineer at Αθήνα Labs — 2020-01-02 - 2022-03\n"
    )
    _, candidates, evidence = extract_deterministic_facts(text, "e" * 32)
    employment = next(
        candidate for candidate in candidates if candidate.fact_type == "employment"
    )
    assert '"start_date":"2020-01-02"' in employment.canonical_value_json
    assert '"end_date":"2022-03"' in employment.canonical_value_json
    raw = text.encode("utf-8")
    for item in evidence:
        assert (
            hashlib.sha256(raw[item.start_byte : item.end_byte]).hexdigest()
            == item.slice_sha256
        )
        assert raw[item.start_byte : item.end_byte].decode("utf-8")


def test_conflicting_dates_keep_discontinuous_evidence() -> None:
    text = (
        "Experience:\n"
        "- Engineer at Fictional Labs — 2020-01 - 2022-03\n"
        "- Engineer at Fictional Labs — 2021-01 - 2023-03\n"
    )
    _, candidates, evidence = extract_deterministic_facts(text, "6" * 32)
    employment = [item for item in candidates if item.fact_type == "employment"]
    assert len(employment) == 2
    first_evidence = sorted(
        (item.start_byte, item.end_byte)
        for item in evidence
        if item.candidate_id == employment[0].id
    )
    assert len(first_evidence) == 4
    assert first_evidence[0][1] < first_evidence[1][0]


def test_candidate_and_evidence_order_are_frozen() -> None:
    text = "Languages: Greek\nSkills: Python\nName: Zoë Example\nEmail: z@example.com\n"
    _, candidates, evidence = extract_deterministic_facts(text, "5" * 32)
    assert [FACT_TYPE_ORDER.index(item.fact_type) for item in candidates] == sorted(
        FACT_TYPE_ORDER.index(item.fact_type) for item in candidates
    )
    assert [(item.candidate_id, item.ordinal) for item in evidence] == sorted(
        (item.candidate_id, item.ordinal) for item in evidence
    )
    for candidate in candidates:
        value = candidate.canonical_value_json
        assert value == canonical_json(json.loads(value))
        assert candidate.value_sha256 == hashlib.sha256(value.encode()).hexdigest()


def test_ambiguity_and_non_inference_gates_prefer_omission() -> None:
    text = (
        "Name: ???\n"
        "Phone: call +30 210 123 4567\n"
        "https://example.com/unlabelled\n"
        "Senior Engineer at Unlabelled Corp 2020-2022\n"
        "Languages: English (fluent), Greek: native\n"
        "Summary: Python, published papers, ten years experience\n"
    )
    _, candidates, _ = extract_deterministic_facts(text, "4" * 32)
    assert candidates == ()


def test_malformed_labelled_portfolio_url_is_omitted() -> None:
    _, candidates, evidence = extract_deterministic_facts(
        "Portfolio: https://[bad\n", "4" * 32
    )
    assert candidates == ()
    assert evidence == ()


def test_per_type_candidate_bound_fails_closed() -> None:
    exact = ", ".join(f"Skill{index}" for index in range(256))
    _, candidates, _ = extract_deterministic_facts(f"Skills: {exact}\n", "f" * 32)
    assert len([item for item in candidates if item.fact_type == "skill"]) == 256
    skills = ", ".join(f"Skill{index}" for index in range(257))
    with pytest.raises(CareerFactError, match="candidate bound"):
        extract_deterministic_facts(f"Skills: {skills}\n", "f" * 32)


def test_input_field_and_m4_finding_bounds_fail_closed() -> None:
    extract_deterministic_facts("x" * INPUT_MAX_BYTES, "3" * 32)
    with pytest.raises(CareerFactError, match="input exceeds"):
        extract_deterministic_facts("x" * (INPUT_MAX_BYTES + 1), "3" * 32)
    with pytest.raises(CareerFactError, match="field bound"):
        extract_deterministic_facts("Skills: " + "x" * (MAX_FIELD_BYTES + 1), "3" * 32)
    contacts = " ".join(f"person{index}@example.com" for index in range(500))
    with pytest.raises(CareerFactError, match="detector failed"):
        extract_deterministic_facts(contacts, "3" * 32)


def test_normalized_key_exact_boundary_and_one_over() -> None:
    overhead = len('skill:{"name":""}')
    exact = "x" * (MAX_NORMALIZED_KEY_LENGTH - overhead)
    _, candidates, _ = extract_deterministic_facts(f"Skills: {exact}\n", "3" * 32)
    assert len(candidates[0].normalized_key) == MAX_NORMALIZED_KEY_LENGTH

    over = exact + "x"
    with pytest.raises(CareerFactError, match="normalized key bound"):
        extract_deterministic_facts(f"Skills: {over}\n", "3" * 32)


def test_evidence_per_candidate_and_global_bounds_are_exact() -> None:
    exact = "Skills:\n" + "\n".join("Python" for _ in range(1000))
    _, candidates, evidence = extract_deterministic_facts(exact, "2" * 32)
    assert len(candidates) == 1 and len(evidence) == 1000
    over = exact + "\nPython"
    with pytest.raises(CareerFactError, match="evidence bound"):
        extract_deterministic_facts(over, "2" * 32)

    exact_global = "Skills:\n" + "\n".join(
        f"Skill{index % 10}" for index in range(10_000)
    )
    _, _, global_evidence = extract_deterministic_facts(exact_global, "2" * 32)
    assert len(global_evidence) == 10_000
    over_global = "Skills:\n" + "\n".join(
        f"Skill{index % 11}" for index in range(10_001)
    )
    with pytest.raises(CareerFactError, match="evidence bound"):
        extract_deterministic_facts(over_global, "2" * 32)


def test_service_cache_hit_is_stable_and_tamper_is_rejected(db) -> None:
    source = persist_source(db, "Name: Zoë Example\nSkills: Python\n")
    first = run_deterministic_facts(db, source.id, created_at="first")
    second = run_deterministic_facts(db, source.id, created_at="second")
    assert second == first
    db.execute(
        "UPDATE career_fact_candidates SET canonical_value_json='{}' "
        "WHERE fact_run_id=?",
        (first[0].id,),
    )
    db.commit()
    with pytest.raises(ResumeConflictError):
        run_deterministic_facts(db, source.id, created_at="third")


def test_service_cache_rejects_tampered_evidence(db) -> None:
    source = persist_source(db, "Skills: Python\n")
    first = run_deterministic_facts(db, source.id, created_at="first")
    db.execute(
        "UPDATE career_fact_evidence SET slice_sha256=? WHERE candidate_id=?",
        ("0" * 64, first[1][0].id),
    )
    db.commit()
    with pytest.raises(ResumeConflictError):
        run_deterministic_facts(db, source.id, created_at="second")


def test_service_cache_rejects_semantically_tampered_key_and_truncation(db) -> None:
    source = persist_source(db, "Skills: Python\n")
    first = run_deterministic_facts(db, source.id, created_at="first")
    candidate_id = first[1][0].id
    db.execute(
        "UPDATE career_fact_candidates SET normalized_key=? WHERE id=?",
        ('skill:{"name":"tampered"}', candidate_id),
    )
    db.commit()
    with pytest.raises(ResumeConflictError):
        run_deterministic_facts(db, source.id, created_at="second")


def test_service_cache_rejects_extra_candidate_id(db) -> None:
    source = persist_source(db, "Skills: Python\n")
    first = run_deterministic_facts(db, source.id, created_at="first")
    value_json = '{"name":"Rust"}'
    db.execute(
        "INSERT INTO career_fact_candidates VALUES (?,?,?,?,?,?,?,?)",
        (
            "f" * 32,
            first[0].id,
            "skill",
            'skill:{"name":"rust"}',
            value_json,
            digest(value_json.encode()),
            1.0,
            "deterministic",
        ),
    )
    db.commit()
    with pytest.raises(ResumeConflictError):
        run_deterministic_facts(db, source.id, created_at="second")


def test_service_cache_rejects_truncated_evidence_and_run_fingerprint(db) -> None:
    source = persist_source(db, "Skills: Python\n")
    first = run_deterministic_facts(db, source.id, created_at="first")
    db.execute(
        "DELETE FROM career_fact_evidence WHERE candidate_id=?",
        (first[1][0].id,),
    )
    db.execute(
        "UPDATE career_fact_runs SET extractor_fingerprint='tampered' WHERE id=?",
        (first[0].id,),
    )
    db.commit()
    with pytest.raises(ResumeConflictError):
        run_deterministic_facts(db, source.id, created_at="second")


def test_recognized_authority_rejects_multibyte_interior_evidence_offset(db) -> None:
    text = "Name: Zoë Example\n"
    source = persist_source(db, text)
    run, candidates, _ = run_deterministic_facts(db, source.id, created_at="first")
    candidate = next(item for item in candidates if item.fact_type == "identity.name")
    row = db.execute(
        "SELECT start_byte,end_byte FROM career_fact_evidence WHERE candidate_id=?",
        (candidate.id,),
    ).fetchone()
    raw = text.encode("utf-8")
    interior = text.index("ë") + 1
    assert raw[interior] & 0b1100_0000 == 0b1000_0000
    db.execute(
        "UPDATE career_fact_evidence SET start_byte=?,slice_sha256=? "
        "WHERE candidate_id=?",
        (interior, digest(raw[interior : row["end_byte"]]), candidate.id),
    )
    db.commit()

    with pytest.raises(ResumeValidationError, match="evidence integrity"):
        validate_recognized_fact_authority(db, run.id)


@pytest.mark.parametrize("tamper", ["candidate_id", "cache_fingerprint"])
def test_recognized_authority_rejects_identity_tampering(db, tamper: str) -> None:
    source = persist_source(db, "Skills: Python\n")
    run, candidates, _ = run_deterministic_facts(db, source.id, created_at="first")
    candidate = candidates[0]
    if tamper == "candidate_id":
        db.commit()
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "UPDATE career_fact_evidence SET candidate_id=? WHERE candidate_id=?",
            ("f" * 32, candidate.id),
        )
        db.execute(
            "UPDATE career_fact_candidates SET id=? WHERE id=?",
            ("f" * 32, candidate.id),
        )
        db.commit()
        db.execute("PRAGMA foreign_keys=ON")
    else:
        db.execute(
            "UPDATE career_fact_runs SET cache_fingerprint=? WHERE id=?",
            ("f" * 64, run.id),
        )
        db.commit()

    with pytest.raises(ResumeValidationError, match="differs from M5 authority"):
        validate_recognized_fact_authority(db, run.id)


def test_zero_candidate_cache_retry_reuses_one_successful_run(db) -> None:
    source = persist_source(db, "Unstructured fictional prose only.\n")
    first = run_deterministic_facts(db, source.id, created_at="first")
    second = run_deterministic_facts(db, source.id, created_at="second")
    assert first == second
    assert first[1:] == ((), ())
    assert db.execute("SELECT count(*) FROM career_fact_runs").fetchone()[0] == 1


def test_overflow_service_persists_only_failed_zero_artifact_run(db) -> None:
    skills = ", ".join(f"Skill{index}" for index in range(257))
    source = persist_source(db, f"Skills: {skills}\n")
    before = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "explicit_preferences",
            "preference_evidence",
            "learned_preference_suppressions",
        )
    }
    with pytest.raises(CareerFactError, match="candidate bound"):
        run_deterministic_facts(db, source.id, created_at="failed")
    assert db.execute("SELECT count(*) FROM career_fact_runs").fetchone()[0] == 1
    assert db.execute("SELECT outcome FROM career_fact_runs").fetchone()[0] == "failed"
    assert db.execute("SELECT count(*) FROM career_fact_candidates").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM career_fact_evidence").fetchone()[0] == 0
    after = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before


def test_empty_extracted_artifact_is_successful_and_empty() -> None:
    run, candidates, evidence = extract_deterministic_facts("", "c" * 32)
    assert run.outcome == "succeeded"
    assert candidates == ()
    assert evidence == ()


def test_v2_list_decoration_pii_boundaries_and_language_layouts() -> None:
    text = (
        "Contact Details\nName: Fictional Owner\nEmail: owner@example.test\n"
        "Phone: +30 210 123 4567\nSummary\nA sentence that is not a skill.\n"
        "Skills:\n  o Python\n  O JAVA\n  o OpenGL\n  o Objective-C\n  o SQL Database\n"
        "  owner@example.test\n  +30 210 123 4567\nProjects\nSelected Projects\n"
        "  o Fictional API\nProfessional Experience\n"
        "- Senior Engineer at Fictional Labs — 2020-01 - 2022\n"
        "Languages\n  o English:\n      Proficient\n  o Hindi: Proficient\n"
        "  - Greek\n  - English: Fluent\n  - Hindi - Advanced\n  Proficient\n"
        "References\n  o Not a skill\n"
    )
    _run, candidates, evidence = extract_deterministic_facts(text, "v" * 32)
    skills = {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "skill"
    }
    languages = {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "language"
    }
    assert {"Python", "JAVA", "OpenGL", "Objective-C", "SQL Database"}.issubset(skills)
    assert not any(
        "owner@example" in value or value.startswith("+") for value in skills
    )
    assert {"English", "Hindi", "Greek"}.issubset(languages)
    assert "Proficient" not in languages
    expected = {
        "English",
        "Hindi",
        "Greek",
        "Python",
        "JAVA",
        "OpenGL",
        "Objective-C",
        "SQL Database",
    }
    language_skill_ids = {
        candidate.id
        for candidate in candidates
        if candidate.fact_type in {"skill", "language"}
    }
    assert all(
        text.encode()[item.start_byte : item.end_byte].decode() in expected
        for item in evidence
        if item.candidate_id in language_skill_ids
    )


def test_v1_identity_and_v2_cache_are_distinct() -> None:
    text = "Skills:\n- Python\nLanguages:\n- English: Fluent\n"
    v1, old_candidates, _ = extract_deterministic_facts_v1(
        text, "x" * 32, fact_run_id="1" * 32, created_at="old"
    )
    v2, new_candidates, _ = extract_deterministic_facts(
        text, "x" * 32, fact_run_id="2" * 32, created_at="new"
    )
    assert v1.extractor_fingerprint == DETERMINISTIC_EXTRACTOR_V1_FINGERPRINT
    assert v2.extractor_fingerprint == DETERMINISTIC_EXTRACTOR_FINGERPRINT
    assert v1.cache_fingerprint != v2.cache_fingerprint
    assert {item.id for item in old_candidates}.isdisjoint(
        {item.id for item in new_candidates}
    )


def test_pinned_historical_v1_fixture_uses_frozen_boundary_grammar() -> None:
    text = "Skills:\n- Python\nProfessional Summary\n- Fictional prose\n"
    run, candidates, _ = extract_deterministic_facts_v1(
        text, "h" * 32, fact_run_id="1" * 32, created_at="old"
    )
    assert (
        run.cache_fingerprint
        == "d75de51be77d18e763bf750f693a8a578f06d9446f702286f54abbeaabda5a98"
    )
    assert [(item.id, item.canonical_value_json) for item in candidates] == [
        ("a6724e1ac1bd3f8da63d169b1842ca3e", '{"name":"Fictional prose"}'),
        ("cb0dca14bcca41dd5977b9ebebfb28a5", '{"name":"Professional Summary"}'),
        ("a246a126b8ebab73a3e3d06a96b4819f", '{"name":"Python"}'),
    ]


def test_v2_wrapped_language_requires_immediate_descriptor() -> None:
    text = (
        "Languages:\n  o English:\n    Proficient\n"
        "  o Hindi: Proficient\n\nProficient\n"
    )
    _run, candidates, _evidence = extract_deterministic_facts(text, "w" * 32)
    assert {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "language"
    } == {"English", "Hindi"}

    dangling = "Languages:\n  o CrossPage:\n\nProficient\n"
    _run, candidates, _evidence = extract_deterministic_facts(dangling, "z" * 32)
    assert candidates == ()


def test_v2_omits_unstructured_multiword_skill_prose() -> None:
    prose = "Skills:\nDesigned cloud systems\nProfessional Experience\n"
    _run, candidates, _evidence = extract_deterministic_facts(prose, "p" * 32)
    assert candidates == ()

    structured = (
        "Skills: SQL Database\n"
        "- Multi Word Skill\n"
        "  o Another Multi Word Skill\n"
        "Python, Cloud Systems\n"
    )
    _run, candidates, _evidence = extract_deterministic_facts(structured, "q" * 32)
    assert {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "skill"
    } == {
        "SQL Database",
        "Multi Word Skill",
        "Another Multi Word Skill",
        "Python",
        "Cloud Systems",
    }

    unindented = "Skills:\no Python\nO JAVA\n  o Python\n"
    _run, candidates, _evidence = extract_deterministic_facts(unindented, "r" * 32)
    assert {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "skill"
    } == {"Python", "JAVA"}


def test_v2_languages_accept_lexical_hyphens_and_bounded_cefr_annotations() -> None:
    text = (
        "Languages:\n"
        "o English\n"
        "- Serbo-Croatian\n"
        "English: LRN Proficiency C2 (Distinction)\n"
        "German: Goethe-Zertifikat B1 (Befriedigend)\n"
        "C2\n"
        "Distinction\n"
    )
    _run, candidates, _evidence = extract_deterministic_facts(text, "c" * 32)
    assert {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "language"
    } == {"English", "Serbo-Croatian", "German"}

    ambiguous = "Languages:\nEnglish -Fluent\nEnglish - Fluent\n"
    _run, candidates, _evidence = extract_deterministic_facts(ambiguous, "d" * 32)
    assert {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "language"
    } == {"English"}


def test_v2_cefr_and_qualification_annotation_matrix() -> None:
    text = (
        "Languages:\n"
        "o English: C2\n"
        "- Hindi: B1\n"
        "Greek: c1\n"
        "English: LRN Proficiency C2 (Distinction)\n"
        "Hindi: Goethe-Zertifikat B1 (Befriedigend)\n"
        "English — Fluent\n"
        "Hindi – Proficient\n"
        "English - Advanced\n"
        "C2\nB1\nProficient\nProficiency\nDistinction\nBefriedigend\n"
    )
    _run, candidates, evidence = extract_deterministic_facts(text, "m" * 32)
    languages = {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "language"
    }
    assert languages == {"English", "Hindi", "Greek"}
    assert not any(item.fact_type == "certification" for item in candidates)
    assert all(
        text.encode()[item.start_byte : item.end_byte].decode() in languages
        for item in evidence
        if item.candidate_id
        in {
            candidate.id
            for candidate in candidates
            if candidate.fact_type == "language"
        }
    )


def test_v2_wrapped_certificate_annotations_are_consumed() -> None:
    text = (
        "Languages:\n"
        "o English:\n  LRN Proficiency C2\n"
        "o German:\n  Goethe-Zertifikat B1\n"
        "German — Goethe-Zertifikat B1 (Befriedigend)\n"
        "English – LRN Proficiency C2 (Distinction)\n"
        "Hindi - Goethe-Zertifikat B1 (Befriedigend)\n"
    )
    _run, candidates, evidence = extract_deterministic_facts(text, "n" * 32)
    language_candidates = [item for item in candidates if item.fact_type == "language"]
    assert {
        json.loads(item.canonical_value_json)["name"] for item in language_candidates
    } == {"English", "German", "Hindi"}
    assert not any(item.fact_type == "certification" for item in candidates)
    ids = {item.id for item in language_candidates}
    assert all(
        text.encode()[item.start_byte : item.end_byte].decode()
        in {"English", "German", "Hindi"}
        for item in evidence
        if item.candidate_id in ids
    )


def test_v2_wrapped_annotation_never_crosses_next_language_entry() -> None:
    for next_entry in ("o German: C2", "- German: C2", "German: C2"):
        text = f"Languages:\no English:\n{next_entry}\n"
        _run, candidates, _evidence = extract_deterministic_facts(text, "x" * 32)
        assert {
            json.loads(item.canonical_value_json)["name"]
            for item in candidates
            if item.fact_type == "language"
        } == {"German"}

    text = "Languages:\no English:\nLRN Proficiency C2 (Distinction)\n"
    _run, candidates, _evidence = extract_deterministic_facts(text, "y" * 32)
    assert {
        json.loads(item.canonical_value_json)["name"]
        for item in candidates
        if item.fact_type == "language"
    } == {"English"}


def test_v2_missing_or_unsupported_qualification_is_omitted() -> None:
    text = (
        "Languages:\n"
        "LRN Proficiency C2 (Distinction)\n"
        "Goethe-Zertifikat B1 (Befriedigend)\n"
        "English: Unsupported Qualification\n"
    )
    _run, candidates, _evidence = extract_deterministic_facts(text, "u" * 32)
    assert candidates == ()


def test_v2_wrapped_arbitrary_cefr_text_is_not_associated() -> None:
    text = "Languages:\no English:\nGerman C2\n"
    _run, candidates, _evidence = extract_deterministic_facts(text, "v" * 32)
    assert candidates == ()
