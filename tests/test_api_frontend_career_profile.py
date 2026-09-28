"""Static contract checks for the zero-build Career Profile workspace."""

from pathlib import Path

ROOT = Path(__file__).parents[1] / "src" / "careeros" / "api" / "static"


def test_career_profile_workspace_has_three_panels_and_navigation():
    html = (ROOT / "index.html").read_text()
    assert 'id="view-career-profile"' in html
    assert 'href="#career-profile"' in html
    for label in ("Career Facts", "Current Profile", "Profile History"):
        assert label in html
    assert 'id="career-tab-facts"' in html
    assert 'aria-labelledby="career-tab-facts"' in html
    assert 'role="tabpanel"' in html


def test_career_profile_client_uses_authoritative_routes_and_no_storage():
    javascript = (ROOT / "app.js").read_text()
    assert "if (raw === CAREER_PROFILE) return CAREER_PROFILE;" in javascript
    assert '"/career-profile/sources?offset="' in javascript
    assert '"/career-profile/current"' in javascript
    assert '"/career-profile/history?offset=0&limit=20"' in javascript
    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "indexedDB" not in javascript


def test_career_profile_client_contains_review_paging_assembly_and_cas_guards():
    javascript = (ROOT / "app.js").read_text()
    for route in (
        "/career-profile/fact-runs/",
        "/career-profile/candidates/",
        "/review",
        "/career-profile/versions",
    ):
        assert route in javascript
    assert "evidence_offset=" in javascript
    assert "history_offset=" in javascript
    assert "approved_edit" in javascript
    assert "expected_revision" in javascript
    assert "status === 409" in javascript
    assert "aria-busy" in javascript
    assert "careerProfileRequestSeq" in javascript
    assert "merge_eligible_candidate_ids" in javascript
    assert "careerProfilePayloadEntries" in javascript
    assert "assembly_primary_candidate_id" in javascript
    assert "preservedDraft" in javascript
    assert "Reapply preserved draft" in javascript
    assert "Stored run is not recognized deterministic M5 authority" in javascript
    assert "Analysis succeeded with no deterministic Career Facts" in javascript
    for field in (
        '"name"',
        '"email"',
        '"phone"',
        '"url"',
        '"title"',
        '"company"',
        '"degree"',
        '"institution"',
        '"issuer"',
    ):
        assert field in javascript


def test_career_profile_save_payload_strips_presentation_state():
    javascript = (ROOT / "app.js").read_text()
    start = javascript.index("function careerProfilePayloadEntries")
    end = javascript.index("function careerProfileButton", start)
    payload_helper = javascript[start:end]
    for forbidden in (
        "entry._label",
        "entry._invalid",
        "entry._candidate_ids",
        "normalized_key",
    ):
        assert forbidden not in payload_helper
    for allowed in (
        "primary_candidate_id",
        "supporting_candidate_ids",
        "logical_fact_id",
        "distinct_identity",
        "privacy_override",
    ):
        assert allowed in payload_helper


def test_career_profile_memory_draft_survives_panel_navigation():
    javascript = (ROOT / "app.js").read_text()
    assert "draftInitialized: false" in javascript
    assert "draftDirty: false" in javascript
    assert "function ensureCareerProfileDraft()" in javascript
    assert "ensureCareerProfileDraft().then(function ()" in javascript
    assert (
        "if (!careerProfileState.draftInitialized || "
        "!careerProfileState.draftDirty) initializeCareerDraft(data);"
    ) in javascript


def test_career_profile_styles_are_narrow_layout_friendly():
    css = (ROOT / "style.css").read_text()
    assert ".career-profile-panels" in css
    assert ".career-profile-source { min-width: 0;" in css
    assert "overflow-wrap: anywhere;" in css
    assert "@media (max-width: 700px)" in css


def test_career_profile_async_and_keyboard_contracts_are_explicit():
    javascript = (ROOT / "app.js").read_text()
    assert "careerProfileViewGeneration" in javascript
    assert "careerProfileOperationToken" in javascript
    assert "focusCareerProfileHeading" in javascript
    assert "setCareerProfileBusy" in javascript
    assert "Candidate state" in javascript
    assert "other.tabIndex = other === button ? 0 : -1" in javascript
    assert (
        "renderCareerProfileCandidates(content, runId, nextOffset, state)" in javascript
    )
    assert "detailSeq !== careerProfileRequestSeq" in javascript
    assert "Profile version authority is unavailable." in javascript
