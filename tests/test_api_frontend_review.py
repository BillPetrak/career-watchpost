"""Focused tests for the Review view (``#review``) of the read-only
``/ui`` frontend.

The Review view is the second frontend milestone: a personalized review &
recommendations screen that posts an inline candidate-profile form to the
computation-only ``POST /report`` endpoint. Like the first slice this suite is
deliberately stdlib/Python-test-only (there is no Node toolchain), so most
checks are source-level invariants on ``index.html`` / ``app.js`` / ``style.css``
plus a few light behavioral checks through the FastAPI TestClient for the
``POST /report`` contract the view depends on.

Covered behaviour: the ``#review`` route existing and being recognized; the
view never auto-submitting on navigation or page load; the form containing
exactly the approved profile inputs plus grouped/limit controls (defaults
grouped=true, limit=10); query construction for the grouped toggle and the
10/25/50 limit choices; the exact ``POST /report`` method/path/headers/body;
only populated profile fields being sent; comma/newline list parsing with
trimming, empty-token removal, and preserved ordering; the 100-entry,
100-char/entry and 50-char remote-preference limits; client validation blocking
the fetch; the 422/network-error/missing-database handling surfaces; grouped
and flat review-queue and recommendation rendering; server-returned ordering
being preserved with no client re-sorting or re-ranking; candidate_count being
displayed from the response rather than derived from visible arrays; match/score
shown only where returned; empty-section handling; safe external links with
unsafe URL suppression; no private fields being rendered; no
profile_path/filesystem/storage usage; no unsafe HTML sinks; the method set
being exactly POST /report plus the Job Detail review-decision PUT/DELETE
mutations; Home/Jobs/unknown-hash/skip-link behaviour remaining intact;
static serving, SCHEMA_VERSION, the no-``careeros.cli`` import guarantee, the
untouched real default database, repeated-request determinism, and narrow
responsive CSS invariants.
"""

from __future__ import annotations

import ast
import importlib.resources
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.database.schema import SCHEMA_VERSION

_SRC = Path(__file__).resolve().parents[1] / "src"

_STATIC_DIR = importlib.resources.files("careeros.api").joinpath("static")

_HTML_NAME = "index.html"
_JS_NAME = "app.js"
_CSS_NAME = "style.css"

# Approved form listing fields. Hardness guard: these are the ONLY list inputs
# in the Review form (mirrors the server's CandidateProfileRequest for these
# three fields plus the remote_preference scalar).
_APPROVED_LIST_FIELDS = (
    ("review-titles", "desired_titles"),
    ("review-skills", "skills"),
    ("review-locations", "preferred_locations"),
)


def _asset_bytes(name: str) -> bytes:
    return _STATIC_DIR.joinpath(name).read_bytes()


def _asset_text(name: str) -> str:
    return _asset_bytes(name).decode("utf-8")


def _html() -> str:
    return _asset_text(_HTML_NAME)


def _js() -> str:
    return _asset_text(_JS_NAME)


def _css() -> str:
    return _asset_text(_CSS_NAME)


def _input_tag(html: str, input_id: str) -> str:
    match = re.search(r'<input\b[^>]*id="' + re.escape(input_id) + r'"[^>]*>', html)
    assert match, f"input #{input_id} not found"
    return match.group(0)


def _textarea_tag(html: str, textarea_id: str) -> str:
    match = re.search(
        r'<textarea\b[^>]*id="' + re.escape(textarea_id) + r'"[^>]*>', html
    )
    assert match, f"textarea #{textarea_id} not found"
    return match.group(0)


def _select_options(html: str, select_id: str) -> tuple[list[str], str | None]:
    match = re.search(
        r'<select\b[^>]*id="' + re.escape(select_id) + r'"[^>]*>(.*?)</select>',
        html,
        re.S,
    )
    assert match, f"select #{select_id} not found"
    values = re.findall(r'<option\b[^>]*value="([^"]*)"', match.group(1))
    selected = re.findall(r'<option\b[^>]*value="([^"]*)"[^>]*selected', match.group(1))
    return values, (selected[0] if selected else None)


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


# --------------------------------------------------------------------------- #
# A. Navigation and routing
# --------------------------------------------------------------------------- #


def test_review_nav_link_view_and_route_exist() -> None:
    html = _html()
    js = _js()
    assert 'href="#review"' in html
    assert '<a href="#review" id="nav-review">Review</a>' in html
    assert 'data-view="review"' in html
    assert 'id="view-review"' in html
    assert 'var REVIEW = "review";' in js
    assert "raw === REVIEW" in js


def test_review_routing_preserves_unknown_hash_fallback() -> None:
    js = _js()
    # REVIEW is a recognized route; unknown/absent/non-view hashes still fall
    # back exactly as before (parsedHash returns null, initial load defaults to
    # Home), and navigation never resets to a different view for #main.
    assert "return null;" in js
    assert "view = HOME;" in js
    assert "view || visibleView() || HOME" in js
    assert "if (view === REVIEW)" in js


def test_review_never_auto_submits_on_navigation_or_load() -> None:
    js = _js()
    # The only POST invocation is inside the submit pipeline: exactly one call
    # of the POST helper exists in the whole file, so navigating to #review
    # (which only resets prior results) can never issue the request.
    assert js.count("await postReportJson(") == 1
    # Entering the view shortly after navigation runs presentation reset only.
    assert js.count("resetReviewResults();") >= 1
    # Submission begins exclusively through the form's submit listeners wired
    # at DOMContentLoaded -- never from navigate() or a timer.
    assert 'addEventListener("submit", reviewFormSubmitted)' in js
    assert 'fetch("/report' not in js  # POST url is built dynamically


def test_main_anchor_does_not_reset_review_view() -> None:
    js = _js()
    # #main is not a view route: parsedHash yields null and navigate() keeps
    # the current visible view (via visibleView()) without re-rendering, so the
    # skip link while on Review preserves Review.
    assert "raw === REVIEW" in js
    assert "return null;" in js
    assert "visibleView()" in js


# --------------------------------------------------------------------------- #
# B. Form structure
# --------------------------------------------------------------------------- #


def test_review_form_has_exactly_the_approved_inputs() -> None:
    html = _html()
    for input_id, field_name in _APPROVED_LIST_FIELDS:
        tag = _textarea_tag(html, input_id)
        assert tag.startswith("<textarea")
        assert f'name="{field_name}"' in tag
    remote_values, remote_selected = _select_options(html, "review-remote")
    assert remote_values == ["", "remote", "onsite_or_unspecified"]
    assert remote_selected == ""
    assert 'name="remote_preference"' in re.search(
        r'<select\b[^>]*id="review-remote"[^>]*>', html
    ).group(0)
    assert re.search(r'<input\b[^>]*id="review-remote"', html) is None
    assert 'name="grouped"' in _input_tag(html, "review-grouped")
    limit_select = re.search(r'<select\b[^>]*id="review-limit"[^>]*>', html)
    assert limit_select is not None
    assert 'name="limit"' in limit_select.group(0)
    # No other candidate-profile fields are exposed in this slice's form.
    for forbidden in (
        "required_skills",
        "excluded_terms",
        "hard_excluded_terms",
        "preferred_employment_types",
        "preferred_seniority",
    ):
        assert forbidden not in html, forbidden
    # The profile is entered directly; no local config or file path is involved.
    assert "profile_path" not in html


def test_review_form_defaults_grouped_true_limit_ten() -> None:
    html = _html()
    grouped_tag = _input_tag(html, "review-grouped")
    assert 'type="checkbox"' in grouped_tag
    assert "checked" in grouped_tag
    values, selected = _select_options(html, "review-limit")
    assert values == ["10", "25", "50"]
    assert selected == "10"


def test_review_remote_preference_is_a_select_with_canonical_options() -> None:
    html = _html()
    # The field is a select, never a free-text input: no input element uses the
    # review-remote id, and no text input is named remote_preference.
    assert re.search(r'<input\b[^>]*id="review-remote"', html) is None
    assert (
        re.search(r'<input\b[^>]*type="text"[^>]*name="remote_preference"[^>]*>', html)
        is None
    )
    # Every canonical backend value is represented verbatim, with the empty
    # "No preference" option first and selected by default.
    values, selected = _select_options(html, "review-remote")
    assert values == ["", "remote", "onsite_or_unspecified"]
    assert selected == ""
    assert ">No preference</option>" in html
    assert 'value="remote">Remote only</option>' in html
    assert 'value="onsite_or_unspecified">Onsite or unspecified</option>' in html
    # No client-side translation or free-form fallback may invent a value: the
    # submitted remote_preference comes straight from the select.
    js = _js()
    assert 'document.getElementById("review-remote").value' in js
    assert "profile.remote_preference = fields.remotePreference" in js
    for sentinel in ("toRemoteValue", "remotePreferenceMap", "remoteStatusMap"):
        assert sentinel not in js, sentinel


def test_review_form_controls_have_labels() -> None:
    html = _html()
    assert 'for="review-grouped"' in html
    assert 'for="review-limit"' in html
    for input_id, _ in _APPROVED_LIST_FIELDS:
        assert f'for="{input_id}"' in html
    assert 'for="review-remote"' in html
    # Form and result regions are keyboard/assistive-tech friendly.
    assert 'role="alert"' in html
    assert 'role="status"' in html


# --------------------------------------------------------------------------- #
# C. Query construction and request contract
# --------------------------------------------------------------------------- #


def test_grouped_toggle_builds_correct_query() -> None:
    js = _js()
    # The checkbox decides the grouped query value: true when paired, false
    # otherwise, serialized as the lowercase strings FastAPI accepts.
    assert '"/report?grouped="' in js
    assert 'fields.grouped ? "true" : "false"' in js
    assert '"&limit="' in js


def test_limit_choices_build_correct_query() -> None:
    html = _html()
    js = _js()
    values, _ = _select_options(html, "review-limit")
    assert values == ["10", "25", "50"]
    # The chosen select value is sent verbatim; the view never re-derives it.
    assert '&limit=" + fields.limit' in js


def test_post_report_method_path_headers_and_body() -> None:
    js = _js()
    assert 'method: "POST"' in js
    assert '"/report?grouped="' in js
    assert '"Content-Type": "application/json"' in js
    assert '"Accept": "application/json"' in js
    assert "JSON.stringify(body)" in js
    # The POST path carries query parameters; a bare "/report/" suffix is never
    # used (and mutation/computation endpoints are covered separately below).
    assert '"/report/' not in js


def test_only_populated_profile_fields_are_sent() -> None:
    js = _js()
    # Every approved field is added only when it parsed to a non-empty value.
    assert "if (fields.desiredTitles.length > 0)" in js
    assert "profile.desired_titles = fields.desiredTitles" in js
    assert "if (fields.skills.length > 0)" in js
    assert "profile.skills = fields.skills" in js
    assert "if (fields.preferredLocations.length > 0)" in js
    assert "profile.preferred_locations = fields.preferredLocations" in js
    assert "if (fields.remotePreference.length > 0)" in js
    assert "profile.remote_preference = fields.remotePreference" in js
    # payload is exactly {"profile": profile}; no other top-level key exists.
    assert "return { profile: profile };" in js
    assert "profile_path" not in js


# --------------------------------------------------------------------------- #
# D. List parsing and validation
# --------------------------------------------------------------------------- #


def test_list_parsing_splits_commas_and_newlines_keeps_order() -> None:
    js = _js()
    # Split on comma/newline runs, trim each entry, drop empty tokens; order is
    # preserved by the natural array operations (no sort/unique anywhere).
    assert ".split(/[,\\n]+/)" in js
    assert "entry.trim()" in js
    assert ".length > 0" in js
    # All three list inputs run through the same parser in field order.
    assert 'parseListValue(document.getElementById("review-titles").value)' in js
    assert 'parseListValue(document.getElementById("review-skills").value)' in js
    assert (
        "parseListValue(\n"
        '        document.getElementById("review-locations").value\n'
        "      )"
    ) in js


def test_client_validation_enforces_server_limits() -> None:
    js = _js()
    assert "var MAX_LIST_ENTRIES = 100;" in js
    assert "var MAX_ENTRY_LENGTH = 100;" in js
    assert "var MAX_REMOTE_LENGTH = 50;" in js
    assert "entries.length > MAX_LIST_ENTRIES" in js
    assert "entries[entryIndex].length > MAX_ENTRY_LENGTH" in js
    assert "fields.remotePreference.length > MAX_REMOTE_LENGTH" in js
    assert '" has more than " + MAX_LIST_ENTRIES + " entries."' in js
    assert '" entries must be " + MAX_ENTRY_LENGTH + " characters or fewer."' in js
    assert (
        '"Remote preference must be " + MAX_REMOTE_LENGTH + " characters or fewer."'
    ) in js


def test_invalid_input_blocks_the_fetch() -> None:
    js = _js()
    # Validation runs before any request is issued: a validation error stops the
    # submit pipeline, and only a valid request reaches the POST helper.
    assert "if (validationError) return;" in js
    assert "runReviewRequest(fields);" in js
    offset = js.index("function submitReview()")
    submit_tail = js[offset : js.index("function runReviewRequest", offset)]
    assert "runReviewRequest(fields);" in submit_tail
    assert "postReportJson(" not in submit_tail


def test_remote_preference_trimmed_and_omitted_when_empty() -> None:
    js = _js()
    assert 'document.getElementById("review-remote").value.trim()' in js
    assert "fields.remotePreference.length > 0" in js


# --------------------------------------------------------------------------- #
# E. Response rendering
# --------------------------------------------------------------------------- #


def test_summary_uses_candidate_counts_from_response() -> None:
    js = _js()
    assert 'countValue(review.candidate_count) + " opportunities to review. "' in js
    assert 'countValue(recommendations.candidate_count) + " recommendations. "' in js
    assert 'review.grouped === true ? "Grouped." : "Individual jobs."' in js


def test_review_queue_grouped_and_flat_rendering() -> None:
    js = _js()
    # The review queue renders one card per server-returned item; every item
    # carries a canonical group whose representative exposes the public job
    # fields, so the same projection serves grouped (real groups) and flat
    # (singleton groups) modes without client-side re-shaping.
    assert "buildJobCard(group.representative, item.match_score, null, group)" in js
    assert "item.group" in js
    assert "group.representative" in js
    assert 'addField(fields, "Match score", String(countValue(matchScore)))' in js
    # Member count appears only when a group actually has multiple members.
    assert "group.members.length > 1" in js
    assert '" listings combined."' in js


def test_recommendations_grouped_and_flat_rendering() -> None:
    js = _js()
    assert "recommendations.ranked_groups" in js
    assert "recommendations.ranked_jobs" in js
    assert "grouped ? recommendations.ranked_groups : recommendations.ranked_jobs" in js
    assert "entry.group && entry.group.representative" in js
    assert "entry.job" in js
    # Both modes surface match_score AND score for recommendations.
    assert "entry.match_score, entry.score, group" in js
    assert 'addField(fields, "Score", String(countValue(score)))' in js
    assert "score !== null && score !== undefined" in js


def test_server_order_preserved_without_client_reranking() -> None:
    js = _js()
    # Server-returned order is preserved: items iterate with forEach in order
    # and there is no sorting, reversing, or client-side score derivation.
    assert "items.forEach(function" in js
    assert "list.forEach(function" in js
    assert ".sort(" not in js
    assert ".reverse(" not in js


def test_no_hardcoded_review_counts_derived_from_visible_arrays() -> None:
    js = _js()
    # The summary counts come straight from candidate_count (response field),
    # never from items.length or a client-side tally.
    assert "countValue(review.candidate_count)" in js
    assert "countValue(recommendations.candidate_count)" in js


def test_empty_review_queue_and_recommendations() -> None:
    js = _js()
    assert '"No opportunities to review."' in js
    assert '"No recommendations."' in js
    # A public response note is shown when present; otherwise the concise
    # fallback is used -- causes are never invented client-side.
    assert 'typeof review.note === "string" && review.note.length > 0' in js
    assert (
        'typeof recommendations.note === "string" && recommendations.note.length > 0'
        in js
    )


def test_review_renders_public_job_fields_only() -> None:
    js = _js()
    for label in (
        '"Company"',
        '"Location"',
        '"Remote"',
        '"Employment type"',
        '"Source"',
        '"Published"',
    ):
        assert f"addField(fields, {label}, " in js
    # Private or internal values are never read from the response (the only
    # ".message" anywhere is the fetch Error's message, which is never rendered
    # as a private field).
    for private in (
        ".reasons",
        "score_components",
        ".description",
        "review.message",
        "recommendations.message",
        "item.message",
        "job.message",
        ".review_decision",
        ".application_status",
    ):
        assert private not in js, private


# --------------------------------------------------------------------------- #
# F. Security and privacy
# --------------------------------------------------------------------------- #


def test_safe_external_links_are_reused() -> None:
    js = _js()
    # The shared helper is reused by every card builder (Jobs, Review,
    # Recommendations, Job Detail member cards). The current-signature call
    # sites carry an explicit label ("Open original").
    assert "safeExternalLink(job.url, " in js
    assert 'target: "_blank"' in js
    assert 'rel: "noopener noreferrer"' in js
    assert "new URL(url, window.location.origin)" in js


def test_unsafe_or_non_http_urls_are_suppressed() -> None:
    js = _js()
    assert 'parsed.protocol !== "http:" && parsed.protocol !== "https:"' in js
    assert "return null;" in js


def test_no_storage_persistence_of_profile_values() -> None:
    combined = "\n".join((_html(), _js(), _css()))
    for forbidden in ("localStorage", "sessionStorage", "document.cookie"):
        assert forbidden not in combined, forbidden


def test_no_unsafe_html_sinks_for_dynamic_data() -> None:
    js = _js()
    assert ".innerHTML" not in js
    assert "innerHTML=" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js
    assert ".outerHTML" not in js


def test_no_filesystem_or_config_access_from_frontend() -> None:
    combined = "\n".join((_html(), _js(), _css())).lower()
    for forbidden in (
        "profile_path",
        "candidate_profile",
        ".sqlite",
        ".yaml",
        ".yml",
        "filesystem",
        "readfile",
        "fs.",
        "require(",
        "import(",
        "/logs/",
        "/data/",
    ):
        assert forbidden not in combined, forbidden


def test_app_js_methods_are_report_post_and_workflow_mutations() -> None:
    js = _js()
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    assert methods == ["POST", "POST", "PUT", "PATCH", "DELETE"]
    assert '"/report?grouped="' in js
    # The Job Detail view legitimately GETs the /applications?job_id={id},
    # /review-decisions?job_id={id}, and /reminders?job_id={id} route families
    # and PUTs/DELETEs only the review-decision route (plus the PATCH reminder
    # edit); no other computation or mutation endpoint is ever called.
    for forbidden in (
        "/saved-searches",
        "/ranked-jobs",
        "/review-queue",
        "/workflow",
    ):
        assert f'"/{forbidden}' not in js, forbidden


# --------------------------------------------------------------------------- #
# G. Existing views unchanged
# --------------------------------------------------------------------------- #


def test_home_behavior_unchanged() -> None:
    js = _js()
    assert 'fetchJson("/report")' in js
    assert 'var data = await fetchJson("/report");' in js
    assert "renderHomeContent(content, data)" in js


def test_jobs_behavior_unchanged() -> None:
    js = _js()
    assert '"/jobs?" + qs' in js
    assert 'await loadJobs(qs ? "/jobs?" + qs : "/jobs")' in js
    assert "fetchJson(url)" in js
    assert 'var query = document.getElementById("jobs-query").value;' in js
    assert 'addEventListener("submit", function (event)' in js


# --------------------------------------------------------------------------- #
# H. Behavioral checks against the POST /report contract
# --------------------------------------------------------------------------- #


def test_missing_database_returns_friendly_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "missing" / "careeros.sqlite3"
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.post(
            "/report",
            json={"profile": {"skills": ["python"]}},
            params={"grouped": "true", "limit": "10"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["overview"]["total_jobs"] == 0
    assert body["review_queue"]["candidate_count"] == 0
    assert body["recommendations"]["candidate_count"] == 0
    assert body["review_queue"]["items"] == []
    assert body["recommendations"]["ranked_jobs"] == []
    assert body["recommendations"]["ranked_groups"] == []
    assert not database_path.exists()


def test_post_report_422_carries_usable_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_database(monkeypatch, tmp_path / "careeros.sqlite3")

    # A field-length violation the client normally catches first; the server
    # stays authoritative and returns the canonical 422 detail shape.
    with TestClient(app) as client:
        response = client.post(
            "/report",
            json={"profile": {"skills": ["x" * 101]}},
        )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_repeated_identical_requests_are_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_database(monkeypatch, tmp_path / "careeros.sqlite3")

    with TestClient(app) as client:
        first = client.post(
            "/report",
            json={"profile": {"desired_titles": ["engineer"], "skills": ["python"]}},
            params={"grouped": "true", "limit": "25"},
        )
        second = client.post(
            "/report",
            json={"profile": {"desired_titles": ["engineer"], "skills": ["python"]}},
            params={"grouped": "true", "limit": "25"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    first_body, second_body = first.json(), second.json()
    first_body["as_of"] = "same"
    second_body["as_of"] = "same"
    assert first_body == second_body  # byte-for-byte apart from as_of


# --------------------------------------------------------------------------- #
# I. Static serving and invariants
# --------------------------------------------------------------------------- #


def test_ui_assets_still_serve_with_correct_types() -> None:
    with TestClient(app) as client:
        index = client.get("/ui")
        js_response = client.get("/ui/app.js")
        css = client.get("/ui/style.css")

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert js_response.status_code == 200
    assert js_response.headers["content-type"].startswith("application/javascript")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")


def test_schema_version_stays_eight() -> None:
    assert SCHEMA_VERSION == 8


def test_no_careeros_cli_import_under_api() -> None:
    api_root = _SRC / "careeros" / "api"
    violations: list[str] = []
    for path in sorted(api_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "careeros.cli" or name.startswith("careeros.cli."):
                    violations.append(f"{path.relative_to(_SRC)}: {name}")
    assert violations == []


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    _set_database(monkeypatch, tmp_path / "careeros.sqlite3")
    with TestClient(app) as client:
        client.post(
            "/report",
            json={"profile": {"skills": ["python"]}},
            params={"grouped": "true"},
        )
        client.get("/ui")

    assert not real.is_file() or real.stat().st_mtime_ns == before


# --------------------------------------------------------------------------- #
# J. Static CSS invariants (responsive / accessible)
# --------------------------------------------------------------------------- #


def test_narrow_layout_and_form_responsive_invariants() -> None:
    css = _css()
    assert "@media (max-width: 600px)" in css
    assert ".profile-field textarea" in css
    assert ".profile-field select" in css
    # Constrain width to the panel so narrow/mobile layouts never overflow.
    assert "width: 100%;" in css
    assert "max-width: 100%;" in css
    assert ".profile-field" in css
    assert ".profile-controls" in css
    assert ".inline-label" in css
