"""Static contract checks for the Applications pipeline workspace."""

from __future__ import annotations

import importlib.resources

_STATIC = importlib.resources.files("careeros.api").joinpath("static")
_APPLICATIONS_END = (
    "// ---------------------------------------------------------------------------\n"
    "  // Reminders overview"
)


def _asset(name: str) -> str:
    return _STATIC.joinpath(name).read_text()


def test_applications_route_and_workspace_shell_exist() -> None:
    html = _asset("index.html")
    js = _asset("app.js")
    assert 'href="#applications"' in html
    assert 'id="view-applications"' in html
    assert 'id="applications-content"' in html
    assert 'var APPLICATIONS = "applications";' in js
    assert '"/application-pipeline?view="' in js


def test_pipeline_contract_and_flat_concrete_ownership() -> None:
    js = _asset("app.js")
    assert "items[*]" not in js
    assert "item.job_id" in js
    assert "item.current_status" in js
    assert "item.created_at" in js
    assert "item.updated_at" in js
    assert "applicationCounts(data)" in js
    assert "data.total" in js
    assert "data.offset" in js
    assert "data.limit" in js
    applications_start = js.index("function renderApplicationsView")
    applications_end = js.index(_APPLICATIONS_END, applications_start)
    assert "grouped" not in js[applications_start:applications_end]


def test_application_filters_are_server_authoritative_and_paginated() -> None:
    js = _asset("app.js")
    start = js.index("var APPLICATION_VIEWS")
    end = js.index("function setPipelineActionsDisabled", start)
    section = js[start:end]
    for value in (
        "active",
        "completed",
        "all",
        "interested",
        "applied",
        "interview",
        "offer",
    ):
        assert 'value: "' + value + '"' in section
    assert "applicationsState.offset = 0" in section
    assert "Previous" in section and "Next" in section
    assert "updated_at" in section
    assert "created_at" in section
    assert "application-pipeline" in section


def test_pipeline_mutation_is_put_then_narrow_refresh_without_optimism() -> None:
    js = _asset("app.js")
    start = js.index("async function submitPipelineApplication")
    end = js.index(_APPLICATIONS_END, start)
    mutation = js[start:end]
    assert 'putJson("/applications/" + jobId' in mutation
    assert "requestApplicationsRefresh();" in mutation
    assert "renderApplicationsView" not in mutation
    assert "/report" not in mutation
    assert "/review" not in mutation
    assert "/reminders" not in mutation
    assert "applicationsCardInFlight[jobId]" in mutation
    assert "if (applicationsCardInFlight[jobId] === operation)" in mutation


def test_applications_have_independent_lifetime_and_origin_guards() -> None:
    js = _asset("app.js")
    assert "applicationsViewGeneration" in js
    assert "applicationsRequestSeq" in js
    assert "applicationsRefreshDirty" in js
    assert "applicationsRefreshOwner" in js
    assert "applicationsOriginState" in js
    assert "leaving === APPLICATIONS" in js
    assert "Back to applications" in js
    assert "jobDetailOrigin = leaving" in js
    assert "applicationsFocusTarget = null" in js
    assert "restoreApplicationsFocus" in js
    assert "applicationsState.offset = Math.max(0" in js


def test_applications_use_safe_dom_and_accessible_controls() -> None:
    combined = _asset("index.html") + _asset("app.js") + _asset("style.css")
    assert ".innerHTML" not in combined
    assert "textContent" in combined
    js = _asset("app.js")
    start = js.index("function renderApplicationsContent")
    end = js.index("function setPipelineActionsDisabled", start)
    section = js[start:end]
    assert 'setAttribute("role", "group")' in section
    assert 'setAttribute("aria-pressed"' in section
    assert 'type: "button"' in section


def test_pipeline_retry_keeps_put_and_get_failures_distinct() -> None:
    js = _asset("app.js")
    start = js.index("async function submitPipelineApplication")
    end = js.index(_APPLICATIONS_END, start)
    section = js[start:end]
    assert "await putJson" in section
    assert "requestApplicationsRefresh();" in section
    # Refresh retry is owned by renderApplicationsView and must not replay PUT.
    render = js[js.index("function renderApplicationsView") : start]
    assert 'refreshing ? "Application status saved' in render
    assert "renderApplicationsView({ refresh: refreshing })" in render


def test_application_workspace_static_assets_remain_packaged() -> None:
    assert _asset("index.html")
    assert _asset("app.js")
    assert _asset("style.css")
