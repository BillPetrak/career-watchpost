"""Tests for the Job Detail Review-decision actions milestone.

The Job Detail view (`#job/<id>`) gains Review-decision action controls that
let the user set, change, or clear a stored job's review decision through the
existing, fully-tested backend mutations ``PUT /review-decisions/{id}`` and
``DELETE /review-decisions/{id}``. This suite is deliberately
stdlib/Python-test-only (there is no Node toolchain), so most checks are
source-level invariants on ``app.js`` / ``style.css`` plus behavioral checks
through the FastAPI TestClient for the exact mutation contract the view sends.

Covered behaviour: the three canonical decisions (`shortlist`, `dismiss`,
`review_later`) with presentation labels and no client-side value translation;
the current-decision visual state (``.current`` class + ``aria-pressed``);
the Clear decision control present only when reviewed; a strictly
server-authoritative flow with NO optimistic updates (the PUT/DELETE response
drives a re-fetch of ONLY the job-scoped list read
``GET /review-decisions?job_id={id}`` and a re-render of only the Review
section, so the normal absent state is an empty 200 list rather than a
canonical 404); controls disabled + section ``aria-busy`` during a
mutation with a ``reviewOperationActive`` guard so rapid clicks cannot issue
overlapping mutations; stale-response protection via a per-navigation
``detailRequestSeq`` token plus ``isCurrentDetailJob`` so a response for an old
job can never overwrite the visible one; mutation failures (404/422/500/503)
rendering a section error box that retries the SAME operation while preserving
the previously rendered review state; a DELETE followed by an empty list
re-fetch rendering "Not reviewed." rather than an error; PUT/DELETE being the
only newly permitted mutation methods with every forbidden endpoint absent; no
storage APIs, unsafe HTML sinks, or private field/path access; Home/Jobs/
Review and the existing read-only detail sections unchanged; the shared method
set invariant ``["POST", "POST", "PUT", "DELETE"]``; static serving, ``SCHEMA_VERSION``,
the no-``careeros.cli`` import guarantee, the untouched real default database,
and wheel packaging of the static assets.
"""

from __future__ import annotations

import ast
import importlib.resources
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import lock_path_for
from careeros.review.service import set_decision

_SRC = Path(__file__).resolve().parents[1] / "src"

_STATIC_DIR = importlib.resources.files("careeros.api").joinpath("static")

_JS_NAME = "app.js"
_CSS_NAME = "style.css"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

_ITEM_FIELDS = [
    "decision_id",
    "job_id",
    "decision",
    "created_at",
    "updated_at",
    "job",
]

_CLEAR_FIELDS = ["job_id", "changed"]


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
    location: str,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed_jobs(path: Path) -> dict[str, int]:
    """Seed three distinct jobs and return a source-job-id to jobs.id mapping."""
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("remoteok", "b-1", "Data Scientist", "Other Co", "Munich"),
            _make_job("jobicy", "c-1", "Frontend Developer", "Frontend Labs", "Berlin"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.close()
    return job_ids


def _review_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --------------------------------------------------------------------------- #
# Child-process lock helper (cross-process flock semantics)
# --------------------------------------------------------------------------- #

_CHILD_HOLD = """\
import sys
from pathlib import Path
from careeros.locking import collection_lock
database = Path(sys.argv[1])
with collection_lock(database, command="collect due"):
    print("LOCKED", flush=True)
    sys.stdin.readline()
"""


def _src_on_pythonpath() -> str:
    existing = os.environ.get("PYTHONPATH")
    return str(_SRC) + (os.pathsep + existing if existing else "")


def _start_child(database: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = _src_on_pythonpath()
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLD, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_lock_line(child: subprocess.Popen) -> None:
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if line != "LOCKED":
        assert child.stderr is not None
        pytest.fail(f"child did not acquire lock: {child.stderr.read()}")


def _release_child(child: subprocess.Popen) -> None:
    assert child.stdin is not None
    child.stdin.write("release\n")
    child.stdin.close()
    child.wait(timeout=10)


# --------------------------------------------------------------------------- #
# A. Decision action controls and labels
# --------------------------------------------------------------------------- #


def test_three_canonical_decisions_with_presentation_labels() -> None:
    js = _js()
    assert "var REVIEW_DECISIONS = [" in js
    assert '{ value: "shortlist", label: "Shortlist" },' in js
    assert '{ value: "dismiss", label: "Dismiss" },' in js
    assert '{ value: "review_later", label: "Review later" },' in js
    # The values sent over the wire must be the exact backend vocabulary, never
    # a client-side translation.
    assert 'putJson("/review-decisions/" + jobId, { decision: decision });' in js
    assert 'deleteJson("/review-decisions/" + jobId);' in js


def test_review_section_builds_decision_controls() -> None:
    js = _js()
    assert "function renderReview(body, job, settled, retry)" in js
    assert "function renderReviewDecisionSection(body, job, review)" in js
    assert "function buildReviewDecisionActions(body, job, review)" in js
    assert 'textContent: "Not reviewed."' in js
    assert 'addField(fields, "Decision", review.decision);' in js
    assert 'addField(fields, "Decided", displayDate(review.created_at));' in js
    assert 'addField(fields, "Last updated", displayDate(review.updated_at));' in js
    # Decision buttons carry a friendly label plus the hidden canonical value.
    assert 'className: "decision-button",' in js
    assert 'type: "button",' in js
    assert "textContent: entry.label," in js
    assert "applyReviewDecision(group, body, job, entry.value);" in js


def test_current_decision_visually_highlighted() -> None:
    js = _js()
    assert "review !== null && review.decision === entry.value" in js
    assert 'button.classList.add("current");' in js
    assert 'button.setAttribute("aria-pressed", "true");' in js


def test_clear_decision_control_only_when_reviewed() -> None:
    js = _js()
    assert 'textContent: "Clear decision",' in js
    assert "clearReviewDecision(group, body, job);" in js
    # The clear control must only be appended when a current decision exists.
    start = js.index("function buildReviewDecisionActions")
    end = js.index("function setReviewActionsDisabled", start)
    builder = js[start:end]
    assert "if (review !== null) {" in builder
    assert builder.count("if (review !== null) {") == 1


# --------------------------------------------------------------------------- #
# B. Server-authoritative flow: no optimistic updates, refresh only Review
# --------------------------------------------------------------------------- #


def test_apply_is_server_authoritative_with_no_optimistic_update() -> None:
    js = _js()
    start = js.index("async function applyReviewDecision")
    end = js.index("async function clearReviewDecision", start)
    apply = js[start:end]
    assert (
        'await putJson("/review-decisions/" + jobId, { decision: decision });' in apply
    )
    assert "await refreshReviewDecision(body, job, seq);" in apply
    # The re-fetch response drives the re-render; nothing is cleared optimistically.
    assert "clearText(body)" not in apply
    assert "fetchDetailSection" not in apply


def test_clear_is_server_authoritative_with_no_optimistic_update() -> None:
    js = _js()
    start = js.index("async function clearReviewDecision")
    end = js.index("async function refreshReviewDecision", start)
    clear = js[start:end]
    assert 'await deleteJson("/review-decisions/" + jobId);' in clear
    assert "await refreshReviewDecision(body, job, seq);" in clear
    assert "clearText(body)" not in clear


def test_successful_mutation_refetches_only_review_section() -> None:
    js = _js()
    start = js.index("async function refreshReviewDecision")
    end = js.index("function renderReminders", start)
    refresh = js[start:end]
    # Only the job-scoped list GET /review-decisions?job_id={id} is re-fetched
    # and only the Review section re-renders; the whole Job Detail view is never
    # reloaded.
    assert 'var settled = await fetchDetailSection("review", jobId);' in refresh
    assert 'renderSectionBody(section, body, job, "review", settled);' in refresh
    assert "loadJobDetailData" not in refresh
    assert "renderJobDetail" not in refresh
    assert 'fetchDetailSection("group"' not in refresh
    assert 'fetchDetailSection("application"' not in refresh
    assert 'fetchDetailSection("reminders"' not in refresh


def test_delete_refetch_empty_list_renders_not_reviewed() -> None:
    js = _js()
    start = js.index("function renderReview(body, job, settled, retry)")
    end = js.index("function renderReviewDecisionSection", start)
    render = js[start:end]
    # After a DELETE the refetched job-scoped list is empty: the empty-items
    # branch renders the "Not reviewed." absent state, not an error panel, and
    # only a rejected (network/5xx) response falls through to the error box.
    assert "data.items.length === 0" in render
    assert "renderReviewDecisionSection(body, job, null);" in render
    assert "return;" in render
    # The empty-list branch renders the absent state and returns early, so
    # the tracked items[0] processing below it can never run for an empty
    # list.
    assert render.index("renderReviewDecisionSection(body, job, null);") < render.index(
        "var review = data.items[0];"
    )


# --------------------------------------------------------------------------- #
# C. In-flight disable, aria-busy, and no overlapping mutations
# --------------------------------------------------------------------------- #


def test_in_flight_state_prevents_overlapping_mutations() -> None:
    js = _js()
    assert "var reviewOperationActive = false;" in js
    assert "reviewOperationActive = true;" in js
    # Both apply and clear guard on the shared flag at entry.
    assert js.count("if (reviewOperationActive) return;") == 2
    for name in ("applyReviewDecision", "clearReviewDecision"):
        start = js.index(f"async function {name}")
        end = js.index("async function", start + len(name) + 30)
        body = js[start:end]
        assert "reviewOperationActive = true;" in body
        assert "setReviewActionsDisabled(group, true);" in body
        assert 'section.setAttribute("aria-busy", "true");' in body
        assert "reviewOperationActive = false;" in body


def test_review_section_busy_state_cleared_after_refresh() -> None:
    js = _js()
    start = js.index("async function refreshReviewDecision")
    end = js.index("function renderReminders", start)
    refresh = js[start:end]
    assert 'section.removeAttribute("aria-busy");' in refresh


# --------------------------------------------------------------------------- #
# D. Stale-response protection
# --------------------------------------------------------------------------- #


def test_render_job_detail_uses_seq_token_and_route_guard() -> None:
    js = _js()
    assert "var detailRequestSeq = 0;" in js
    start = js.index("async function renderJobDetail")
    end = js.index("function renderJobDetailContent", start)
    detail = js[start:end]
    assert "var seq = ++detailRequestSeq;" in detail
    assert "reviewOperationActive = false;" in detail
    assert "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;" in (
        detail
    )


def test_is_current_detail_job_compares_parsed_route() -> None:
    js = _js()
    assert "function isCurrentDetailJob(jobId)" in js
    assert "return parsedJobId() === jobId;" in js


def test_section_retry_and_refresh_guard_against_stale_responses() -> None:
    js = _js()
    # retrySection preserves the existing fetch line and guards its render.
    start = js.index("async function retrySection")
    end = js.index("function sectionErrorBox", start)
    retry = js[start:end]
    assert "var settled = await fetchDetailSection(sectionKey, job.id);" in retry
    assert "if (seq !== detailRequestSeq || !isCurrentDetailJob(job.id)) return;" in (
        retry
    )
    # refreshReviewDecision guards on the captured seq + route before rendering.
    start = js.index("async function refreshReviewDecision")
    end = js.index("function renderReminders", start)
    refresh = js[start:end]
    assert "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;" in (
        refresh
    )


# --------------------------------------------------------------------------- #
# E. Mutation-failure error handling
# --------------------------------------------------------------------------- #


def test_apply_failure_preserves_state_and_retries_put() -> None:
    js = _js()
    start = js.index("async function applyReviewDecision")
    end = js.index("async function clearReviewDecision", start)
    apply = js[start:end]
    assert "replaceSectionError(" in apply
    assert '"The review decision could not be saved."' in apply
    # Retry re-issues the failed PUT (never a full reload) and re-enables the
    # controls; the previously rendered review state is left in place.
    assert "applyReviewDecision(group, body, job, decision);" in apply
    assert "setReviewActionsDisabled(group, false);" in apply
    assert 'section.removeAttribute("aria-busy");' in apply
    assert "clearText(body)" not in apply
    # The mutation catch path must never append directly to the section body:
    # that is exactly what accumulated duplicate error panels.
    assert "body.appendChild(" not in apply


def test_clear_failure_preserves_state_and_retries_delete() -> None:
    js = _js()
    start = js.index("async function clearReviewDecision")
    end = js.index("async function refreshReviewDecision", start)
    clear = js[start:end]
    assert "replaceSectionError(" in clear
    assert '"The review decision could not be cleared."' in clear
    assert "clearReviewDecision(group, body, job);" in clear
    assert "setReviewActionsDisabled(group, false);" in clear
    assert 'section.removeAttribute("aria-busy");' in clear
    assert "clearText(body)" not in clear
    assert "body.appendChild(" not in clear


def test_write_json_propagates_http_status_via_read_http_error() -> None:
    js = _js()
    assert "async function writeJson(url, options)" in js
    assert "function putJson(url, body)" in js
    assert "function deleteJson(url)" in js
    assert "error.status = response.status;" in js


def test_repeated_review_mutation_failures_replace_not_append() -> None:
    """Regression: repeated failed mutations must replace the Review-section
    error panel, never accumulate duplicates (each section holds at most one)."""
    js = _js()
    # The reusable section-error boundary: replaceSectionError removes any
    # existing .error-box in the section body before appending the fresh one.
    assert "function replaceSectionError(body, message, error, retryAction)" in js
    start = js.index("function replaceSectionError")
    end = js.index("function renderOpportunity", start)
    helper = js[start:end]
    assert 'body.querySelectorAll(".error-box")' in helper
    assert "box.remove();" in helper
    assert "body.appendChild(sectionErrorBox(message, error, retryAction));" in helper
    # Exactly one definition plus one call in each mutation catch path (review
    # apply, review clear, application apply, reminder complete, reminder
    # reopen, reminder delete); the reminder create failure uses its own
    # non-replay panel.
    assert js.count("replaceSectionError(") == 7
    for name in ("applyReviewDecision", "clearReviewDecision"):
        start = js.index(f"async function {name}")
        end = js.index("async function", start + len(name) + 30)
        fn = js[start:end]
        assert "replaceSectionError(" in fn
        # The old append-only pattern is what accumulated duplicate panels.
        assert "body.appendChild(" not in fn
    # Successful recovery clears the error: the success path clears the section
    # body (removing any error box) before re-rendering canonical server state.
    start = js.index("async function refreshReviewDecision")
    end = js.index("function renderReminders", start)
    refresh = js[start:end]
    assert "clearText(body);" in refresh


# --------------------------------------------------------------------------- #
# F. Method set, forbidden endpoints, and hygiene
# --------------------------------------------------------------------------- #


def test_method_set_is_exactly_post_put_delete_in_order() -> None:
    js = _js()
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    # POST /report (Review view), POST /reminders (create), PUT (review/
    # application/reminder lifecycle), DELETE (review clear) -- in source order.
    assert methods == ["POST", "POST", "PUT", "DELETE"]


def test_forbidden_endpoints_remain_absent() -> None:
    js = _js()
    for forbidden in (
        "/saved-searches",
        "/ranked-jobs",
        "/review-queue",
        "/workflow",
        "/report/",
    ):
        assert f'"/{forbidden}' not in js, forbidden


def test_home_jobs_review_regressions_intact() -> None:
    js = _js()
    assert 'var data = await fetchJson("/report");' in js
    assert 'await loadJobs(qs ? "/jobs?" + qs : "/jobs");' in js
    assert js.count("await postReportJson(") == 1
    assert 'addEventListener("submit", reviewFormSubmitted);' in js


def test_read_only_detail_sections_unchanged() -> None:
    js = _js()
    for signature in (
        "function renderOpportunity(body, job, settled, retry)",
        "function buildMemberCard(member, currentJob)",
        "function renderApplication(body, job, settled, retry)",
        "function renderReminders(body, job, settled, retry)",
        "function buildCoreJobSection(job)",
        'base + "/group"',
        '"/applications?job_id=" + jobId',
        '"/review-decisions?job_id=" + jobId',
        '"/reminders?job_id=" + jobId',
    ):
        assert signature in js, signature


def test_no_private_fields_filesystem_profile_or_storage_access() -> None:
    js = _js()
    for forbidden in (
        ".innerHTML",
        "innerHTML=",
        "insertAdjacentHTML",
        ".outerHTML",
        "document.write",
        "localStorage",
        "sessionStorage",
        "profile_path",
        "candidate_profile",
        ".sqlite",
        "/data/",
        ".source_job_id",
        ".group_id",
        "score_components",
        ".reasons",
        "reminder.message",
        "notification_summary",
        "last_error",
        "notes",
    ):
        assert forbidden not in js, forbidden


def test_decision_actions_css_present() -> None:
    css = _css()
    assert ".decision-actions {" in css
    assert ".decision-button {" in css
    assert ".decision-button.current {" in css
    assert ".decision-button.clear-decision {" in css
    assert ".decision-button:disabled {" in css


# --------------------------------------------------------------------------- #
# G. Behavioral mutation contract through the TestClient
# --------------------------------------------------------------------------- #


def test_put_shortlist_reflects_in_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert list(payload) == _ITEM_FIELDS
        assert payload["decision"] == "shortlist"
        assert payload["job_id"] == job_ids["a-1"]

        fetched = client.get(f"/review-decisions/{job_ids['a-1']}")
        assert fetched.status_code == 200
        assert fetched.json()["decision"] == "shortlist"


def test_put_change_decision_updates_current_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        changed = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "dismiss"}
        )
        assert changed.status_code == 200
        assert changed.json()["decision"] == "dismiss"

        fetched = client.get(f"/review-decisions/{job_ids['a-1']}")
        assert fetched.json()["decision"] == "dismiss"

    assert _review_count(path, "review_events") == 2


def test_put_same_decision_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        second = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
        assert second.status_code == 200
        assert second.json() == first.json()

    assert _review_count(path, "review_events") == 1


def test_put_invalid_decision_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "bogus"}
        )

    assert response.status_code == 422
    assert (
        "Invalid review decision 'bogus'; expected one of: shortlist, dismiss, "
        "review_later." in response.text
    )
    assert _review_count(path, "review_decisions") == 0
    assert _review_count(path, "review_events") == 0


def test_put_unknown_job_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put("/review-decisions/9999", json={"decision": "dismiss"})

    assert response.status_code == 404
    assert "No job found with ID 9999." in response.text


def test_delete_after_decision_clears_and_get_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    connection = connect(path)
    initialize_schema(connection)
    set_decision(
        connection,
        job_id=job_ids["a-1"],
        decision="review_later",
        now=_EPOCH,
    )
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        deleted = client.delete(f"/review-decisions/{job_ids['a-1']}")
        assert deleted.status_code == 200
        assert list(deleted.json()) == _CLEAR_FIELDS
        assert deleted.json() == {"job_id": job_ids["a-1"], "changed": True}

        fetched = client.get(f"/review-decisions/{job_ids['a-1']}")
        assert fetched.status_code == 404

    assert _review_count(path, "review_decisions") == 0
    assert _review_count(path, "review_events") == 2


def test_delete_unreviewed_is_idempotent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.delete(f"/review-decisions/{job_ids['a-1']}")

    assert response.status_code == 200
    assert response.json() == {"job_id": job_ids["a-1"], "changed": False}
    assert _review_count(path, "review_events") == 0


# --------------------------------------------------------------------------- #
# H. Database-availability and lock-contention behaviour
# --------------------------------------------------------------------------- #


def test_missing_database_is_500_and_nothing_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "careeros.sqlite3"
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        put = client.put("/review-decisions/1", json={"decision": "shortlist"})
        delete = client.delete("/review-decisions/1")

    for response in (put, delete):
        assert response.status_code == 500
        assert "The database schema is unavailable" in response.text
    assert not path.exists()
    assert not path.parent.exists()
    assert not lock_path_for(path).exists()


def test_lock_contention_maps_to_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.put(
                f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
            )
        assert response.status_code == 503
        assert (
            "The database is temporarily busy; retry the request later."
            in response.text
        )
        assert _review_count(path, "review_decisions") == 0
    finally:
        _release_child(child)

    # The lock is released: the same mutation now succeeds.
    with TestClient(app) as client:
        retry = client.put(
            f"/review-decisions/{job_ids['a-1']}", json={"decision": "shortlist"}
        )
    assert retry.status_code == 200
    assert retry.json()["decision"] == "shortlist"


# --------------------------------------------------------------------------- #
# I. Shared invariants: schema version, no cli import, real DB, static assets
# --------------------------------------------------------------------------- #


def test_schema_version_stays_8() -> None:
    assert SCHEMA_VERSION == 8


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
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))
    with TestClient(app) as client:
        for route, name in (
            ("/ui", "index.html"),
            ("/ui/app.js", _JS_NAME),
            ("/ui/style.css", _CSS_NAME),
        ):
            response = client.get(route)
            assert response.status_code == 200
            assert response.content == _asset_bytes(name)
    monkeypatch.undo()
    assert not path.exists()
