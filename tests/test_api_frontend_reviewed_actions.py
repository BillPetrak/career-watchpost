"""Concurrency and safe-rendering invariants for Reviewed actions."""

from __future__ import annotations

import importlib.resources
import re

_JS = importlib.resources.files("careeros.api").joinpath("static", "app.js").read_text()
_BANNER = "// " + "-" * 75


def test_reviewed_has_independent_lifetime_and_operation_tokens() -> None:
    assert "var reviewedViewGeneration = 0;" in _JS
    assert "var reviewedRequestSeq = 0;" in _JS
    assert "var reviewedDataGeneration = 0;" in _JS
    assert "var reviewedCardInFlight = {};" in _JS
    assert "var reviewedOperationSeq = 0;" in _JS
    assert "reviewedCardInFlight[jobId] === operation" in _JS


def test_reviewed_refresh_coalesces_and_reentry_invalidates() -> None:
    assert "var reviewedRefreshActive = false;" in _JS
    assert "var reviewedRefreshDirty = false;" in _JS
    assert "reviewedRefreshOwner === owner" in _JS
    assert "reviewedRequestSeq++;" in _JS
    assert "reviewedViewGeneration++;" in _JS
    assert re.search(r"generation !== reviewedViewGeneration", _JS)
    assert re.search(r"seq !== reviewedRequestSeq", _JS)


def test_reviewed_mutation_has_distinct_refresh_retry_and_no_optimism() -> None:
    start = _JS.index("async function submitReviewedMutation")
    end = _JS.index(_BANNER, start)
    fn = _JS[start:end]
    assert (
        "if (reviewedCardInFlight[jobId] || visibleView() !== REVIEWED) return;" in fn
    )
    assert "setReviewedDecisionDisabled(card, true);" in fn
    assert "replaceSectionError" in fn
    assert ".remove()" not in fn
    assert "clearText(" not in fn
    assert "finally" in fn
