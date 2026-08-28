"""Focused contract checks for the R2 M2 Saved Searches workspace.

The UI is deliberately zero-build JavaScript, so these tests pin the public
route, request and rendering boundaries without requiring a browser runtime.
Browser suites can use the same stable ids and data attributes.
"""

from __future__ import annotations

import importlib.resources
import re

_STATIC = importlib.resources.files("careeros.api").joinpath("static")


def _asset(name: str) -> str:
    return _STATIC.joinpath(name).read_text()


def test_saved_search_route_and_primary_navigation() -> None:
    html, js = _asset("index.html"), _asset("app.js")
    assert 'href="#saved-searches"' in html
    assert 'id="nav-saved-searches"' in html
    assert 'id="view-saved-searches"' in html
    assert 'var SAVED_SEARCHES = "saved-searches"' in js
    assert "if (raw === SAVED_SEARCHES) return SAVED_SEARCHES" in js


def test_entry_and_selection_are_metadata_only() -> None:
    js = _asset("app.js")
    assert 'fetchJson("/saved-searches?offset="' in js
    assert '"/saved-searches/" + encodeURIComponent(definition.name)' in js
    # Selection renders configuration and clears snapshots; only Run invokes POST.
    selection = js[
        js.index("function selectSavedSearch") : js.index(
            "function renderSavedSearchWorkspace"
        )
    ]
    assert "runSavedSearch" not in selection


def test_exact_snapshot_request_and_profile_boundary() -> None:
    js = _asset("app.js")
    assert '"/saved-searches/" + encodeURIComponent(definition.name)' in js
    assert '"/execute?snapshot=true"' in js
    assert "var body = captured.profile ? profilePayload(captured.profile) : {};" in js
    region = js[
        js.index("async function runSavedSearch") : js.index(
            "async function renderSavedSearchesView"
        )
    ]
    assert '"&offset="' not in region
    assert '"&limit="' not in region
    assert "saved_search" in region


def test_search_is_unranked_and_rank_reuses_m1_renderers() -> None:
    js = _asset("app.js")
    saved = js[
        js.index("function renderSavedResult") : js.index(
            "function renderSavedSearchSnapshot"
        )
    ]
    assert 'var rank = definition.mode === "rank"' in saved
    assert "attachCardExplanationAndContext" in saved
    assert 'surface: "saved-search"' in saved
    assert "if (rank)" in saved
    assert "buildUnrankedJobCard" in saved
    assert "Why this match?" not in saved


def test_snapshot_response_is_authoritative_and_bounded_in_memory() -> None:
    js = _asset("app.js")
    assert "savedSearchState.snapshot = Object.freeze({" in js
    assert "saved_search: used" in js
    assert "Object.freeze(data.items.slice())" in js
    assert 'textContent: "Definition used for this run"' in js
    assert "savedSearchFields(definition)" in js
    assert "snapshot.total" in js
    assert "items.slice(start, start + savedSearchState.pageSize)" in js
    assert "SAVED_SEARCH_SNAPSHOT_MAX_RESULTS" not in js  # server owns this rejection


def test_local_pagination_has_no_fetch_or_sorting() -> None:
    js = _asset("app.js")
    region = js[
        js.index("function renderSavedSearchSnapshot") : js.index(
            "function selectSavedSearch"
        )
    ]
    assert "items.slice(start" in region
    assert "fetch(" not in region
    assert ".sort(" not in region
    assert "groupBy" not in region
    assert "rerank" not in region.lower()


def test_rank_profile_reuses_memory_only_matching_preferences() -> None:
    html, js = _asset("index.html"), _asset("app.js")
    assert "saved-search-profile-form" not in html + js
    assert "renderProfileSummary(profileSummary, matchingActiveProfile)" in js
    assert 'href: "#matching"' in js
    assert "freezeProfile(retryRun && retryRun.profile" in js
    assert "matchingActiveProfileGeneration" in js
    assert "snapshot.profileGeneration !== matchingActiveProfileGeneration" in js
    assert (
        "localStorage" not in js
        and "sessionStorage" not in js
        and "document.cookie" not in js
    )


def test_stale_selection_execution_and_navigation_guards() -> None:
    js = _asset("app.js")
    assert "metadataSeq" in js
    assert "selectionGeneration" in js
    assert "executionSeq" in js
    assert "visibleView() !== SAVED_SEARCHES" in js
    assert "if (seq === savedSearchState.executionSeq)" in js
    assert "savedSearchState.executionSeq++" in js


def test_retry_reuses_definition_version_and_validates_returned_definition() -> None:
    js = _asset("app.js")
    region = js[
        js.index("async function runSavedSearch") : js.index(
            "async function renderSavedSearchesView"
        )
    ]
    assert "definition.updated_at" in region
    assert "used.id !== definition.id" in region
    assert "used.name !== definition.name" in region
    assert "used.updated_at !== definition.updated_at" in region
    assert "error.savedSearchChanged === true" in region
    assert "data.mode !== used.mode" in region
    assert "data.grouped !== used.grouped" in region
    assert "data.limit !== used.result_limit" in region
    assert "data.total !== data.items.length" in region
    assert "Refresh its metadata before running it again" in region
    assert "loadSavedSearchMetadata" in region
    assert "runSavedSearch(definition, captured)" in region


def test_job_detail_context_supports_saved_search_and_back_link() -> None:
    js = _asset("app.js")
    assert 'jobDetailOrigin === SAVED_SEARCHES ? "Back to saved searches"' in js
    assert 'surface === "saved-search"' in js
    assert "savedSearchId" in js and "savedSearchUpdatedAt" in js
    assert "ranked saved search" in js
    assert "No ranking context is available" in js


def test_private_values_and_unsafe_dom_sinks_never_enter_frontend() -> None:
    combined = _asset("index.html") + _asset("app.js") + _asset("style.css")
    lowered = combined.lower()
    for forbidden in (
        "profile_path",
        "localstorage",
        "sessionstorage",
        "document.cookie",
        ".innerhtml",
        "insertadjacenthtml",
        "document.write",
        ".outerhtml",
    ):
        assert forbidden not in lowered
    assert "textContent" in combined and "createElement" in combined


def test_external_url_sanitization_and_same_origin_job_routes() -> None:
    js = _asset("app.js")
    assert 'parsed.protocol !== "http:" && parsed.protocol !== "https:"' in js
    assert 'href: "#job/" + job.id' in js
    assert 'target: "_blank"' in js
    assert 'rel: "noopener noreferrer"' in js


def test_accessibility_and_responsive_workspace_contract() -> None:
    html, css, js = _asset("index.html"), _asset("style.css"), _asset("app.js")
    assert 'aria-live="polite"' in html
    assert 'setAttribute("aria-busy", "true")' in js
    assert 'className: "saved-search-list"' in js
    assert ".saved-search-layout" in css
    assert "@media (max-width: 600px)" in css
    assert ".saved-search-layout { grid-template-columns: 1fr; }" in css


def test_no_resource_management_or_r2_m3_controls() -> None:
    html, js = _asset("index.html"), _asset("app.js")
    combined = html + js
    for label in (
        "Create Saved Search",
        "Edit Saved Search",
        "Delete Saved Search",
        "Rename Saved Search",
        "Duplicate Saved Search",
        "run history",
    ):
        assert label not in combined


def test_saved_search_request_uses_encoded_name_and_safe_definition_allowlist() -> None:
    js = _asset("app.js")
    assert "encodeURIComponent(definition.name)" in js
    metadata = js[
        js.index("function savedSearchFields") : js.index("function renderSavedResult")
    ]
    for field in (
        "id",
        "name",
        "mode",
        "query",
        "grouped",
        "result_limit",
        "source",
        "remote",
        "location",
        "untracked",
        "include_statuses",
        "exclude_statuses",
        "min_score",
    ):
        assert field in metadata
    assert "profile_path" not in metadata


def test_static_assets_contain_no_external_dependencies() -> None:
    html, css, js = _asset("index.html"), _asset("style.css"), _asset("app.js")
    assert not re.search(r"https?://", html + css + js)
    assert 'src="/ui/app.js"' in html
    assert 'href="/ui/style.css"' in html
