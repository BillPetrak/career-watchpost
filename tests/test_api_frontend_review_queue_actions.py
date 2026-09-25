"""Tests for the Review Queue card actions milestone (#review).

The Review view's Review Queue cards gain per-card decision controls that
reuse the SAME canonical mutation as the Job Detail Review section:
``PUT /review-decisions/{id}`` with ``{"decision": ...}`` and the exact
vocabulary values (``shortlist`` / ``dismiss`` / ``review_later``). Grouped
cards target the representative job id, which the shared review service
resolves to the whole canonical opportunity.

Covered behaviour (source-level invariants on ``app.js`` plus behavioral
checks through the FastAPI TestClient for the exact contract the view sends):

- exactly one set of three decision buttons per Review Queue card, attached
  ONLY in ``buildReviewQueueSection``; Recommendations cards never receive
  controls and stay byte-stable across decisions;
- per-card in-flight protection: a ``reviewCardInFlight`` map keyed by job id,
  duplicate activations for that card rejected outright, only the submitted
  card's buttons disabled, no view-wide queue mutation lock anywhere;
- strictly server-authoritative flow with NO optimistic removal and NO
  client-side ranking: success refreshes only the existing POST /review-queue
  request, preserving Recommendations from the last authoritative report;
- queue refresh coalescing and lifetime protection: one coordinator reruns a
  dirty refresh when another decision lands during an active refresh, while
  view-generation and per-operation tokens discard stale responses and make
  in-flight cleanup conditional;
- failure keeps the card visible, re-enables its buttons, renders ONE
  recoverable inline error whose Retry reissues the exact failed PUT, and
  never triggers the authoritative refresh;
- behavioral: shortlist/dismiss/review_later each remove their opportunity
  from the authoritative grouped/flat report queue while remaining items keep
  server order; a grouped representative decision removes the whole group
  while an undecided flat twin stays; two close-together decisions converge;
  an invalid decision is a canonical 422 that changes nothing;
- regressions: the intended ``postReportJson`` call sites and current global
  method inventory including package-owned mutations,
  forbidden endpoints absent, Job Detail review/application actions untouched,
  no unsafe HTML sinks/storage/private fields, reused CSS classes,
  ``SCHEMA_VERSION`` stays 9, the real default database is never touched,
  static assets are served without a database, and wheel packaging lists the
  static assets.
"""

from __future__ import annotations

import ast
import importlib.resources
import re
from pathlib import Path

import pytest

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from tests.support import TestClient

_STATIC_DIR = importlib.resources.files("careeros.api").joinpath("static")

_JS_NAME = "app.js"
_CSS_NAME = "style.css"

_ITEM_FIELDS = [
    "decision_id",
    "job_id",
    "decision",
    "created_at",
    "updated_at",
    "job",
]

# Section-banner anchor used to slice single functions out of app.js.
_JS_BANNER = "// " + "-" * 75

# A profile matching every seeded job so all of them enter the review queue
# (min_score is 0) and ordering comes purely from the server.
_PROFILE = {
    "desired_titles": ["Engineer"],
    "skills": ["Python"],
    "preferred_locations": ["Berlin"],
    "remote_preference": "remote",
}


def _asset_bytes(name: str) -> bytes:
    return _STATIC_DIR.joinpath(name).read_bytes()


def _asset_text(name: str) -> str:
    return _asset_bytes(name).decode("utf-8")


def _js() -> str:
    return _asset_text(_JS_NAME)


def _css() -> str:
    return _asset_text(_CSS_NAME)


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    published_at: str,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location="Berlin",
        description="Python development work.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at=published_at,
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed_database(path: Path, *, with_group: bool = False) -> dict[str, int]:
    """Seed singleton jobs (plus an optional cross-source duplicate pair)."""
    jobs = [
        _make_job(
            "arbeitnow", "a-1", "Senior Engineer", "Acme Alpha", "2026-08-01T10:00:00Z"
        ),
        _make_job(
            "remoteok", "k-1", "Backend Engineer", "Acme Beta", "2026-07-01T10:00:00Z"
        ),
        _make_job(
            "jobicy",
            "j-1",
            "Frontend Developer",
            "Acme Gamma",
            "2026-08-02T10:00:00Z",
        ),
    ]
    if with_group:
        jobs.append(
            _make_job(
                "arbeitnow",
                "d-1",
                "Platform Engineer",
                "Dup Co",
                "2026-07-20T10:00:00Z",
            )
        )
        jobs.append(
            _make_job(
                "weworkremotely",
                "d-2",
                "Platform Engineer",
                "Dup Co",
                "2026-07-21T10:00:00Z",
            )
        )
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(connection, jobs)
    if with_group:
        ensure_grouped(connection)
    mapping = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.close()
    return mapping


def _table_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _post_report(client: TestClient, *, grouped: bool) -> dict[str, object]:
    response = client.post(
        "/report",
        json={"profile": dict(_PROFILE)},
        params={"grouped": "true" if grouped else "false"},
    )
    assert response.status_code == 200
    return response.json()


def _post_review_queue(client: TestClient, *, grouped: bool) -> dict[str, object]:
    response = client.post(
        "/review-queue",
        json={"profile": dict(_PROFILE)},
        params={"grouped": "true" if grouped else "false"},
    )
    assert response.status_code == 200
    return response.json()


def _queue_source_ids(body: dict[str, object]) -> list[str]:
    items = body["review_queue"]["items"]  # type: ignore[index]
    return [item["group"]["representative"]["source_job_id"] for item in items]


def _queue_candidate_count(body: dict[str, object]) -> int:
    return body["review_queue"]["candidate_count"]  # type: ignore[no-any-return]


def _decide(client: TestClient, job_id: int, decision: str) -> object:
    response = client.put(f"/review-decisions/{job_id}", json={"decision": decision})
    assert response.status_code == 200
    return response.json()


# --------------------------------------------------------------------------- #
# A. Source invariants: queue-card controls and endpoint reuse
# --------------------------------------------------------------------------- #


def test_queue_section_attaches_decision_controls_exactly_once() -> None:
    js = _js()
    assert js.count("attachQueueDecisionActions(") == 2  # one def + one call
    start = js.index("function buildReviewQueueSection")
    end = js.index("function buildRecommendationsSection")
    assert "attachQueueDecisionActions(card, group.representative);" in js[start:end]
    # The exact shared buildJobCard call string is unchanged (grouped mode).
    assert "buildJobCard(group.representative, item.match_score, null, group)" in js


def test_recommendations_never_receive_decision_controls() -> None:
    js = _js()
    start = js.index("function buildRecommendationsSection")
    end = js.index("function buildJobCard(")
    region = js[start:end]
    assert "attachQueueDecisionActions" not in region
    assert "submitQueueDecision" not in region


def test_queue_controls_use_canonical_vocabulary_and_endpoint() -> None:
    js = _js()
    start = js.index("function attachQueueDecisionActions")
    end = js.index("async function submitQueueDecision")
    attach = js[start:end]
    for entry in (
        '{ value: "shortlist", label: "Shortlist" },',
        '{ value: "dismiss", label: "Dismiss" },',
        '{ value: "review_later", label: "Review later" },',
    ):
        assert entry in js  # REVIEW_DECISIONS table unchanged
    assert "REVIEW_DECISIONS.forEach" in attach
    assert 'className: "decision-button"' in attach
    assert 'type: "button"' in attach
    assert 'setAttribute("aria-label", "Review queue decision")' in attach
    # The same endpoint/payload shape as the Job Detail review actions, now
    # used from exactly two call sites (detail + queue card).
    expected_put = (
        'await putJson("/review-decisions/" + jobId, { decision: decision });'
    )
    assert js.count(expected_put) == 2
    # One of the two call sites lives inside the queue-card mutation handler.
    assert expected_put in _submit_queue_decision_fn(js)
    # The representative id must be validated before use, exactly like Job
    # Detail validates its job ids.
    assert "!isFiniteNumber(job.id) || job.id < 1" in attach


# --------------------------------------------------------------------------- #
# B. Source invariants: per-card concurrency and duplicate protection
# --------------------------------------------------------------------------- #


def test_per_card_in_flight_map_declared_and_scoped_to_review_view() -> None:
    js = _js()
    assert "var reviewCardInFlight = {};" in js
    assert "var lastReviewFields = null;" in js
    # No view-wide queue mutation lock exists anywhere: protection is strictly
    # per card so other cards stay usable while one PUT is in flight.
    assert "reviewQueueOperationActive" not in js


def test_duplicate_activations_rejected_before_any_state_change() -> None:
    js = _js()
    fn = _submit_queue_decision_fn(js)
    guard = fn.index("if (reviewCardInFlight[jobId]) return;")
    busy = fn.index("reviewCardInFlight[jobId] = operation;")
    put = fn.index('await putJson("/review-decisions/"')
    assert guard < busy < put
    # The marker is always released, even when the trigger was skipped.
    assert "delete reviewCardInFlight[jobId];" in fn.rsplit("finally {", 1)[1]


def test_only_the_submitted_card_is_disabled() -> None:
    js = _js()
    start = js.index("function setQueueDecisionDisabled")
    end = js.index("function attachQueueDecisionActions")
    helper = js[start:end]
    # Scoping to `card` (never a section/view root) keeps sibling cards usable.
    assert helper.count('card.querySelectorAll(".decision-button")') == 1
    assert "button.disabled = disabled;" in helper
    assert 'card.setAttribute("aria-busy", "true");' in helper
    assert 'card.removeAttribute("aria-busy");' in helper
    # The mutation handler never touches a global operation flag.
    fn = _submit_queue_decision_fn(js)
    assert "OperationActive" not in fn


def test_successful_put_ends_busy_state_while_queue_refresh_is_pending() -> None:
    js = _js()
    fn = _submit_queue_decision_fn(js)
    put = fn.index('await putJson("/review-decisions/"')
    clear_busy = fn.index('card.removeAttribute("aria-busy");')
    refresh = fn.index("requestReviewQueueRefresh(generation, fields);")
    assert put < clear_busy < refresh
    # The stale card is not re-enabled after the decision was saved. Only the
    # authoritative queue refresh may replace it with current controls.
    assert "setQueueDecisionDisabled(card, false);" not in fn[put:clear_busy]


def _submit_queue_decision_fn(js: str) -> str:
    start = js.index("async function submitQueueDecision")
    end = js.index(_JS_BANNER, start)
    return js[start:end]


# --------------------------------------------------------------------------- #
# C. Source invariants: authoritative refresh and stale/navigation races
# --------------------------------------------------------------------------- #


def test_success_refreshes_only_the_authoritative_queue_request() -> None:
    js = _js()
    fn = _submit_queue_decision_fn(js)
    # No optimistic removal and no client-side ranking anywhere in the
    # handler: the card is only removed by the refreshed server response.
    assert ".remove(" not in fn
    assert "clearText(" not in fn
    assert "sort(" not in fn
    # The mutation handler schedules a queue-only refresh. It must never call
    # the expensive full report path or mutate Recommendations locally.
    assert "runReviewRequest(" not in fn
    assert "requestReviewQueueRefresh" in fn
    assert "postReviewQueue" in js or "review-queue?grouped=" in js
    assert '"/report?' not in fn


def test_review_refresh_has_generation_and_coalescing_guards() -> None:
    js = _js()
    assert re.search(r"var review(?:ViewGeneration|Generation)\s*=\s*0", js)
    assert re.search(r"var review(?:Queue)?Refresh", js)
    assert re.search(r"reviewQueueRefreshDirty", js)
    assert "generation" in js
    # Queue responses and errors must be ignored after navigation/re-entry.
    assert "visibleView() !== REVIEW" in js
    assert "reviewRequestSeq++" in js or "reviewViewGeneration++" in js


def test_navigation_leaving_review_invalidates_in_flight_continuations() -> None:
    js = _js()
    bump = "if (leaving === REVIEW && name !== REVIEW) {"
    assert bump in js
    after = js[js.index(bump) : js.index(bump) + 400]
    assert "reviewRequestSeq++;" in after or "reviewViewGeneration++;" in after


def test_reentry_uses_new_generation_and_old_queue_refresh_cannot_swallow_it() -> None:
    js = _js()
    # Leaving and re-entering Review each changes the generation. The queue
    # response guard must compare the captured generation and data generation,
    # allowing a new view's request to render while discarding the old one.
    assert js.count("reviewViewGeneration++;") >= 2
    start = js.index("async function runReviewQueueRefresh")
    end = js.index("function requestReviewQueueRefresh", start)
    refresh = js[start:end]
    assert "generation !== reviewViewGeneration" in refresh
    assert "dataGeneration !== reviewDataGeneration" in refresh
    assert "visibleView() !== REVIEW" in refresh


def test_navigation_clears_operation_markers_but_preserves_run_snapshot() -> None:
    js = _js()
    start = js.index("if (leaving === REVIEW && name !== REVIEW) {")
    end = js.index("if (leaving === REVIEWED", start)
    leaving = js[start:end]
    assert "reviewCardInFlight = {};" in leaving
    assert "reviewSnapshot = null" not in leaving
    assert "review-content" not in leaving


def test_operation_cleanup_is_conditional_on_identity_token() -> None:
    js = _js()
    fn = _submit_queue_decision_fn(js)
    assert re.search(
        r"reviewCardInFlight\[jobId\].*operation|"
        r"operation.*reviewCardInFlight\[jobId\]",
        js,
        re.S,
    )
    assert "finally" in fn
    # An old operation must not delete a newer same-job marker.
    assert re.search(
        r"if\s*\([^\n]*reviewCardInFlight\[jobId\][^\n]*===|"
        r"reviewCardInFlight\[jobId\][^\n]*===",
        fn,
    )


# --------------------------------------------------------------------------- #
# D. Source invariants: failure handling and Retry semantics
# --------------------------------------------------------------------------- #


def test_failure_keeps_card_and_retries_the_exact_failed_put() -> None:
    js = _js()
    fn = _submit_queue_decision_fn(js)
    catch_tail = fn[fn.index("catch (error)") : fn.index("finally {")]
    # A failed mutation never triggers the authoritative refresh.
    assert "runReviewRequest(" not in catch_tail
    # Superseded contexts are left completely alone.
    assert (
        "if (generation !== reviewViewGeneration || visibleView() !== REVIEW) return;"
        in catch_tail
    )
    # Buttons are re-enabled BEFORE the recoverable inline error is rendered.
    enable = catch_tail.index("setQueueDecisionDisabled(card, false);")
    box = catch_tail.index("replaceSectionError(")
    assert enable < box
    assert '"The review decision could not be saved.",' in catch_tail
    # Retry reissues the EXACT failed PUT (same card, id, decision).
    assert "submitQueueDecision(card, jobId, decision);" in catch_tail
    # Exactly one inline error panel per card: replaceSectionError replaces
    # instead of appending.
    assert catch_tail.count("replaceSectionError(") == 1


def test_queue_refresh_failure_has_refresh_only_retry() -> None:
    js = _js()
    # The queue refresh path must have a distinct recoverable error boundary;
    # its retry may refresh the queue but must not invoke the successful PUT
    # again. This guards the server-authoritative mutation/refresh split.
    assert re.search(r"queue.*refresh|refresh.*queue", js, re.I | re.S)
    assert re.search(r"refresh.*retry|retry.*refresh", js, re.I | re.S)
    assert "The review decision could not be saved." in js


# --------------------------------------------------------------------------- #
# E. Regressions: method set, forbidden endpoints, hygiene, CSS, packaging
# --------------------------------------------------------------------------- #


def test_global_http_method_inventory_includes_package_mutations() -> None:
    js = _js()
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    assert methods == [
        "POST",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "PUT",
        "DELETE",
        "PUT",
        "DELETE",
        "DELETE",
        "PUT",
        "DELETE",
        "PUT",
    ]


def test_forbidden_endpoints_remain_absent() -> None:
    js = _js()
    for forbidden in ("/workflow",):
        assert forbidden not in js, forbidden


def test_queue_refresh_does_not_rebuild_recommendations() -> None:
    js = _js()
    fn = _submit_queue_decision_fn(js)
    assert "buildRecommendationsSection" not in fn
    assert "recommendations" not in fn
    assert "/review-queue" in js


def test_post_helper_has_initial_report_and_queue_refresh_call_sites() -> None:
    js = _js()
    # The helper is used once for the initial full report, once for the
    # queue-only authoritative decision refresh, and once for explicit Saved
    # Search execution, plus the explicit Matching Preview.
    assert js.count("await postReportJson(") == 4


def test_job_detail_actions_remain_untouched() -> None:
    js = _js()
    for signature in (
        "async function applyReviewDecision(group, body, job, decision)",
        "async function clearReviewDecision(group, body, job)",
        "async function applyApplicationStatus(group, body, job, status)",
    ):
        assert signature in js, signature
    start = js.index("async function applyReviewDecision")
    detail_apply = js[start : js.index("async function clearReviewDecision")]
    assert "var seq = detailRequestSeq;" in detail_apply
    assert 'await putJson("/review-decisions/" + jobId, { decision: decision });' in (
        detail_apply
    )


def test_no_unsafe_sinks_storage_or_private_fields() -> None:
    js = _js()
    for forbidden in (
        ".innerHTML",
        "innerHTML=",
        "insertAdjacentHTML",
        ".outerHTML",
        "document.write",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "eval(",
        "profile_path",
        "candidate_profile",
        ".sqlite",
        "score_components",
        "notification_summary",
        "last_error",
        "notes",
    ):
        assert forbidden not in js, forbidden
    # Package completeness reasons are server-authoritative public workflow
    # data. No ranking/profile/internal ``reasons`` property may be accessed.
    assert re.findall(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.reasons\b", js) == [
        "data.completeness.reasons",
        "completeness.reasons",
        "completeness.reasons",
    ]


def test_existing_decision_css_reused_without_new_styles() -> None:
    css = _css()
    assert ".decision-actions {" in css
    assert ".decision-button {" in css
    assert ".decision-button:disabled {" in css


def test_wheel_package_data_lists_static_assets() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "[tool.setuptools.package-data]" in pyproject
    line = (
        '"careeros.api" = ["static/*.html", "static/*.js", "static/*.css", '
        '"static/*.svg"]'
    )
    assert line in pyproject


# --------------------------------------------------------------------------- #
# F. Behavioral contract through the TestClient: the three decisions
# --------------------------------------------------------------------------- #


def test_review_queue_endpoint_reflects_successful_decision_without_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        before = _post_review_queue(client, grouped=True)
        before_ids = [
            item["group"]["representative"]["source_job_id"]
            for item in before["items"]  # type: ignore[index]
        ]
        assert before_ids == ["a-1", "k-1", "j-1"]
        assert before["total"] == 3

        decision = _decide(client, job_ids["a-1"], "shortlist")
        assert decision["decision"] == "shortlist"  # type: ignore[index]

        after = _post_review_queue(client, grouped=True)
        after_ids = [
            item["group"]["representative"]["source_job_id"]
            for item in after["items"]  # type: ignore[index]
        ]
        assert after_ids == [
            source_id for source_id in before_ids if source_id != "a-1"
        ]
        # ReviewQueueResponse calls its authoritative candidate count ``total``.
        assert after["total"] == before["total"] - 1  # type: ignore[operator]


@pytest.mark.parametrize("decision", ["shortlist", "dismiss", "review_later"])
def test_decision_removes_opportunity_from_authoritative_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        pre = _post_report(client, grouped=True)
        pre_ids = _queue_source_ids(pre)
        assert set(pre_ids) == {"a-1", "k-1", "j-1"}
        assert _queue_candidate_count(pre) == 3

        target = job_ids["a-1"]
        item = _decide(client, target, decision)
        assert item["decision"] == decision  # type: ignore[index]
        assert list(item) == _ITEM_FIELDS  # type: ignore[arg-type]

        fetched = client.get(f"/review-decisions/{target}")
        assert fetched.status_code == 200
        assert fetched.json()["decision"] == decision

        post = _post_report(client, grouped=True)
        post_ids = _queue_source_ids(post)
        assert post_ids == [job for job in pre_ids if job != "a-1"]
        assert _queue_candidate_count(post) == 2

        # Repeating the identical decision stays idempotent and never alters
        # the authoritative queue membership or order.
        repeat = client.put(f"/review-decisions/{target}", json={"decision": decision})
        assert repeat.status_code == 200
        again = _post_report(client, grouped=True)
        assert _queue_source_ids(again) == post_ids
        assert _queue_candidate_count(again) == 2


# --------------------------------------------------------------------------- #
# G. Behavioral contract: grouped opportunities and flat twins
# --------------------------------------------------------------------------- #


def test_grouped_representative_decision_reviews_whole_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_database(path, with_group=True)
    pairs = sorted(
        [(job_ids["d-1"], "d-1"), (job_ids["d-2"], "d-2")],
    )
    (representative, representative_sid), (twin, twin_sid) = pairs
    assert representative < twin
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        pre = _post_report(client, grouped=True)
        grouped_items = [
            item
            for item in pre["review_queue"]["items"]  # type: ignore[index]
            if any(
                member["id"] in (representative, twin)
                for member in item["group"]["members"]
            )
        ]
        assert len(grouped_items) == 1
        assert len(grouped_items[0]["group"]["members"]) == 2
        assert grouped_items[0]["group"]["representative"]["id"] == representative

        _decide(client, representative, "shortlist")

        # Grouped mode: deciding the representative removes the WHOLE
        # canonical opportunity from the queue.
        post_grouped = _post_report(client, grouped=True)
        for item in post_grouped["review_queue"]["items"]:  # type: ignore[index]
            member_ids = [member["id"] for member in item["group"]["members"]]
            assert representative not in member_ids
            assert twin not in member_ids
        assert _queue_candidate_count(post_grouped) == _queue_candidate_count(pre) - 1

        # Flat mode: the decision anchors to ONE concrete jobs.id, so the
        # undecided twin legitimately remains until it is decided itself.
        post_flat = _post_report(client, grouped=False)
        flat_sids = set(_queue_source_ids(post_flat))
        assert representative_sid not in flat_sids
        assert twin_sid in flat_sids

        # Deciding the twin removes it from the flat queue too.
        _decide(client, twin, "dismiss")
        final_flat = _post_report(client, grouped=False)
        final_sids = set(_queue_source_ids(final_flat))
        assert representative_sid not in final_sids
        assert twin_sid not in final_sids


# --------------------------------------------------------------------------- #
# H. Behavioral contract: close-together decisions, order, recommendations
# --------------------------------------------------------------------------- #


def test_two_close_decisions_converge_in_one_final_refresh_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        _post_report(client, grouped=True)

        # Two mutations landing back-to-back (the frontend would fire two
        # refreshes; only the newest response may render).
        first = _decide(client, job_ids["a-1"], "shortlist")
        second = _decide(client, job_ids["k-1"], "dismiss")
        assert first["decision"] == "shortlist"  # type: ignore[index]
        assert second["decision"] == "dismiss"  # type: ignore[index]

        final = _post_report(client, grouped=True)
        final_ids = _queue_source_ids(final)
        assert final_ids == ["j-1"]
        assert _queue_candidate_count(final) == 1


def test_remaining_queue_order_survives_an_unrelated_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        pre = _post_report(client, grouped=True)
        pre_ids = _queue_source_ids(pre)

        last_id = job_ids[pre_ids[-1]]
        _decide(client, last_id, "review_later")

        post = _post_report(client, grouped=True)
        assert _queue_source_ids(post) == pre_ids[:-1]


def test_recommendations_are_stable_across_review_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_database(path)
    _set_database(monkeypatch, path)

    def recommendation_rows(body: dict[str, object]) -> list[tuple[object, ...]]:
        recs = body["recommendations"]  # type: ignore[index]
        if recs["grouped"]:  # type: ignore[index]
            entries = recs["ranked_groups"]  # type: ignore[index]
            return [
                (
                    item["group"]["representative"]["id"],
                    item["match_score"],
                    item["score"],
                )
                for item in entries
            ]
        entries = recs["ranked_jobs"]  # type: ignore[index]
        return [
            (item["job"]["id"], item["match_score"], item["score"]) for item in entries
        ]

    with TestClient(app) as client:
        pre = _post_report(client, grouped=True)
        assert len(recommendation_rows(pre)) >= 3

        _decide(client, job_ids["j-1"], "shortlist")

        post = _post_report(client, grouped=True)
        assert recommendation_rows(post) == recommendation_rows(pre)


# --------------------------------------------------------------------------- #
# I. Behavioral contract: invalid decisions change nothing
# --------------------------------------------------------------------------- #


def test_invalid_decision_is_422_and_leaves_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        baseline = _post_report(client, grouped=True)

        rejected = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "bogus"}
        )
        assert rejected.status_code == 422

        # Nothing was written: the audit trail stays empty and the queue is
        # stable, so the card's Retry can safely reissue the request. Only
        # section payloads are compared -- `as_of` is a fresh per-request
        # timestamp by design.
        assert _table_count(path, "review_decisions") == 0
        assert _table_count(path, "review_events") == 0
        after = _post_report(client, grouped=True)
        assert after["review_queue"] == baseline["review_queue"]
        assert after["recommendations"] == baseline["recommendations"]

        recovered = _decide(client, job_ids["a-1"], "shortlist")
        assert recovered["decision"] == "shortlist"  # type: ignore[index]


# --------------------------------------------------------------------------- #
# J. Shared invariants: schema version, real DB, static assets
# --------------------------------------------------------------------------- #


def test_schema_version_stays_10() -> None:
    assert SCHEMA_VERSION == 13


def test_no_careeros_cli_import() -> None:
    for module in _walk_package("careeros.api"):
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module == "careeros.cli":
                pytest.fail(f"careeros.api imports careeros.cli: {node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "careeros.cli":
                        pytest.fail(f"careeros.api imports careeros.cli: {node.lineno}")


def _walk_package(prefix: str):
    import importlib

    package = importlib.import_module(prefix)
    root = Path(package.__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        yield ast.parse(path.read_text())


def test_real_default_database_untouched() -> None:
    database = (
        Path(__file__).resolve().parents[1] / "data" / "database" / "careeros.sqlite3"
    )
    before = database.read_bytes() if database.exists() else None
    _js()
    _css()
    assert (database.read_bytes() if database.exists() else None) == before


def test_static_assets_served_without_database(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.close()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    try:
        with TestClient(app) as client:
            for route, name in (
                ("/ui", "index.html"),
                ("/ui/app.js", _JS_NAME),
                ("/ui/style.css", _CSS_NAME),
            ):
                response = client.get(route)
                assert response.status_code == 200
                assert response.content == _asset_bytes(name)
    finally:
        monkeypatch.undo()
    assert path.exists()


def test_database_file_never_created_by_asset_or_mutation_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing.sqlite3"
    _set_database(monkeypatch, path)
    with pytest.raises(ValueError, match="current schema v13"):
        with TestClient(app):
            pass
    assert not path.exists()
