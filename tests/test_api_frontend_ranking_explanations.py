"""Source contracts for the R2 M1 ranked explanation UI."""

from __future__ import annotations

import importlib.resources

STATIC = importlib.resources.files("careeros.api").joinpath("static")


def _asset(name: str) -> str:
    return STATIC.joinpath(name).read_text()


def test_ranked_cards_use_native_disclosure_and_server_explanation() -> None:
    js = _asset("app.js")
    assert 'el("details"' in js
    assert 'textContent: "Why this match?"' in js
    assert "appendCardExplanation" in js
    assert "entry.explanation" in js
    assert "item.explanation" in js
    assert "score_components" not in js
    # Preparation has its own readiness reasons; keep the ranking-card slice
    # prohibited from accessing/rendering raw reasons.
    ranking_slice = js.split("// Preparation is an independently loaded", 1)[0]
    assert "reasons" not in ranking_slice


def test_job_detail_requires_ephemeral_exact_ranked_transition() -> None:
    js = _asset("app.js")
    assert "rankingContextGeneration" in js
    assert "pendingRankedNavigation" in js
    assert "validJobExplanationContext" in js
    assert 'textContent: "Why this job?"' in js
    assert "No ranking context is available." in js
    assert "localStorage" not in js
    assert "sessionStorage" not in js
    assert "document.cookie" not in js


def test_ranked_context_carries_surface_kind_generation_and_members() -> None:
    js = _asset("app.js")
    for field in (
        "generation",
        "surface",
        "targetKind",
        "targetIdentity",
        "memberIds",
        "explanation",
    ):
        assert field in js
    assert 'targetKind: "opportunity"' in js
    assert 'group["group_id"]' in js
    assert '"group:" + group["group_id"]' in js
    assert '"job:" + job.id' in js
    assert 'scope === "opportunity"' in js


def test_explanation_rendering_is_safe_and_responsive() -> None:
    js = _asset("app.js")
    css = _asset("style.css")
    assert ".innerHTML" not in js
    assert "innerHTML=" not in js
    assert "textContent" in js
    assert "summary:focus-visible" in css
    assert "@media (max-width: 600px)" in css


def test_mobile_header_navigation_wraps_without_horizontal_overflow() -> None:
    css = _asset("style.css")
    assert ".site-header .nav" in css
    assert "flex: 1 1 100%" in css
    assert "flex-wrap: wrap" in css
    assert "min-width: 0" in css
