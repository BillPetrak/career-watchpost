"""Focused tests for the Job Detail view (``#job/<id>``) of the read-only
``/ui`` frontend.

The Job Detail view is the third frontend milestone: a same-origin detail page
opened from Jobs/Review/Recommendation cards that shows one stored job's public
fields plus its canonical group context, application status, review decision,
and reminders. Like the earlier slices this suite is deliberately
stdlib/Python-test-only (there is no Node toolchain), so most checks are
source-level invariants on ``index.html`` / ``app.js`` / ``style.css`` plus
behavioral checks through the FastAPI TestClient for the exact five read GET
route families the view depends on.

Covered behaviour: the ``#job/<positive-int>`` route parsing and its
malformed-input contract; in-memory origin/back behavior (Jobs vs Review) with
no storage APIs; the two card actions (View details / Open original) present in
Jobs, Review, Recommendations, and group members; exactly five read GET route
families issued concurrently via ``Promise.allSettled`` with no
per-member N+1 fetches and only the approved review-decision mutations
(PUT/DELETE ``/review-decisions/{id}``) additionally permitted; the core job
response being authoritative (404 -> whole-page not found, network/500 ->
whole-page error + full retry); group failures being section errors with
section-only retry (never a 404-tolerated absence); singleton groups omitting
the Opportunity section; application/review reads using their job-scoped list
endpoints so the empty-items state renders "Not tracked." / "Not reviewed."
without any expected 404; reminders rendering in server order with Open/Completed
states; private fields never rendered; safe http(s)-only external links; no
unsafe HTML sinks; static serving, SCHEMA_VERSION, the no-``careeros.cli``
import guarantee, the untouched real default database, missing/uninitialized
database behaviour across all five endpoints, narrow responsive CSS, and wheel
packaging of the static assets.
"""

from __future__ import annotations

import ast
import importlib.resources
import re
from datetime import datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.reminders.service import add_reminder, complete_reminder
from careeros.review.service import set_decision
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_STATIC_DIR = importlib.resources.files("careeros.api").joinpath("static")

_HTML_NAME = "index.html"
_JS_NAME = "app.js"
_CSS_NAME = "style.css"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

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

_GROUP_FIELDS = ["group_id", "representative", "members", "sources", "urls"]

_APPLICATION_FIELDS = [
    "application_id",
    "job_id",
    "current_status",
    "created_at",
    "updated_at",
    "job",
]

_REVIEW_FIELDS = [
    "decision_id",
    "job_id",
    "decision",
    "created_at",
    "updated_at",
    "job",
]

_REMINDER_FIELDS = [
    "reminder_id",
    "job_id",
    "kind",
    "due_at",
    "completed_at",
    "created_at",
    "updated_at",
    "job",
]


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


def _render_application_slice(js: str) -> str:
    start = js.index("function renderApplication(body, job, settled, retry)")
    end = js.index("function buildApplicationStatusActions", start)
    return js[start:end]


def _render_review_slice(js: str) -> str:
    start = js.index("function renderReview(body, job, settled, retry)")
    end = js.index("function renderReviewDecisionSection", start)
    return js[start:end]


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str,
    *,
    remote_status: str | None = "remote",
    employment_type: str | None = "Full-time",
    published_at: str = "2026-08-01T10:00:00Z",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed(path: Path) -> tuple[dict[str, int], str, dict[str, int]]:
    """Seed a duplicate pair plus singletons with local workflow state.

    The arbeitnow "Senior Engineer" listing is the duplicate pair's
    representative (lowest job id); the remotive listing is the
    non-representative member. The representative carries an application, a
    review decision, one open reminder and one completed reminder. Returns
    source-job-id -> jobs.id, the canonical group ID, and kind -> reminder id.
    """
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("remotive", "r-1", "Senior Engineer", "Acme", "Berlin, Germany"),
            _make_job(
                "remoteok",
                "k-1",
                "Data Scientist",
                "Other Co",
                "Munich",
                remote_status=None,
                employment_type="Contract",
            ),
            _make_job("jobicy", "j-1", "Frontend Developer", "Frontend Labs", "Berlin"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    ensure_grouped(connection)
    group_id = connection.execute(
        "SELECT group_id FROM job_group_members WHERE job_id = ?",
        (job_ids["a-1"],),
    ).fetchone()[0]

    set_application_status(
        connection,
        job_id=job_ids["a-1"],
        status="interview",
        note="secret-application-note",
        now=_EPOCH,
    )
    set_decision(
        connection,
        job_id=job_ids["a-1"],
        decision="shortlist",
        note="secret-review-note",
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["a-1"],
        kind="follow_up",
        due_at="2026-08-05T09:00:00+00:00",
        message="secret-reminder-message",
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["a-1"],
        kind="interview",
        due_at="2026-08-10T14:00:00+00:00",
        now=_EPOCH,
    )
    reminder_ids = {
        row["kind"]: row["id"]
        for row in connection.execute("SELECT id, kind FROM reminders ORDER BY id")
    }
    complete_reminder(connection, reminder_id=reminder_ids["interview"], now=_EPOCH)
    connection.close()
    return job_ids, group_id, reminder_ids


def _clean_seed(path: Path) -> dict[str, int]:
    """Seed only a duplicate pair, no workflow state."""
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("remotive", "r-1", "Senior Engineer", "Acme", "Berlin, Germany"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    ensure_grouped(connection)
    connection.close()
    return job_ids


# --------------------------------------------------------------------------- #
# A. Routing: #job/<id> parsing and origin/back behaviour
# --------------------------------------------------------------------------- #


def test_job_route_is_recognized_by_navigation() -> None:
    js = _js()
    assert 'var JOB = "job";' in js
    assert "function parsedJobId()" in js
    assert "var jobId = parsedJobId();" in js
    assert "if (jobId !== null)" in js
    assert re.search(r"view\s*=\s*JOB\s*;", js)
    assert re.search(r"else\s+if\s*\(\s*view\s*===\s*JOB\s*\)", js)
    assert "await renderJobDetail(jobId);" in js


def test_job_route_regex_contract() -> None:
    """#job/<id> must accept positive integers only; malformed ids are treated
    exactly like unknown hashes (null)."""
    js = _js()
    assert r"/^job\/(\d+)$/" in js
    assert "var id = parseInt(match[1], 10);" in js
    assert "return id >= 1 ? id : null;" in js

    pattern = re.compile(r"^job/(\d+)$")

    def parse(raw: str) -> int | None:
        match = pattern.match(raw)
        if not match:
            return None
        value = int(match.group(1))
        return value if value >= 1 else None

    assert parse("job/123") == 123
    assert parse("job/1") == 1
    assert parse("job/0") is None
    assert parse("job/-1") is None
    assert parse("job/abc") is None
    assert parse("job/12/foo") is None
    assert parse("job/") is None
    assert parse("job") is None
    assert parse("jobs") is None
    assert parse("job/99999999999999999999") == 99999999999999999999


def test_main_skip_link_anchor_keeps_job_view() -> None:
    """#main (and every non-job non-view hash) must parse to null in both
    parsers, so activating the skip link while on a job keeps the job."""
    js = _js()
    assert "return null; // unknown / absent / non-view (e.g. #main)" in js
    assert "var name = view || visibleView() || HOME;" in js
    # Parsing only recognizes the exact route literals.
    assert "raw === JOBS" in js
    assert "raw === REVIEW" in js
    assert "raw === HOME" in js
    # The job parser returns null for non-job hashes (e.g. #main).
    assert "if (!match) return null;" in js


def test_job_detail_origin_is_in_memory_only() -> None:
    js = _js()
    assert "var jobDetailOrigin = JOBS;" in js
    assert "if (leaving === JOBS || leaving === REVIEW || leaving === REVIEWED ||" in js
    assert "leaving === MATCHING" in js
    assert "jobDetailOrigin = leaving;" in js
    assert 'href: "#" + jobDetailOrigin' in js
    assert 'jobDetailOrigin === REVIEWED ? "Back to reviewed"' in js
    assert 'jobDetailOrigin === APPLICATIONS ? "Back to applications"' in js
    combined = "\n".join((_html(), js, _css())).lower()
    for forbidden in ("localstorage", "sessionstorage", "document.cookie"):
        assert forbidden not in combined, forbidden


def test_deep_link_refresh_of_job_route_supported() -> None:
    """A direct load/refresh with #job/<id> in the hash renders the detail
    view: parsedJobId reads window.location.hash and navigate() renders on the
    initial DOMContentLoaded pass."""
    js = _js()
    assert "window.location.hash" in js
    assert re.search(r"view\s*=\s*HOME;", js)  # initial-load default still present
    assert 'document.addEventListener("DOMContentLoaded"' in js
    assert "navigate();" in js


def test_back_link_uses_in_memory_origin_not_browser_history_api() -> None:
    js = _js()
    # The Back link is a plain hash change so browser Back/Forward stays
    # natural; no history.pushState/replaceState is used anywhere.
    assert 'href: "#" + jobDetailOrigin' in js
    assert "history.pushState" not in js
    assert "history.replaceState" not in js


# --------------------------------------------------------------------------- #
# B. Card actions: View details + Open original
# --------------------------------------------------------------------------- #


def test_view_details_link_added_to_every_card_builder() -> None:
    js = _js()
    # Jobs list, Review/Recommendation cards, and group member cards all build
    # through appendJobCardActions, so the pair of actions is reused.
    assert js.count("appendJobCardActions(card, job);") >= 2
    assert js.count("appendJobCardActions(card, member);") == 1
    assert 'href: "#job/" + job.id' in js
    assert 'textContent: "View details"' in js


def test_job_details_link_suppresses_unusable_ids() -> None:
    js = _js()
    assert "!isObject(job) || !isFiniteNumber(job.id) || job.id < 1" in js
    assert "return null;" in js


def test_open_original_action_retained_and_safe() -> None:
    js = _js()
    # Card actions and the core detail section both open the original listing.
    assert js.count('safeExternalLink(job.url, "Open original")') == 3
    assert 'target: "_blank"' in js
    assert 'rel: "noopener noreferrer"' in js
    assert 'parsed.protocol !== "http:" && parsed.protocol !== "https:"' in js


# --------------------------------------------------------------------------- #
# C. Request model: seven canonical read GETs, concurrent, no N+1
# --------------------------------------------------------------------------- #


def test_seven_canonical_route_families_built_once() -> None:
    js = _js()
    assert "function jobDetailUrls(jobId)" in js
    assert 'var base = "/jobs/" + jobId;' in js
    for route in (
        "job: base",
        'group: base + "/group"',
        'application: "/applications?job_id=" + jobId',
        'preparation: "/applications/" + jobId + "/preparation"',
        'package: "/applications/" + jobId + "/package?offset="',
        'review: "/review-decisions?job_id=" + jobId',
        'reminders: "/reminders?job_id=" + jobId',
    ):
        assert route in js, route
    assert (
        'var keys = ["job", "group", "application", "preparation", '
        '"package", "review", "reminders"];' in js
    )


def test_load_issues_all_requests_concurrently_via_all_settled() -> None:
    js = _js()
    assert "Promise.allSettled(" in js
    assert "keys.map(function (key)" in js
    assert "return fetchJson(urls[key]);" in js


def test_no_per_member_n_plus_one_fetches() -> None:
    js = _js()
    # Members come from the embedded group response only.
    assert "group.members.forEach(function (member)" in js
    for forbidden in (
        '"/jobs/" + member',
        '"/applications/" + member',
        '"/review-decisions/" + member',
        '"/reminders?job_id=" + member',
    ):
        assert forbidden not in js, forbidden


def test_only_workflow_mutations_permitted() -> None:
    js = _js()
    # Frontend mutations are the Job Detail workflow actions: PUT and
    # DELETE /review-decisions/{id}, PUT /applications/{id}, and the reminder
    # POST /reminders plus PATCH /reminders/{id} (edit),
    # PUT /reminders/{id}/complete, PUT /reminders/{id}/reopen, and
    # DELETE /reminders/{id}. The computation-only POST /report remains the
    # Review view's sole POST, and every forbidden mutation/computation route
    # stays absent. The additional literal request descriptors belong to the
    # package answer/material mutation definitions; packageWrite applies the
    # authoritative method and both independent CAS headers.
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    assert methods == [
        "POST",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "DELETE",
        "PUT",
        "DELETE",
        "PUT",
    ]
    for forbidden in (
        "/saved-searches",
        "/ranked-jobs",
        "/review-queue",
        "/workflow",
        "/report/",
    ):
        assert f'"/{forbidden}' not in js, forbidden


def test_core_job_response_is_authoritative() -> None:
    js = _js()
    # A canonical 404 on the core job -> whole-page not found.
    assert "reason.status === 404" in js
    assert "renderJobNotFound(content, jobId);" in js
    assert 'textContent: "Job not found"' in js
    assert 'textContent: "No job found with ID " + jobId + "."' in js
    # A network/500/malformed core failure -> whole-page error whose Retry
    # reruns the entire detail load.
    assert "renderWholePageError(content, reason, function () {" in js
    assert 'textContent: "Could not load this job."' in js
    assert js.count("renderJobDetail(jobId);") >= 3  # nav + whole-page retries


def test_ancillary_section_retry_refetches_only_that_section() -> None:
    js = _js()
    assert "async function retrySection(container, body, job, sectionKey)" in js
    assert "var settled = await fetchDetailSection(sectionKey, job.id);" in js
    assert "function fetchDetailSection(sectionKey, jobId)" in js
    assert 'return { status: "fulfilled", value: value };' in js


# --------------------------------------------------------------------------- #
# D. Section rendering
# --------------------------------------------------------------------------- #


def test_section_headings_and_creation_order() -> None:
    js = _js()
    assert 'if (sectionKey === "group") return "Opportunity";' in js
    assert 'if (sectionKey === "application") return "Application";' in js
    assert 'if (sectionKey === "review") return "Review";' in js
    assert 'return "Reminders";' in js
    # The detail content renders core, then group, application, review,
    # reminders in that order.
    assert "content.appendChild(buildCoreJobSection(job));" in js
    assert 'fillDetailSection(content, job, "group", results.group);' in js
    assert 'fillDetailSection(content, job, "application", results.application);' in js
    assert 'fillDetailSection(content, job, "review", results.review);' in js
    assert 'fillDetailSection(content, job, "reminders", results.reminders);' in js


def test_core_job_section_renders_public_fields_only() -> None:
    js = _js()
    assert "function buildCoreJobSection(job)" in js
    for label in (
        'addField(fields, "Title", job.title);',
        'addField(fields, "Company", job.company);',
        'addField(fields, "Location", job.location);',
        'addField(fields, "Remote", job.remote_status);',
        'addField(fields, "Employment type", job.employment_type);',
        'addField(fields, "Source", job.source);',
        'addField(fields, "Published", displayDate(job.published_at));',
    ):
        assert label in js, label


def test_singleton_group_omits_opportunity_section() -> None:
    js = _js()
    assert "settled.value.members.length <= 1" in js
    assert "container.hidden = true;" in js
    assert (
        '// A one-member (singleton) group is the normal "no duplicates" state:' in js
    )


def test_multi_member_group_renders_count_and_members() -> None:
    js = _js()
    assert 'group.members.length + " listings in this opportunity."' in js
    assert "group.members.forEach(function (member)" in js
    assert "var card = buildMemberCard(member, job);" in js


def test_group_failure_is_section_error_not_absence() -> None:
    js = _js()
    start = js.index("function renderOpportunity")
    end = js.index("function buildMemberCard")
    opportunity = js[start:end]
    assert (
        'sectionErrorBox("The opportunity could not be loaded.", settled.reason, retry)'
        in opportunity
    )
    # The Opportunity section must never special-case a 404 as "no group".
    assert "status === 404" not in opportunity
    # The core job response remains the only 404-tolerated read: a canonical
    # 404 there is the whole-page "Job not found" state. Application/review
    # absent state is now an empty 200 list, so the two sections never
    # special-case a 404. The only other 404 special-cases in the file are the
    # reminder-DELETE, reminder-edit-PATCH, Today reminder-completion mutation,
    # and Saved Search execution reconciles (a Saved Search 404 refreshes its
    # metadata); every other rejection is a plain section error.
    # Package adds one normal untracked-application absence and one
    # post-deletion authoritative reconciliation branch.
    assert js.count("status === 404") == 8
    assert "status === 404" not in _render_application_slice(js)
    assert "status === 404" not in _render_review_slice(js)


def test_application_not_tracked_and_public_shape() -> None:
    js = _js()
    assert 'textContent: "Not tracked."' in js
    # The absent state is an empty 200 list from the job-scoped list endpoint,
    # never a canonical 404: the empty-items branch renders the note and the
    # action controls, and a >1 list is an explicit invariant error, never an
    # arbitrary items[0] selection.
    render = _render_application_slice(js)
    assert "data.items.length === 0" in render
    assert "buildApplicationStatusActions(body, job, null));" in render
    assert "data.items.length > 1" in render
    # The Status field renders the friendly label; the transmitted value stays
    # the canonical backend status.
    assert (
        'addField(fields, "Status", applicationStatusLabel(app.current_status));' in js
    )
    assert 'addField(fields, "First tracked", displayDate(app.created_at));' in js
    assert 'addField(fields, "Last updated", displayDate(app.updated_at));' in js
    assert 'typeof app.current_status !== "string"' in js


def test_review_not_reviewed_and_public_shape() -> None:
    js = _js()
    assert 'textContent: "Not reviewed."' in js
    # The absent state is an empty 200 list from the job-scoped list endpoint,
    # never a canonical 404; a >1 list is an explicit invariant error, never an
    # arbitrary items[0] selection.
    render = _render_review_slice(js)
    assert "data.items.length === 0" in render
    assert "renderReviewDecisionSection(body, job, null);" in render
    assert "data.items.length > 1" in render
    assert 'addField(fields, "Decision", review.decision);' in js
    assert 'addField(fields, "Decided", displayDate(review.created_at));' in js
    assert 'addField(fields, "Last updated", displayDate(review.updated_at));' in js
    assert 'typeof review.decision !== "string"' in js


def test_reminders_empty_open_and_completed_states() -> None:
    js = _js()
    assert 'textContent: "No reminders."' in js
    assert "data.items.forEach(function (reminder)" in js
    assert (
        'typeof reminder.completed_at === "string" && '
        "reminder.completed_at.length > 0" in js
    )
    assert '? "Completed"' in js
    assert ': "Open";' in js
    assert "Array.isArray(data.items)" in js


def test_no_client_reranking_or_resorting() -> None:
    js = _js()
    assert ".sort(" not in js
    assert ".reverse(" not in js


# --------------------------------------------------------------------------- #
# E. Privacy, security, and hygiene
# --------------------------------------------------------------------------- #


def test_no_private_fields_rendered_or_requested() -> None:
    legacy_js = _js()[: _js().index("// Reviewed opportunities workspace")]
    combined = "\n".join((_html(), legacy_js, _css()))
    for forbidden in (
        ".description",
        ".source_job_id",
        "score_components",
        ".reasons",
        ".review_decision",
        ".application_status",
        "review.message",
        "application.message",
        "reminder.message",
        "reminders.message",
        "notification_summary",
        "last_error",
        "notes",
    ):
        assert forbidden not in combined, forbidden


def test_no_private_filesystem_profile_or_log_access() -> None:
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


def test_no_unsafe_html_sinks_for_dynamic_data() -> None:
    js = _js()
    assert ".innerHTML" not in js
    assert "innerHTML=" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js
    assert ".outerHTML" not in js


def test_http_only_external_links_and_no_hardcoded_absolutes() -> None:
    js = _js()
    assert "http://" not in js and "https://" not in js


def test_job_detail_view_section_markers_in_html() -> None:
    html = _html()
    assert 'id="view-job"' in html
    assert 'data-view="job"' in html
    assert 'id="job-detail-status"' in html
    assert 'id="job-detail-content"' in html
    assert 'class="view" data-view="job" hidden' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html


def test_job_detail_css_present() -> None:
    css = _css()
    assert ".job-back" in css
    assert ".section-body" in css
    assert ".job-actions" in css
    assert "flex-wrap: wrap" in css
    assert ".current-job" in css
    assert ".retry-button" in css
    assert ".error-detail" in css
    assert "@media" in css


# --------------------------------------------------------------------------- #
# F. Behavioral: the five canonical endpoints the view consumes
# --------------------------------------------------------------------------- #


def test_jobs_detail_endpoint_returns_canonical_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_ids['a-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _JOB_FIELDS
    assert payload["id"] == job_ids["a-1"]
    assert "description" not in payload


def test_group_endpoint_is_canonical_for_representative_and_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        rep = client.get(f"/jobs/{job_ids['a-1']}/group")
        non_rep = client.get(f"/jobs/{job_ids['r-1']}/group")

    assert rep.status_code == 200
    assert non_rep.status_code == 200
    assert rep.content == non_rep.content
    payload = rep.json()
    assert list(payload) == _GROUP_FIELDS
    assert payload["group_id"] == group_id
    assert payload["representative"]["id"] == job_ids["a-1"]
    assert [member["id"] for member in payload["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]


def test_application_endpoint_404_when_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/applications/{job_ids['k-1']}")

    assert response.status_code == 404
    assert response.json()["detail"] == (
        f"No application found for job {job_ids['k-1']}."
    )


def test_application_endpoint_shape_and_no_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/applications/{job_ids['a-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _APPLICATION_FIELDS
    assert payload["job_id"] == job_ids["a-1"]
    assert payload["current_status"] == "interview"
    assert list(payload["job"]) == _JOB_FIELDS


def test_review_endpoint_404_when_unreviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/review-decisions/{job_ids['k-1']}")

    assert response.status_code == 404
    assert (
        response.json()["detail"]
        == f"No review decision found for job {job_ids['k-1']}."
    )


def test_review_endpoint_shape_and_no_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/review-decisions/{job_ids['a-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _REVIEW_FIELDS
    assert payload["job_id"] == job_ids["a-1"]
    assert payload["decision"] == "shortlist"
    assert list(payload["job"]) == _JOB_FIELDS


def test_reminders_endpoint_empty_for_no_reminders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/reminders?job_id={job_ids['k-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 0
    assert payload["items"] == []
    assert list(payload) == ["items", "total", "offset", "limit"]


def test_reminders_endpoint_orders_by_due_then_id_with_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/reminders?job_id={job_ids['a-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["kind"] for item in payload["items"]] == ["follow_up", "interview"]
    assert payload["items"][0]["completed_at"] is None  # open
    assert payload["items"][1]["completed_at"] is not None  # completed
    for item in payload["items"]:
        assert list(item) == _REMINDER_FIELDS
        assert list(item["job"]) == _JOB_FIELDS


def test_seeded_full_detail_five_endpoint_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact five requests the frontend issues for one job id, all
    successful against a seeded database."""
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id, _reminder_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        job = client.get(f"/jobs/{job_ids['a-1']}")
        group = client.get(f"/jobs/{job_ids['a-1']}/group")
        application = client.get(f"/applications/{job_ids['a-1']}")
        review = client.get(f"/review-decisions/{job_ids['a-1']}")
        reminders = client.get(f"/reminders?job_id={job_ids['a-1']}")

    assert job.status_code == 200
    assert group.status_code == 200
    assert application.status_code == 200
    assert review.status_code == 200
    assert reminders.status_code == 200
    assert job.json()["title"] == "Senior Engineer"
    assert group.json()["group_id"] == group_id
    assert application.json()["current_status"] == "interview"
    assert review.json()["decision"] == "shortlist"
    assert reminders.json()["total"] == 2


# --------------------------------------------------------------------------- #
# G. Missing / uninitialized databases
# --------------------------------------------------------------------------- #


def test_missing_database_all_five_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist" / "db.sqlite3"
    _set_database(monkeypatch, missing)

    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass
    # Nothing is ever created for a missing database.
    assert not missing.exists()
    assert not missing.parent.exists()


def test_uninitialized_database_all_five_endpoints_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    path.write_bytes(b"")  # present but uninitialized
    _set_database(monkeypatch, path)

    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass


# --------------------------------------------------------------------------- #
# H. Invariants
# --------------------------------------------------------------------------- #


def test_static_serving_requires_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.database.connection import connect
    from careeros.database.schema import initialize_schema

    database = tmp_path / "db.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    with TestClient(app) as client:
        response = client.get("/ui")
    assert response.status_code == 200
    assert database.exists()


def test_schema_version_is_10_for_m1() -> None:
    assert SCHEMA_VERSION == 12


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    database = tmp_path / "careeros.sqlite3"
    from careeros.database.connection import connect
    from careeros.database.schema import initialize_schema

    with connect(database) as connection:
        initialize_schema(connection)
    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    with TestClient(app) as client:
        client.get("/ui")

    assert not real.is_file() or real.stat().st_mtime_ns == before


def test_api_does_not_import_careeros_cli() -> None:
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


def test_wheel_packaging_declares_all_static_assets() -> None:
    pyproject = _SRC.parent.joinpath("pyproject.toml").read_text(encoding="utf-8")
    assert (
        '"careeros.api" = ["static/*.html", "static/*.js", "static/*.css", '
        '"static/*.svg"]' in pyproject
    )


def test_prior_views_still_driven_by_their_single_endpoint() -> None:
    js = _js()
    # Home is still GET /report; Jobs is still GET /jobs; Review uses POST
    # /report initially and POST /review-queue after a successful decision.
    # Saved Searches and Matching Preview each add one explicit POST call site.
    assert 'var data = await fetchJson("/report");' in js
    assert 'await loadJobs(qs ? "/jobs?" + qs : "/jobs");' in js
    assert js.count("await postReportJson(") == 4
    assert 'addEventListener("submit", reviewFormSubmitted)' in js
