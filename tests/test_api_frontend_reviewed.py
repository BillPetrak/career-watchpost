"""Static contract checks for the Reviewed opportunities workspace."""

from __future__ import annotations

import importlib.resources

_STATIC = importlib.resources.files("careeros.api").joinpath("static")
_BANNER = "// " + "-" * 75


def _asset(name: str) -> str:
    return _STATIC.joinpath(name).read_text()


def test_reviewed_route_and_workspace_shell_are_present() -> None:
    html = _asset("index.html")
    js = _asset("app.js")
    assert 'href="#reviewed"' in html
    assert 'id="view-reviewed"' in html
    assert 'id="reviewed-content"' in html
    assert 'var REVIEWED = "reviewed";' in js
    assert '"/reviewed-opportunities?decision="' in js


def test_reviewed_view_is_server_authoritative_and_uses_owner_id() -> None:
    js = _asset("app.js")
    assert "decision_job_id" in js
    assert "fetchJson(reviewedUrl(state))" in js
    assert "reviewedCounts(data)" in js
    assert "requestReviewedRefresh" in js
    assert "localStorage" not in js
    assert "sessionStorage" not in js
    assert ".innerHTML" not in js
    owner_start = js.index("function reviewedItemOwnerId")
    owner_end = js.index("function reviewedCounts", owner_start)
    owner_fn = js[owner_start:owner_end]
    assert "return isFiniteNumber(owner) && owner >= 1" in owner_fn
    assert "job.id" not in owner_fn


def test_reviewed_actions_do_not_rebuild_other_workspaces() -> None:
    js = _asset("app.js")
    start = js.index("async function submitReviewedMutation")
    end = js.index(_BANNER, start)
    mutation = js[start:end]
    assert '"/review-decisions/"' in mutation
    assert "requestReviewedRefresh();" in mutation
    assert "runReviewRequest" not in mutation
    assert "/report" not in mutation
    assert "/review-queue" not in mutation
