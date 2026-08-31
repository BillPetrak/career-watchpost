"""Tests for the Job Detail Application-status actions milestone.

The Job Detail view (`#job/<id>`) gains Application-status action controls that
let the user assign or move a stored job's application status through the
existing, fully-tested backend mutation ``PUT /applications/{id}``. This suite
is deliberately stdlib/Python-test-only (there is no Node toolchain), so most
checks are source-level invariants on ``app.js`` / ``style.css`` plus
behavioral checks through the FastAPI TestClient for the exact mutation
contract the view sends.

Covered behaviour: the six canonical statuses (`interested`, `applied`,
`interview`, `offer`, `rejected`, `withdrawn`) in that exact order with
presentation labels and no client-side value translation; every status always
available (the backend permits any status -> any other status and the view must
never invent a linear state machine or disable a status based on the current
one); the current-status visual state (``.status-button.current`` +
``aria-pressed``); a strictly server-authoritative flow with NO optimistic
updates (the PUT response drives a re-fetch of ONLY the job-scoped list read
``GET /applications?job_id={id}`` and a re-render of only the Application
section, so the normal absent state is an empty 200 list rather than a
canonical 404); controls disabled + section ``aria-busy`` during a mutation
with an ``applicationOperationActive`` guard so rapid clicks cannot issue
overlapping mutations; stale-response protection via a per-navigation
``detailRequestSeq`` token plus ``isCurrentDetailJob`` so a response for an old
job can never overwrite the visible one; the exact distinction between the two
404 semantics (the ``GET /applications/{id}`` 404 remains the canonical
untracked absent state for singular-API clients, while the view reads the
list endpoint whose empty-items branch is the "Not tracked." state, and a
``PUT /applications/{id}`` 404 is a genuine mutation failure rendering the
normal single section-local error with Retry); mutation failures
(404/422/500/503) rendering a section error box that retries the SAME operation
while preserving the previously rendered application state;
no untrack/clear/reset control (no backend operation exists, so nothing is
invented and no DELETE is issued for applications); PUT being the only newly
permitted application mutation with every forbidden endpoint absent and no
review/reminder mutation touched; no storage APIs, unsafe HTML sinks, or
private field/path access; Home/Jobs/Review and the existing read-only detail
sections unchanged; the shared method set invariant ``["POST", "POST",
"PUT", "DELETE"]``; static serving, ``SCHEMA_VERSION``, the no-``careeros.cli`` import
guarantee, the untouched real default database, and wheel packaging of the
static assets.
"""

from __future__ import annotations

import ast
import importlib.resources
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import lock_path_for
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_STATIC_DIR = importlib.resources.files("careeros.api").joinpath("static")

_JS_NAME = "app.js"
_CSS_NAME = "style.css"

_ITEM_FIELDS = [
    "application_id",
    "job_id",
    "current_status",
    "created_at",
    "updated_at",
    "job",
]

_JOB_FIELDS = [
    "id",
    "source",
    "source_job_id",
    "title",
    "company",
    "location",
    "remote_status",
    "employment_type",
    "published_at",
    "url",
]


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


def _application_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --------------------------------------------------------------------------- #
# Source-slice helpers (bounded so a slice never bleeds into the next
# function's doc comment, which would corrupt substring assertions).
# --------------------------------------------------------------------------- #


def _application_render_slice(js: str) -> str:
    start = js.index("function renderApplication(body, job, settled, retry)")
    end = js.index("function buildApplicationStatusActions", start)
    return js[start:end]


def _application_apply_slice(js: str) -> str:
    start = js.index("async function applyApplicationStatus")
    end = js.index("// After a successful mutation, refetch ONLY", start)
    return js[start:end]


def _application_refresh_slice(js: str) -> str:
    start = js.index("async function refreshApplication")
    end = js.index("// Canonical review decisions", start)
    return js[start:end]


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
# A. Six canonical statuses, friendly labels, exact order
# --------------------------------------------------------------------------- #


def test_six_canonical_statuses_in_exact_order() -> None:
    js = _js()
    assert "var APPLICATION_STATUSES = [" in js
    start = js.index("var APPLICATION_STATUSES = [")
    end = js.index("function applicationStatusLabel", start)
    table = js[start:end]
    lines = [
        '{ value: "interested", label: "Interested" },',
        '{ value: "applied", label: "Applied" },',
        '{ value: "interview", label: "Interview" },',
        '{ value: "offer", label: "Offer" },',
        '{ value: "rejected", label: "Rejected" },',
        '{ value: "withdrawn", label: "Withdrawn" },',
    ]
    # Every canonical status with its friendly label is present.
    for line in lines:
        assert line in table, line
    # And they appear in exactly the canonical order.
    positions = [table.index(line) for line in lines]
    assert positions == sorted(positions)
    # The value sent over the wire must be the exact backend vocabulary, never
    # a client-side translation.
    assert 'await putJson("/applications/" + jobId, { status: status });' in js


def test_no_linear_state_machine_and_no_disabled_statuses() -> None:
    js = _js()
    # Every status is always rendered as a clickable button; the current-status
    # check only adds visual state (class/aria), never a disabled attribute.
    start = js.index("function buildApplicationStatusActions")
    end = js.index("function setApplicationActionsDisabled", start)
    builder = js[start:end]
    assert "APPLICATION_STATUSES.forEach(function (entry)" in builder
    assert "button.disabled" not in builder
    assert "current_status === entry.value" in builder
    # Repeating the current status is explicitly allowed.
    assert "Repeating the current status is allowed" in js


# --------------------------------------------------------------------------- #
# B. Server-authoritative flow: no optimistic updates, refresh only Application
# --------------------------------------------------------------------------- #


def test_apply_is_server_authoritative_with_no_optimistic_update() -> None:
    js = _js()
    apply = _application_apply_slice(js)
    assert 'await putJson("/applications/" + jobId, { status: status });' in apply
    assert "await refreshApplication(body, job, seq);" in apply
    # The re-fetch response drives the re-render; nothing is cleared optimistically.
    assert "clearText(body)" not in apply
    assert "fetchDetailSection" not in apply


def test_successful_mutation_refetches_only_application_section() -> None:
    js = _js()
    refresh = _application_refresh_slice(js)
    # Only the job-scoped list GET /applications?job_id={id} is re-fetched and
    # only the Application section re-renders; the whole Job Detail view is
    # never reloaded.
    assert 'var settled = await fetchDetailSection("application", jobId);' in refresh
    assert 'renderSectionBody(section, body, job, "application", settled);' in refresh
    assert "loadJobDetailData" not in refresh
    assert "renderJobDetail" not in refresh
    assert 'fetchDetailSection("group"' not in refresh
    assert 'fetchDetailSection("review"' not in refresh
    assert 'fetchDetailSection("reminders"' not in refresh


def test_empty_list_refetch_renders_not_tracked() -> None:
    js = _js()
    render = _application_render_slice(js)
    # The empty 200 list from the job-scoped list endpoint is the "Not
    # tracked." absent state, not an error panel: the empty-items branch renders
    # the empty state, appends the six status actions (so a first assignment is
    # possible), and returns early; only a rejected (network/5xx) response
    # falls through to the error box.
    assert "data.items.length === 0" in render
    assert 'textContent: "Not tracked."' in render
    assert "buildApplicationStatusActions(body, job, null)" in render
    assert "return;" in render
    # The empty-list branch appends the action group and returns early, so the
    # tracked items[0] processing below it can never run for an empty list.
    assert render.index(
        "buildApplicationStatusActions(body, job, null)"
    ) < render.index("var app = data.items[0];")


def test_tracked_job_shows_server_fields_with_friendly_label() -> None:
    js = _js()
    render = _application_render_slice(js)
    assert (
        'addField(fields, "Status", applicationStatusLabel(app.current_status));'
        in render
    )
    assert 'addField(fields, "First tracked", displayDate(app.created_at));' in render
    assert 'addField(fields, "Last updated", displayDate(app.updated_at));' in render
    assert "buildApplicationStatusActions(body, job, app)" in render


def test_current_status_visually_highlighted() -> None:
    js = _js()
    start = js.index("function buildApplicationStatusActions")
    end = js.index("function setApplicationActionsDisabled", start)
    builder = js[start:end]
    assert "app.current_status === entry.value" in builder
    assert 'button.classList.add("current");' in builder
    assert 'button.setAttribute("aria-pressed", "true");' in builder


def test_untracked_renders_all_six_status_actions() -> None:
    js = _js()
    # The empty-list ("Not tracked.") branch appends the action group via the
    # shared builder, so all six buttons are present for a first assignment.
    start = js.index("function buildApplicationStatusActions")
    end = js.index("function setApplicationActionsDisabled", start)
    builder = js[start:end]
    assert 'className: "status-button",' in builder
    assert 'type: "button",' in builder
    assert "textContent: entry.label," in builder
    assert "applyApplicationStatus(group, body, job, entry.value);" in builder


def test_no_untrack_or_reset_control() -> None:
    js = _js()
    # No backend operation deletes/clears an application record, so the view
    # must never invent an Untrack button, map a status to "untracked", or
    # issue a DELETE for applications. The read path uses the job-scoped list
    # endpoint; the PUT mutation still targets the singular route.
    application_actions = js[
        js.index("function buildApplicationStatusActions") : js.index(
            "// Canonical review decisions"
        )
    ]
    assert "Untrack" not in application_actions
    assert "untracked" not in application_actions
    assert 'deleteJson("/applications/' not in js
    assert '"/applications?job_id=" + jobId' in js
    assert 'putJson("/applications/" + jobId, { status: status });' in js


# --------------------------------------------------------------------------- #
# C. In-flight disable, aria-busy, and no overlapping mutations
# --------------------------------------------------------------------------- #


def test_in_flight_state_prevents_overlapping_mutations() -> None:
    js = _js()
    assert "var applicationOperationActive = false;" in js
    assert "applicationOperationActive = true;" in js
    # The single mutation entry point guards on the shared flag.
    assert js.count("if (applicationOperationActive) return;") == 1
    apply = _application_apply_slice(js)
    assert "applicationOperationActive = true;" in apply
    assert "setApplicationActionsDisabled(group, true);" in apply
    assert 'section.setAttribute("aria-busy", "true");' in apply
    assert "applicationOperationActive = false;" in apply


def test_render_job_detail_resets_application_guard() -> None:
    js = _js()
    start = js.index("async function renderJobDetail")
    end = js.index("function renderJobDetailContent", start)
    detail = js[start:end]
    assert "applicationOperationActive = false;" in detail


def test_application_section_busy_state_cleared_after_refresh() -> None:
    js = _js()
    refresh = _application_refresh_slice(js)
    assert 'section.removeAttribute("aria-busy");' in refresh


# --------------------------------------------------------------------------- #
# D. Stale-response protection
# --------------------------------------------------------------------------- #


def test_apply_and_refresh_guard_against_stale_responses() -> None:
    js = _js()
    apply = _application_apply_slice(js)
    assert "var seq = detailRequestSeq;" in apply
    assert "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;" in (
        apply
    )
    refresh = _application_refresh_slice(js)
    assert "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;" in (
        refresh
    )


# --------------------------------------------------------------------------- #
# E. Mutation-failure error handling (PUT 404 is an error, GET 404 is absent)
# --------------------------------------------------------------------------- #


def test_apply_failure_preserves_state_and_retries_put() -> None:
    js = _js()
    apply = _application_apply_slice(js)
    assert "replaceSectionError(" in apply
    assert '"The application status could not be saved."' in apply
    # Retry re-issues the failed PUT (never a full reload) and re-enables the
    # controls; the previously rendered application state is left in place.
    assert "applyApplicationStatus(group, body, job, status);" in apply
    assert "setApplicationActionsDisabled(group, false);" in apply
    assert 'section.removeAttribute("aria-busy");' in apply
    assert "clearText(body)" not in apply
    # The mutation catch path must never append directly to the section body:
    # that is exactly what accumulated duplicate error panels.
    assert "body.appendChild(" not in apply


def test_put_404_is_mutation_failure_not_absent_state() -> None:
    js = _js()
    # The PUT response is never treated as an absent state: applyApplicationStatus
    # only routes through the section-error path (replaceSectionError), and the
    # "Not tracked." absent state is keyed exclusively to the empty-items branch
    # inside renderApplication.
    apply = _application_apply_slice(js)
    assert "Not tracked." not in apply
    assert "replaceSectionError(" in apply
    # renderApplication is the only place that renders the absent state.
    render = _application_render_slice(js)
    assert 'textContent: "Not tracked."' in render


def test_repeated_mutation_failures_replace_not_append() -> None:
    """Regression: repeated failed mutations must replace the Application-section
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
    # Exactly one definition, and the application mutation catch path calls it
    # exactly once (the review mutations each call it once too).
    assert js.count("function replaceSectionError(") == 1
    apply = _application_apply_slice(js)
    assert apply.count("replaceSectionError(") == 1
    # The old append-only pattern is what accumulated duplicate panels.
    assert "body.appendChild(" not in apply
    # Successful recovery clears the error: the success path clears the section
    # body (removing any error box) before re-rendering canonical server state.
    refresh = _application_refresh_slice(js)
    assert "clearText(body);" in refresh


def test_write_json_helpers_reused_and_put_is_exact() -> None:
    js = _js()
    assert "async function writeJson(url, options)" in js
    assert "function putJson(url, body)" in js
    assert "error.status = response.status;" in js
    # The application PUT body carries exactly the canonical status field.
    assert "body: JSON.stringify(body)," in js
    assert 'await putJson("/applications/" + jobId, { status: status });' in js


# --------------------------------------------------------------------------- #
# F. Method set, forbidden endpoints, and hygiene
# --------------------------------------------------------------------------- #


def test_method_set_is_exactly_post_put_patch_delete_in_order() -> None:
    js = _js()
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    # POST /report (Review view), POST /reminders (create), PUT (review/
    # application/reminder lifecycle), PATCH (reminder edit), DELETE (review
    # clear + reminder delete) -- in source order.
    assert methods == ["POST", "POST", "PUT", "PATCH", "DELETE"]


def test_forbidden_endpoints_remain_absent() -> None:
    js = _js()
    for forbidden in (
        "/ranked-jobs",
        "/review-queue",
        "/workflow",
        "/report/",
    ):
        assert f'"/{forbidden}' not in js, forbidden


def test_reminder_mutations_are_lifecycle_only() -> None:
    js = _js()
    # The review-decision PUT/DELETE and application PUT mutation lines are
    # unchanged. The reminder mutations are exactly the create POST, the
    # idempotent lifecycle PUTs (complete/reopen), the inline edit PATCH
    # /reminders/{id}, and the confirmed DELETE (remove).
    assert 'putJson("/review-decisions/" + jobId, { decision: decision });' in js
    assert 'deleteJson("/review-decisions/" + jobId);' in js
    assert 'putJson("/applications/" + jobId, { status: status });' in js
    assert '"/reminders?job_id=" + jobId' in js
    assert 'await postJson("/reminders", {' in js
    assert 'putJson("/reminders/" + reminderId + "/complete");' in js
    assert 'putJson("/reminders/" + reminderId + "/reopen");' in js
    assert 'await deleteJson("/reminders/" + reminderId);' in js
    assert 'await patchJson("/reminders/" + reminderId, payload);' in js


def test_home_jobs_review_regressions_intact() -> None:
    js = _js()
    assert 'var data = await fetchJson("/report");' in js
    assert 'await loadJobs(qs ? "/jobs?" + qs : "/jobs");' in js
    # Matching Preview adds one explicit call to the three existing ranking
    # call sites; application mutations remain independent of all four.
    assert js.count("await postReportJson(") == 4
    assert 'addEventListener("submit", reviewFormSubmitted);' in js


def test_read_only_detail_sections_unchanged() -> None:
    js = _js()
    for signature in (
        "function renderOpportunity(body, job, settled, retry)",
        "function buildMemberCard(member, currentJob)",
        "function renderReminders(body, job, settled, retry)",
        "function buildCoreJobSection(job)",
        "function renderReview(body, job, settled, retry)",
        "function renderReviewDecisionSection(body, job, review)",
        'base + "/group"',
        '"/applications?job_id=" + jobId',
        '"/review-decisions?job_id=" + jobId',
        '"/reminders?job_id=" + jobId',
    ):
        assert signature in js, signature


def test_no_per_member_application_fetches() -> None:
    js = _js()
    # Application reads/mutations stay anchored to the current jobId, never a
    # grouped member.
    assert '"/applications/" + member' not in js


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
        "document.cookie",
        "profile_path",
        "candidate_profile",
        ".sqlite",
        "/data/",
        ".source_job_id",
        "score_components",
        ".reasons",
        "reminder.message",
        "notification_summary",
        "last_error",
        "notes",
    ):
        assert forbidden not in js, forbidden


def test_application_actions_css_present() -> None:
    css = _css()
    assert ".status-actions {" in css
    assert ".status-button {" in css
    assert ".status-button.current {" in css
    assert ".status-button:disabled {" in css
    # No destructive status control is styled (there is no untrack/reset).
    assert ".status-button.clear" not in css


# --------------------------------------------------------------------------- #
# G. Behavioral mutation contract through the TestClient
# --------------------------------------------------------------------------- #


def test_put_first_assignment_reflects_in_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            f"/applications/{job_ids['a-1']}", json={"status": "interested"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert list(payload) == _ITEM_FIELDS
        assert payload["current_status"] == "interested"
        assert payload["job_id"] == job_ids["a-1"]
        assert payload["application_id"] > 0
        assert list(payload["job"]) == _JOB_FIELDS

        fetched = client.get(f"/applications/{job_ids['a-1']}")
        assert fetched.status_code == 200
        assert fetched.json() == payload

    assert _application_count(path, "applications") == 1
    assert _application_count(path, "application_events") == 1


def test_every_valid_status_is_assignable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for status in (
            "interested",
            "applied",
            "interview",
            "offer",
            "rejected",
            "withdrawn",
        ):
            response = client.put(
                f"/applications/{job_ids['a-1']}", json={"status": status}
            )
            assert response.status_code == 200, status
            assert response.json()["current_status"] == status, status

        detail = client.get(f"/applications/{job_ids['a-1']}")
        assert detail.json()["current_status"] == "withdrawn"

    assert _application_count(path, "applications") == 1
    assert _application_count(path, "application_events") == 6


def test_any_to_any_backwards_transition_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        client.put(f"/applications/{job_ids['a-1']}", json={"status": "applied"})
        backwards = client.put(
            f"/applications/{job_ids['a-1']}", json={"status": "interested"}
        )
        assert backwards.status_code == 200
        assert backwards.json()["current_status"] == "interested"
        assert backwards.json()["application_id"] > 0

    assert _application_count(path, "application_events") == 2


def test_put_same_status_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.put(
            f"/applications/{job_ids['a-1']}", json={"status": "applied"}
        )
        second = client.put(
            f"/applications/{job_ids['a-1']}", json={"status": "applied"}
        )
        assert second.status_code == 200
        assert second.json() == first.json()

    assert _application_count(path, "application_events") == 1


def test_put_invalid_status_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put(
            f"/applications/{job_ids['a-1']}", json={"status": "bogus"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Invalid application status 'bogus'; expected one of: "
        "interested, applied, interview, offer, rejected, withdrawn."
    )
    assert _application_count(path, "applications") == 0
    assert _application_count(path, "application_events") == 0


def test_put_unknown_job_is_404_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put("/applications/9999", json={"status": "applied"})

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_get_untracked_is_canonical_absent_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/applications/{job_ids['b-1']}")

    assert response.status_code == 404
    assert response.json() == {
        "detail": f"No application found for job {job_ids['b-1']}."
    }


# --------------------------------------------------------------------------- #
# H. Database-availability and lock-contention behaviour
# --------------------------------------------------------------------------- #


def test_missing_database_is_500_and_nothing_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "careeros.sqlite3"
    _set_database(monkeypatch, path)

    with pytest.raises(ValueError, match="current schema v10"):
        with TestClient(app):
            pass
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
                f"/applications/{job_ids['a-1']}", json={"status": "applied"}
            )
        assert response.status_code == 503
        assert (
            "The database is temporarily busy; retry the request later."
            in response.text
        )
        assert _application_count(path, "applications") == 0
    finally:
        _release_child(child)

    # The lock is released: the same mutation now succeeds.
    with TestClient(app) as client:
        retry = client.put(
            f"/applications/{job_ids['a-1']}", json={"status": "applied"}
        )
    assert retry.status_code == 200
    assert retry.json()["current_status"] == "applied"


# --------------------------------------------------------------------------- #
# I. Shared invariants: schema version, no cli import, real DB, static assets
# --------------------------------------------------------------------------- #


def test_schema_version_stays_current() -> None:
    assert SCHEMA_VERSION == 10


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
    assert path.exists()
