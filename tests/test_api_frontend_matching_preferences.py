"""Contracts for the memory-only R2 M3 Matching Preferences workspace."""

from __future__ import annotations

from importlib import resources

STATIC = resources.files("careeros.api").joinpath("static")


def text(name: str) -> str:
    return STATIC.joinpath(name).read_text()


def region(js: str, start: str, end: str) -> str:
    return js[js.index(start) : js.index(end, js.index(start))]


def test_matching_workspace_exposes_all_profile_fields_and_actions() -> None:
    html, js = text("index.html"), text("app.js")
    assert 'href="#matching"' in html
    assert 'data-view="matching"' in html
    for field in (
        "desiredTitles",
        "requiredSkills",
        "skills",
        "preferredLocations",
        "remotePreference",
        "preferredEmploymentTypes",
        "preferredSeniority",
        "excludedTerms",
        "hardExcludedTerms",
    ):
        assert field in js
    for action in (
        "Preview matches",
        "Use these preferences for this session",
        "Reset draft to current preferences",
    ):
        assert action in js


def test_required_and_hard_exclusion_copy_is_truthful() -> None:
    js = text("app.js")
    assert "Required does not remove a job" in js
    assert "missing skills can reduce rank when description evidence is available" in js
    assert "force displayed match_score to zero" in js
    assert "do not delete the listing" in js
    assert "threshold-zero surface may still include it" in js


def test_list_parsing_trims_and_preserves_order_and_duplicates() -> None:
    js = text("app.js")
    parser = region(js, "function parseListValue", "function readReviewFields")
    assert ".split(/[,\\n]+/)" in parser
    assert "entry.trim()" in parser
    assert "entry.length > 0" in parser
    assert ".sort(" not in parser
    assert "Set(" not in parser
    assert "dedup" not in parser.lower()
    for forbidden in ("toLowerCase", "synonym", "stem", "normalize"):
        assert forbidden not in parser


def test_open_remote_and_seniority_vocabulary_is_not_narrowed() -> None:
    js = text("app.js")
    matching = region(js, "function profileFieldSpec", "// Saved Searches workspace")
    assert '"preferredSeniority"' in matching
    assert 'remote.name = "remote_preference"' in matching
    assert "remote.maxLength = MAX_REMOTE_LENGTH" in matching
    assert "node.value.trim()" in matching
    assert 'document.createElement("select")' not in matching
    assert "onsite_or_unspecified" not in matching


def test_client_limits_are_ux_only_and_server_request_is_authoritative() -> None:
    js = text("app.js")
    assert "var MAX_LIST_ENTRIES = 100;" in js
    assert "var MAX_ENTRY_LENGTH = 100;" in js
    assert "var MAX_REMOTE_LENGTH = 50;" in js
    assert "values.length > MAX_LIST_ENTRIES" in js
    assert "values[j].length > MAX_ENTRY_LENGTH" in js
    assert "profile.remotePreference.length > MAX_REMOTE_LENGTH" in js
    preview = region(
        js, "async function previewMatching", "function renderMatchingPreview"
    )
    assert "await postReportJson(" in preview
    assert "The /ranked-jobs response did not match" in preview


def test_matching_validation_is_announced_focused_and_clears_busy_state() -> None:
    html, js = text("index.html"), text("app.js")
    assert (
        'id="matching-status" class="status" role="status" '
        'aria-live="polite" tabindex="-1"' in html
    )
    validation = region(
        js, "function announceMatchingValidation", "function adoptMatchingProfile"
    )
    assert "doneLoading(status)" in validation
    assert "status.textContent = message" in validation
    assert "status.focus()" in validation
    adoption = region(
        js, "function adoptMatchingProfile", "function validateMatchingPreviewControls"
    )
    preview = region(
        js, "async function previewMatching", "function renderMatchingPreview"
    )
    assert "announceMatchingValidation(status, error)" in adoption
    assert "announceMatchingValidation(status, validation)" in preview


def test_preview_uses_only_ranked_jobs_with_draft_and_run_controls() -> None:
    js = text("app.js")
    preview = region(
        js, "async function previewMatching", "function renderMatchingPreview"
    )
    assert '"/ranked-jobs?grouped="' in preview
    assert '"&minimum_score="' in preview
    assert '"&offset=0&limit="' in preview
    assert "untracked: true" in preview
    assert "freezeProfile(matchingDraftProfile)" in preview
    assert '"/report' not in preview
    render = region(js, "function renderMatchingPreview", "// Saved Searches workspace")
    assert "snapshot.items.forEach" in render
    assert ".sort(" not in render
    assert "filter(" not in render


def test_draft_edit_reset_and_adoption_make_zero_requests() -> None:
    js = text("app.js")
    editing = region(
        js, "function matchingDraftInputChanged", "function matchingSnapshotIsStale"
    )
    adoption = region(
        js, "function adoptMatchingProfile", "function validateMatchingPreviewControls"
    )
    workspace = region(
        js, "function renderMatchingWorkspace", "function validateMatchingProfile"
    )
    for source in (editing, adoption, workspace):
        assert "postReportJson(" not in source
        assert "fetch(" not in source
    assert "matchingActiveProfile = cloneProfile(profile)" in adoption
    assert "matchingActiveProfileGeneration++" in adoption
    assert "matchingDraftProfile = cloneProfile(matchingActiveProfile)" in workspace
    assert "matchingPreviewSnapshot = null" not in workspace


def test_preview_snapshot_is_immutable_and_generation_complete() -> None:
    js = text("app.js")
    preview = region(
        js, "async function previewMatching", "function renderMatchingPreview"
    )
    assert "matchingPreviewSnapshot = Object.freeze({" in preview
    for field in (
        "profileGeneration",
        "draftGeneration",
        "controlsGeneration",
        "controls",
        "runGeneration",
        "items",
        "total",
    ):
        assert field in preview
    assert "Object.freeze(data.items.slice())" in preview


def test_concurrency_guards_reject_stale_preview_success_error_and_navigation() -> None:
    js = text("app.js")
    preview = region(
        js, "async function previewMatching", "function renderMatchingPreview"
    )
    assert "seq !== matchingPreviewRequestSeq" in preview
    assert "draftGeneration !== matchingDraftGeneration" in preview
    assert "profileGeneration !== matchingActiveProfileGeneration" in preview
    assert "controlsGeneration !== matchingPreviewControlsGeneration" in preview
    assert "visibleView() !== MATCHING" in preview
    assert "matchingPreviewRequestSeq++" in js


def test_review_captures_active_profile_and_mutation_refresh_reuses_it() -> None:
    js = text("app.js")
    review = region(
        js, "async function runReviewRequest", "function renderStoredReviewSnapshot"
    )
    assert "freezeProfile(fields.profile || matchingActiveProfile)" in review
    assert "fields.profileGeneration" in review
    assert "reviewSnapshot = Object.freeze({" in review
    assert "runReviewRequest(captured)" in review
    refresh = region(
        js, "async function runReviewQueueRefresh", "function requestReviewQueueRefresh"
    )
    assert "buildProfilePayload(fields)" in refresh
    decision = region(
        js, "async function submitQueueDecision", "// Reviewed opportunities"
    )
    assert "var fields = lastReviewFields" in decision
    assert "requestReviewQueueRefresh(generation, fields)" in decision


def test_saved_search_search_mode_is_profile_free_and_rank_mode_captures_profile() -> (
    None
):
    js = text("app.js")
    run = region(
        js, "async function runSavedSearch", "async function renderSavedSearchesView"
    )
    assert 'definition.mode === "rank"' in run
    assert "freezeProfile(retryRun && retryRun.profile" in run
    assert "var body = captured.profile ? profilePayload(captured.profile) : {};" in run
    assert "profileGeneration: captured.profileGeneration" in run
    assert "runGeneration: runGeneration" in run
    assert "captured.profileGeneration !== matchingActiveProfileGeneration" in run
    render = region(
        js, "function renderSavedSearchSnapshot", "function selectSavedSearch"
    )
    assert 'definition.mode === "rank"' in render
    assert "snapshot.profileGeneration !== matchingActiveProfileGeneration" in render


def test_job_detail_context_retains_exact_historical_run_basis() -> None:
    js = text("app.js")
    for field in (
        "runGeneration",
        "profileGeneration",
        "runConfig",
        "targetIdentity",
        "memberIds",
        "explanation",
    ):
        assert field in js
    assert 'surface: "matching-preview"' in js
    assert 'jobDetailOrigin === MATCHING ? "Back to matching preferences"' in js
    assert "matchingPreviewSnapshot.runGeneration" in js
    assert "reviewSnapshot.runGeneration" in js
    assert "savedSearchState.snapshot.runGeneration" in js


def test_privacy_no_persistence_no_client_scoring_and_safe_rendering() -> None:
    combined = text("index.html") + text("app.js") + text("style.css")
    lowered = combined.lower()
    for forbidden in (
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "document.cookie",
        "profile_path",
        ".innerhtml",
        "insertadjacenthtml",
        "document.write",
    ):
        assert forbidden not in lowered
    js = text("app.js")
    matching = region(js, "function profileFieldSpec", "// Saved Searches workspace")
    for forbidden in ("_TITLE_POINTS", "SKILL_POINTS", "scoreJob", "rerank", ".sort("):
        assert forbidden not in matching
    assert "textContent" in combined


def test_saved_and_learned_state_is_loaded_from_authoritative_endpoint() -> None:
    js = text("app.js")
    refresh = region(
        js,
        "async function refreshMatchingPreferenceState",
        "async function runPreferenceMutation",
    )
    assert 'await fetchJson("/preferences")' in refresh
    assert "matchingPreferenceState = data" in refresh
    assert "profileFromPreferenceApi(data.explicit.profile)" in refresh
    assert "var draftGeneration = matchingDraftGeneration" in refresh
    assert "var activeProfileGeneration = matchingActiveProfileGeneration" in refresh
    assert "initializeDraft &&" in refresh
    assert "matchingPreferenceInitializationEligible &&" in refresh
    assert "draftGeneration === matchingDraftGeneration" in refresh
    assert "activeProfileGeneration === matchingActiveProfileGeneration" in refresh
    assert refresh.index("var draftGeneration") < refresh.index(
        'await fetchJson("/preferences")'
    )
    assert refresh.index("draftGeneration === matchingDraftGeneration") < refresh.index(
        "matchingDraftProfile = cloneProfile(stored)"
    )
    assert refresh.index("matchingPreferenceState = data") < refresh.index(
        "matchingPreferenceInitializationEligible &&"
    )
    assert "matchingDraftProfile = cloneProfile(stored)" in refresh
    assert refresh.index("matchingDraftGeneration++") < refresh.index(
        "consumeMatchingPreferenceInitializationEligibility()"
    )
    assert "Configured deliberately empty." in js
    assert "Unconfigured — no stored explicit authority." in js


def test_preference_initialization_eligibility_is_durable_and_one_way() -> None:
    js = text("app.js")
    declaration = "var matchingPreferenceInitializationEligible = true;"
    assert js.count(declaration) == 1
    assert js.count("matchingPreferenceInitializationEligible = true") == 1

    consumer = region(
        js,
        "function consumeMatchingPreferenceInitializationEligibility",
        "function cloneProfile",
    )
    assert "matchingPreferenceInitializationEligible = false" in consumer
    assert "matchingPreferenceInitializationEligible = true" not in consumer
    assert js.count("matchingPreferenceInitializationEligible = false") == 1

    navigate = region(js, "async function navigate", "// DOM helpers")
    assert "matchingPreferenceInitializationEligible" not in navigate

    refresh = region(
        js,
        "async function refreshMatchingPreferenceState",
        "async function runPreferenceMutation",
    )
    successful_path, failure_path = refresh.split("} catch (errorResponse)", 1)
    assert (
        successful_path.count("consumeMatchingPreferenceInitializationEligibility()")
        == 1
    )
    assert "consumeMatchingPreferenceInitializationEligibility()" not in failure_path
    assert "!matchingPreferenceLoaded" in failure_path


def test_only_local_profile_actions_consume_initialization_eligibility() -> None:
    js = text("app.js")
    editing = region(
        js, "function matchingDraftInputChanged", "function matchingSnapshotIsStale"
    )
    profile_edit = region(
        editing, "if (spec)", '} else if (node.id === "matching-remote")'
    )
    remote_edit = region(
        editing,
        '} else if (node.id === "matching-remote")',
        '} else if (\n      node.id === "matching-grouped"',
    )
    preview_controls = editing[editing.index('node.id === "matching-grouped"') :]
    for profile_action in (profile_edit, remote_edit):
        assert "consumeMatchingPreferenceInitializationEligibility()" in profile_action
        assert profile_action.index(
            "consumeMatchingPreferenceInitializationEligibility()"
        ) < profile_action.index("matchingDraftGeneration++")
    assert (
        "consumeMatchingPreferenceInitializationEligibility()" not in preview_controls
    )

    workspace = region(
        js, "function renderMatchingWorkspace", "function validateMatchingProfile"
    )
    draft_reset = region(
        workspace,
        'reset.addEventListener("click", function ()',
        "actions.appendChild(reset)",
    )
    assert "consumeMatchingPreferenceInitializationEligibility()" in draft_reset
    assert draft_reset.index(
        "consumeMatchingPreferenceInitializationEligibility()"
    ) < draft_reset.index("matchingDraftProfile = cloneProfile(matchingActiveProfile)")

    adoption = region(
        js, "function adoptMatchingProfile", "function validateMatchingPreviewControls"
    )
    assert "consumeMatchingPreferenceInitializationEligibility()" in adoption
    assert adoption.index(
        "consumeMatchingPreferenceInitializationEligibility()"
    ) < adoption.index("matchingActiveProfile = cloneProfile(profile)")


def test_local_draft_save_clear_and_authoritative_refetch_are_separate() -> None:
    js = text("app.js")
    save = region(
        js, "function saveExplicitPreferences", "function clearExplicitPreferences"
    )
    clear = region(
        js,
        "function clearExplicitPreferences",
        "function setLearnedPreferenceSuppression",
    )
    mutation = region(
        js,
        "async function runPreferenceMutation",
        "function saveExplicitPreferences",
    )
    assert "readMatchingDraft()" in save
    assert 'runPreferenceMutation("/preferences/explicit"' in save
    assert 'method: "PUT"' in save
    assert 'method: "DELETE"' in clear
    assert "matchingPreferenceState =" not in save + clear
    assert "await writeJson(url, options)" in mutation
    assert "var data = await writeJson" not in mutation
    assert "matchingPreferenceState =" not in mutation
    assert (
        "await refreshMatchingPreferenceState(false, successMessage, mutationFailure)"
        in mutation
    )


def test_learned_rendering_states_provenance_suppress_restore_and_safe_text() -> None:
    js = text("app.js")
    rendering = region(
        js,
        "function renderLearnedExamples",
        "function renderMatchingWorkspace",
    )
    assert 'preference.suppressed ? "suppressed"' in rendering
    assert 'preference.active ? "active" : "inactive"' in rendering
    assert 'preference.suppressed ? "Restore" : "Suppress"' in rendering
    assert "preference.positive_examples" in rendering
    assert "preference.negative_examples" in rendering
    assert "preference.positive_count" in rendering
    assert "preference.negative_count" in rendering
    assert "textContent" in rendering
    for unsafe in ("innerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe not in rendering


def test_preference_stale_responses_cannot_overwrite_newer_state() -> None:
    js = text("app.js")
    refresh = region(
        js,
        "async function refreshMatchingPreferenceState",
        "async function runPreferenceMutation",
    )
    mutation = region(
        js,
        "async function runPreferenceMutation",
        "function saveExplicitPreferences",
    )
    assert "var seq = ++matchingPreferenceRequestSeq" in refresh
    assert "seq !== matchingPreferenceRequestSeq" in refresh
    assert "visibleView() !== MATCHING" in refresh
    assert refresh.index("seq !== matchingPreferenceRequestSeq") < refresh.index(
        "matchingPreferenceState = data"
    )
    assert "matchingPreferenceRequestSeq++" in mutation
    assert "operation !== matchingPreferenceMutationSeq" in mutation
    assert mutation.index("matchingPreferenceRequestSeq++") < mutation.index(
        "matchingPreferenceMutationQueue = matchingPreferenceMutationQueue.then"
    )
    catch = refresh[refresh.index("} catch (errorResponse)") :]
    assert catch.index("seq !== matchingPreferenceRequestSeq") < catch.index(
        "renderError(status"
    )


def test_preference_mutations_are_serialized_and_every_current_outcome_reconciles() -> (
    None
):
    js = text("app.js")
    mutation = region(
        js,
        "async function runPreferenceMutation",
        "function saveExplicitPreferences",
    )
    assert "var matchingPreferenceMutationQueue = Promise.resolve()" in js
    assert "matchingPreferenceMutationQueue.then(" in mutation
    assert mutation.count("queuedMutation") >= 3
    assert "await writeJson(url, options)" in mutation
    assert "mutationFailure = errorResponse" in mutation
    assert mutation.index("await writeJson(url, options)") < mutation.index(
        "mutationFailure = errorResponse"
    )
    assert mutation.index("mutationFailure = errorResponse") < mutation.index(
        "operation !== matchingPreferenceMutationSeq"
    )
    assert mutation.index(
        "operation !== matchingPreferenceMutationSeq"
    ) < mutation.index(
        "await refreshMatchingPreferenceState(false, successMessage, mutationFailure)"
    )
    assert "renderError(" not in mutation


def test_matching_navigation_invalidates_hidden_rendering_and_reentry_refreshes() -> (
    None
):
    js = text("app.js")
    navigate = region(js, "async function navigate", "// DOM helpers")
    leave = region(
        navigate,
        "if (leaving === MATCHING && name !== MATCHING)",
        "if (name === REVIEWED",
    )
    assert "matchingPreferenceRequestSeq++" in leave
    assert "matchingPreferenceMutationSeq++" in leave
    assert "matchingPreferenceViewGeneration++" in leave
    assert "doneLoading(matchingStatus)" in leave
    assert "await refreshMatchingPreferenceState(!matchingPreferenceLoaded)" in navigate

    mutation = region(
        js,
        "async function runPreferenceMutation",
        "function saveExplicitPreferences",
    )
    generation_guard = region(
        mutation,
        "if (viewGeneration !== matchingPreferenceViewGeneration)",
        "if (operation !== matchingPreferenceMutationSeq",
    )
    assert "if (visibleView() === MATCHING)" in generation_guard
    assert "await refreshMatchingPreferenceState(false)" in generation_guard


def test_preference_failure_clears_busy_and_recovery_only_refetches() -> None:
    js = text("app.js")
    refresh = region(
        js,
        "async function refreshMatchingPreferenceState",
        "async function runPreferenceMutation",
    )
    mutation_failure = region(refresh, "if (mutationFailure)", "} else if (status)")
    assert refresh.index("doneLoading(status)") < refresh.index("if (mutationFailure)")
    assert "renderError(" in mutation_failure
    assert "function () { refreshMatchingPreferenceState(false); }" in mutation_failure
    assert "runPreferenceMutation(" not in mutation_failure

    catch = refresh[refresh.index("} catch (errorResponse)") :]
    assert catch.index("doneLoading(status)") < catch.index("renderError(status")
    assert "refreshMatchingPreferenceState(" in catch
    assert "runPreferenceMutation(" not in catch


def test_reset_requires_confirmation_and_preference_client_does_no_learning() -> None:
    js = text("app.js")
    reset = region(
        js,
        "function confirmLearnedPreferenceReset",
        "function profileFieldSpec",
    )
    assert "window.confirm(" in reset
    assert reset.index("window.confirm(") < reset.index(
        'runPreferenceMutation("/preferences/learned"'
    )
    authority = region(
        js,
        "function explicitPreferencePayload",
        "function profileFieldSpec",
    )
    for forbidden in ("scoreJob", "rerank", "deriveLearned", "positive_count++"):
        assert forbidden not in authority
