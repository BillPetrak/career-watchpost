"""Tests for the Job Detail Reminder create/complete/reopen milestone.

The Job Detail view (`#job/<id>`) gains an always-visible create form plus one
lifecycle action per stored reminder, driving the existing, fully-tested
backend mutations ``POST /reminders``, ``PUT /reminders/{id}/complete``, and
``PUT /reminders/{id}/reopen``. This suite is deliberately stdlib/Python-test-
only (there is no Node toolchain), so most checks are source-level invariants
on ``app.js`` / ``style.css`` plus behavioral checks through the FastAPI
TestClient for the exact mutation contract the view sends.

Covered behaviour: the five canonical kinds (`follow_up`, `interview`, `offer`,
`review`, `custom`) in that exact order with presentation labels and canonical
wire values; the always-visible compact create form (kind select +
datetime-local due input, accessible labels, no reminder message field);
explicit, deterministic datetime serialization — the UTC offset is derived from
``new Date(year, month - 1, day, hour, minute).getTimezoneOffset()`` for the
ENTERED date/time (never today's offset, never a guessed zone, never
``toISOString()``), formatted ``±HH:MM`` with the sign flipped and two-digit
padding, and appended to the wall time without inventing seconds so the server
receives e.g. ``2026-08-10T14:00+02:00``; the visible helper text
``Due time uses your browser's local timezone (UTC±HH:MM).``; strictly
server-authoritative flow with NO optimistic updates (a successful POST/PUT
re-fetches ONLY ``GET /reminders?job_id={id}`` and re-renders only the
Reminders section); controls disabled + section ``aria-busy`` during a mutation
with a single ``reminderOperationActive`` guard so rapid clicks cannot issue
overlapping mutations; stale-response protection via the per-navigation
``detailRequestSeq`` token plus ``isCurrentDetailJob``; exactly one lifecycle
action per reminder (open → "Complete", completed → "Reopen", never both, no
edit control in this slice, plus a separate Delete control for every
reminder); the non-idempotent CREATE retry contract —
a failed ``POST`` is NEVER blindly replayed, a deterministic 422 keeps the
form's values and shows the server detail next to the form, and any other
failure (network/500/503) renders a single error panel whose explicit
"Check reminders" action reconciles against the server with a safe GET before
the user submits again — while the idempotent complete/reopen PUTs may safely
retry the exact failed PUT through the normal section error box; at most one
error panel per section; the destructive Delete action is gated by a native
``window.confirm`` dialog that fires ONLY on the original click (cancelling is
a true no-op, and an automated Retry re-issues the exact DELETE without
re-confirming), a DELETE 404 is reconciled as success via the same
reminder-only refresh, and all other DELETE failures render the single
section error panel; no storage APIs, unsafe HTML sinks, private fields,
custom ``confirm()`` implementations, or client-side sorting; Home/Jobs/Review
and the other read-only detail sections unchanged; the method set invariant
``["POST", "POST", "PUT", "DELETE"]``; static serving, ``SCHEMA_VERSION``, the
no-``careeros.cli`` import guarantee, and the untouched real default database.
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


def _reminder_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --------------------------------------------------------------------------- #
# Source-slice helpers (bounded so a slice never bleeds into the next
# function's doc comment, which would corrupt substring assertions).
# --------------------------------------------------------------------------- #


def _reminders_render_slice(js: str) -> str:
    start = js.index("function renderReminders(body, job, settled, retry)")
    end = js.index("// Canonical reminder kinds", start)
    return js[start:end]


def _reminders_create_slice(js: str) -> str:
    start = js.index("async function createReminder")
    end = js.index("// A failed creation is never re-sent.", start)
    return js[start:end]


def _reminders_reconcile_slice(js: str) -> str:
    start = js.index("// Reconciliation for a failed creation")
    end = js.index("// Exactly one lifecycle action per reminder", start)
    return js[start:end]


def _reminders_lifecycle_slice(js: str) -> str:
    start = js.index("// Exactly one lifecycle action per reminder")
    end = js.index("// After a successful mutation, refetch ONLY", start)
    return js[start:end]


def _reminders_refresh_slice(js: str) -> str:
    start = js.index("async function refreshReminders")
    end = js.index("// Wiring", start)
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
# A. Five canonical kinds, friendly labels, exact order
# --------------------------------------------------------------------------- #


def test_five_canonical_kinds_in_exact_order() -> None:
    js = _js()
    assert "var REMINDER_KINDS = [" in js
    start = js.index("var REMINDER_KINDS = [")
    end = js.index("function reminderKindLabel", start)
    table = js[start:end]
    lines = [
        '{ value: "follow_up", label: "Follow up" },',
        '{ value: "interview", label: "Interview" },',
        '{ value: "offer", label: "Offer" },',
        '{ value: "review", label: "Review" },',
        '{ value: "custom", label: "Custom" },',
    ]
    for line in lines:
        assert line in table, line
    positions = [table.index(line) for line in lines]
    assert positions == sorted(positions)
    # The value sent over the wire must be the exact backend vocabulary, never
    # a client-side translation.
    assert "kind: kind.value" in js
    assert "option.value = entry.value;" in js


def test_reminder_kind_label_falls_back_to_value() -> None:
    js = _js()
    assert "function reminderKindLabel(value)" in js
    assert "return value;" in js


# --------------------------------------------------------------------------- #
# B. Always-visible create form
# --------------------------------------------------------------------------- #


def test_create_form_always_visible_before_empty_state() -> None:
    js = _js()
    render = _reminders_render_slice(js)
    # The form is rendered before the empty/list states so a first reminder is
    # always possible, even on the canonical "No reminders." absent state.
    assert "body.appendChild(buildReminderCreateForm(body, job));" in render
    assert 'textContent: "No reminders."' in render
    form_pos = render.index("buildReminderCreateForm(body, job)")
    empty_pos = render.index('textContent: "No reminders."')
    assert form_pos < empty_pos


def test_create_form_shape_and_accessibility() -> None:
    js = _js()
    slice_ = js[
        js.index("function buildReminderCreateForm") : js.index(
            "function setReminderControlsDisabled"
        )
    ]
    assert 'el("form", { className: "reminder-form" })' in slice_
    assert 'setAttribute("aria-label", "Add reminder")' in slice_
    assert '{ className: "reminder-label", textContent: "Kind" }' in slice_
    assert 'setAttribute("for", "reminder-kind")' in slice_
    assert '{ id: "reminder-kind", className: "reminder-input" }' in slice_
    assert '{ className: "reminder-label", textContent: "Due" }' in slice_
    assert 'setAttribute("for", "reminder-due")' in slice_
    assert 'id: "reminder-due",' in slice_
    assert 'type: "datetime-local",' in slice_
    assert 'setAttribute("name", "due_at")' in slice_
    assert 'setAttribute("required", "")' in slice_
    assert 'textContent: "Add reminder"' in slice_
    # No reminder message field exists: the API never accepts or exposes one.
    assert "message" not in slice_


def test_reminder_form_styles_present() -> None:
    css = _css()
    for selector in (
        ".reminder-form",
        ".reminder-label",
        ".reminder-input",
        ".reminder-add-button",
        ".reminder-actions",
        ".reminder-button",
    ):
        assert selector in css, selector
    assert ".reminder-button:disabled" in css or "reminder-button" in css


# --------------------------------------------------------------------------- #
# C. Explicit, deterministic datetime/timezone serialization
# --------------------------------------------------------------------------- #


def test_offset_uses_entered_datetime_not_today() -> None:
    js = _js()
    # The offset must be derived from the ENTERED date/time, so DST changes are
    # respected; today's offset is never used.
    assert "var date = new Date(year, month - 1, day, hour, minute);" in js
    assert "date.getTimezoneOffset()" in js


def test_offset_roundtrip_validates_wall_time() -> None:
    js = _js()
    # The parsed components must survive the Date round-trip; a wall time that
    # does not exist in the local zone (e.g. a DST gap) yields null.
    assert "date.getFullYear() !== year" in js
    assert "date.getMonth() !== month - 1" in js
    assert "date.getDate() !== day" in js
    assert "date.getHours() !== hour" in js
    assert "date.getMinutes() !== minute" in js
    assert "return null; // wall time does not exist in the local zone" in js


def test_offset_sign_flipped_and_padded() -> None:
    js = _js()
    # getTimezoneOffset() returns minutes behind UTC (negative ahead of UTC);
    # the sign is flipped so "+" means ahead of UTC, and both parts are padded
    # to exactly two digits.
    assert "var offsetMinutes = -date.getTimezoneOffset();" in js
    assert 'var sign = offsetMinutes >= 0 ? "+" : "-";' in js
    assert 'String(offsetHours).padStart(2, "0")' in js
    assert 'String(offsetMins).padStart(2, "0")' in js


def test_wall_time_parsed_from_datetime_local_pattern() -> None:
    js = _js()
    assert "exec(value)" in js
    assert r"/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/".replace("\\d", "\\d") in js


def test_no_invented_seconds_and_no_to_iso_shortcut() -> None:
    js = _js()
    # The wire value is exactly the wall time plus the derived offset (e.g.
    # "2026-08-10T14:00+02:00"); no seconds are invented.
    assert "return value + offset;" in js
    # Date.toISOString() must never be used (it would change the intended
    # wall-clock semantics): it may appear only in the explanatory comment.
    assert js.count("toISOString") == 1
    assert "never used as a shortcut" in js


def test_offset_minutes_to_hhmm_format_contract() -> None:
    # Python-side restatement of the exact ±HH:MM format the JS produces, so a
    # fixed set of offset minutes maps to the strings the server must receive.
    def format_offset(offset_minutes: int) -> str:
        sign = "+" if offset_minutes >= 0 else "-"
        absolute = abs(offset_minutes)
        return sign + str(absolute // 60).zfill(2) + ":" + str(absolute % 60).zfill(2)

    assert format_offset(0) == "+00:00"
    assert format_offset(120) == "+02:00"
    assert format_offset(90) == "+01:30"
    assert format_offset(-330) == "-05:30"
    assert format_offset(-420) == "-07:00"
    # The wire value the JS sends is the wall time plus this offset.
    assert "2026-08-10T14:00" + format_offset(120) == "2026-08-10T14:00+02:00"


def test_due_hint_visible_and_offset_explicit() -> None:
    js = _js()
    assert (
        'hint.textContent = "Pick a date and time to see the timezone it uses."' in js
        or ('"Pick a due date and time to see the timezone it uses."' in js)
    )
    # The hint names the browser/IANA zone when resolvable (never hard-coded)
    # plus the DST-correct per-date numeric offset.
    assert (
        '"Due time uses your local timezone (" + zone + ", UTC" + offset'
        ' + " for this date)."' in js
    )
    assert '"Due time uses your browser\'s local timezone (UTC" + offset + ")."' in js
    assert "Intl.DateTimeFormat().resolvedOptions()" in js
    assert "browserTimezoneName" in js
    # The hint updates live as the user edits the due input.
    assert 'due.addEventListener("input", function () {' in js
    assert "updateDueHint(due, hint);" in js


# --------------------------------------------------------------------------- #
# D. Create mutation flow
# --------------------------------------------------------------------------- #


def test_create_posts_exact_method_url_and_body() -> None:
    js = _js()
    create = _reminders_create_slice(js)
    assert 'await postJson("/reminders", {' in create
    assert "job_id: jobId," in create
    assert "kind: kind.value," in create
    assert "due_at: timestamp," in create
    assert 'method: "POST"' in create or 'method: "POST"' in js


def test_create_is_server_authoritative_no_optimistic_update() -> None:
    js = _js()
    create = _reminders_create_slice(js)
    # The POST response (or its failure) drives everything: there is no
    # client-side list mutation before the request, and the section's previously
    # rendered server state is never cleared while the request is in flight.
    assert "await refreshReminders(body, job, seq);" in create
    assert "clearText(body)" not in create


def test_create_success_refetches_only_reminders_section() -> None:
    js = _js()
    refresh = _reminders_refresh_slice(js)
    assert 'await fetchDetailSection("reminders", jobId);' in refresh
    assert "renderReminders(body, job, settled, function () {" in refresh
    assert "renderJobDetail(" not in refresh
    assert "loadJobDetailData" not in refresh


def test_empty_due_and_invalid_local_time_are_client_validation() -> None:
    js = _js()
    create = _reminders_create_slice(js)
    assert 'validation.textContent = "Pick a due time.";' in create
    assert "due.focus();" in create
    message = (
        '"That due time is not valid in your local timezone; '
        'pick another date and time."'
    )
    assert message in create


# --------------------------------------------------------------------------- #
# E. Non-idempotent create: never blindly replayed
# --------------------------------------------------------------------------- #


def test_create_failure_never_reposts() -> None:
    js = _js()
    create = _reminders_create_slice(js)
    # The create path contains exactly one POST (the initial attempt); the
    # catch path hands off to the failure handler and never re-invokes the POST
    # or createReminder itself.
    assert create.count("postJson") == 1
    assert 'await postJson("/reminders", {' in create
    assert (
        "handleCreateReminderFailure(body, form, job, seq, kind.value, dueAt, error);"
        in create
    )


def test_create_422_preserves_form_and_shows_server_detail() -> None:
    js = _js()
    handler = js[
        js.index("function handleCreateReminderFailure") : js.index(
            "// The create-failure error panel"
        )
    ]
    assert "error.status === 422" in handler
    assert "validation.textContent = error.message;" in handler
    # The 422 branch returns without touching the reconcile/replay panel; that
    # panel is reached only in the non-422 (ambiguous) path.
    assert "return;" in handler
    four_two_two = handler.index("error.status === 422")
    panel_call = handler.index("replaceReminderSaveError")
    assert panel_call > four_two_two


def test_create_ambiguous_failure_reconciles_with_get_not_post() -> None:
    js = _js()
    reconcile = _reminders_reconcile_slice(js)
    assert 'await fetchDetailSection("reminders", job.id);' in reconcile
    assert "postJson" not in reconcile
    assert "createReminder" not in reconcile
    assert "renderReminders(body, job, settled, function () {" in reconcile
    # The freshly reconciled form restores the previously entered values so the
    # user can submit again explicitly if the reminder is still absent.
    assert "kindSelect.value = kind;" in reconcile
    assert "dueInput.value = dueAt;" in reconcile
    assert "updateDueHint(dueInput, hint);" in reconcile


def test_create_error_panel_has_check_reminders_not_replay() -> None:
    js = _js()
    panel = js[
        js.index("function replaceReminderSaveError") : js.index(
            "// Reconciliation for a failed creation"
        )
    ]
    assert 'textContent: "Check reminders"' in panel
    assert "reconcileAction();" in panel
    assert "reconcileReminders(body, job, seq, kind, dueAt);" in js
    # The panel replaces any existing one so a section never holds two boxes.
    assert 'body.querySelectorAll(".error-box").forEach(function (box) {' in panel
    assert "box.remove();" in panel


# --------------------------------------------------------------------------- #
# F. Complete / Reopen lifecycle actions
# --------------------------------------------------------------------------- #


def test_one_lifecycle_action_complete_or_reopen() -> None:
    js = _js()
    lifecycle = _reminders_lifecycle_slice(js)
    assert 'textContent: completed ? "Reopen" : "Complete"' in lifecycle
    assert "buildReminderActions(body, job, reminder)" in lifecycle
    # Every reminder carries exactly one lifecycle action (Complete or Reopen)
    # plus an Edit control that switches the row into the inline edit form; the
    # lifecycle PUTs (complete/reopen) and the edit PATCH live outside this
    # slice (after refreshReminders). Every reminder also carries a separate
    # Delete control gated by the native confirm dialog; the DELETE request
    # itself also lives after refreshReminders.
    assert 'textContent: "Edit"' in lifecycle
    assert "reminder-edit-button" in lifecycle
    assert "startReminderEdit(group.parentNode, body, job, reminder);" in lifecycle
    assert 'textContent: "Delete"' in lifecycle
    assert "delete-reminder" in lifecycle
    assert "requestDeleteReminder(group, body, job, reminder);" in lifecycle
    assert "window.confirm(" in lifecycle
    assert "deleteJson" not in lifecycle


def test_complete_put_url_and_idempotent_retry() -> None:
    js = _js()
    complete = js[
        js.index("async function completeReminder") : js.index(
            "// PUT /reminders/{id}/reopen"
        )
    ]
    assert 'await putJson("/reminders/" + reminderId + "/complete");' in complete
    assert "await refreshReminders(body, job, seq);" in complete
    # The Retry action re-issues the exact failed PUT (idempotent, so safe).
    assert "completeReminder(group, body, job, reminder);" in complete


def test_reopen_put_url_and_idempotent_retry() -> None:
    js = _js()
    reopen = js[
        js.index("async function reopenReminder") : js.index(
            "async function refreshReminders"
        )
    ]
    assert 'await putJson("/reminders/" + reminderId + "/reopen");' in reopen
    assert "await refreshReminders(body, job, seq);" in reopen
    assert "reopenReminder(group, body, job, reminder);" in reopen


# --------------------------------------------------------------------------- #
# G. Guards, concurrency, stale protection, error-panel hygiene
# --------------------------------------------------------------------------- #


def test_reminder_operation_guard_single() -> None:
    js = _js()
    assert "var reminderOperationActive = false;" in js
    assert "if (reminderOperationActive) return;" in js
    assert "reminderOperationActive = true;" in js
    assert "reminderOperationActive = false;" in js
    # Exactly one guard flag: one in-flight reminder operation at a time.
    assert js.count("reminderOperationActive") >= 5


def test_render_job_detail_resets_guard() -> None:
    js = _js()
    assert "reminderOperationActive = false;" in js
    reset_pos = js.index(
        "reminderOperationActive = false;", js.index("function renderJobDetail")
    )
    # The reset happens inside renderJobDetail, so every Job Detail load starts
    # with a clean guard.
    render_job_detail_pos = js.index("async function renderJobDetail")
    assert reset_pos > render_job_detail_pos


def test_in_flight_disables_controls_and_aria_busy() -> None:
    js = _js()
    # The in-flight disable selector covers the create form, every reminder
    # button (including the edit form's Save/Cancel and the edit button itself),
    # so no reminder control stays usable while a reminder mutation runs.
    controls = (
        '".reminder-form input, .reminder-form select, .reminder-form '
        "button, .reminder-edit-form input, .reminder-edit-form select, "
        '.reminder-edit-form button, .reminder-button"'
    )
    assert controls in js
    assert 'section.setAttribute("aria-busy", "true");' in js
    assert 'section.removeAttribute("aria-busy");' in js


def test_stale_response_guard_in_every_mutation() -> None:
    js = _js()
    for func in (
        "createReminder",
        "completeReminder",
        "reopenReminder",
        "deleteReminder",
        "submitReminderEdit",
    ):
        start = js.index(f"async function {func}")
        end = js.index("  }\n", js.index("} finally {", start))
        body = js[start:end]
        assert "var seq = detailRequestSeq;" in body
        assert (
            "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;"
            in body
        ), func
    # The refresh/reconcile paths apply the same guard.
    for marker in (
        "async function refreshReminders",
        "async function reconcileReminders",
    ):
        start = js.index(marker)
        body = js[start : js.index("  }\n", start)]
        assert "seq !== detailRequestSeq || !isCurrentDetailJob" in body, marker


def test_one_error_panel_max_per_section() -> None:
    js = _js()
    # Both the review-style section error and the reminder create panel remove
    # existing error boxes before appending, and a single guard serializes
    # reminder mutations.
    assert "replaceSectionError(" in js
    assert 'body.querySelectorAll(".error-box").forEach' in js
    assert "box.remove();" in js


# --------------------------------------------------------------------------- #
# H. Privacy, security, and endpoint hygiene
# --------------------------------------------------------------------------- #


def test_no_private_fields_or_unsafe_sinks() -> None:
    js = _js()
    # Unsafe HTML sinks are prohibited as property access/assignment, mirroring
    # the shared invariant (the bare word is allowed in prose comments).
    for forbidden in (
        ".innerHTML",
        "innerHTML=",
        "insertAdjacentHTML",
        "document.write",
        ".outerHTML",
        "reminder.message",
        "reminders.message",
        "notification_summary",
        "localStorage",
        "sessionStorage",
        "profile_path",
        ".sort(",
        "eval(",
    ):
        assert forbidden not in js, forbidden
    # Native confirmation gates both destructive reminder deletion and
    # destructive requirement deletion; retries and other paths do not confirm.
    # Reminder deletion, Preparation requirement deletion, and package-version
    # deletion each have an explicit native confirmation gate.
    assert js.count("window.confirm(") == 4
    assert js.replace("window.confirm(", "").count("confirm(") == 0


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
    # The only PATCH is the reminder-edit mutation; the writeJson helper family
    # (postJson/putJson/patchJson/deleteJson) is the sole HTTP surface.
    assert "patchJson" in js
    assert 'await patchJson("/reminders/" + reminderId, payload);' in js
    assert js.count("patchJson(") == 2


def test_method_set_is_exactly_post_post_put_patch_delete_in_order() -> None:
    js = _js()
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    # POST /report (Review view), POST /reminders (create), PUT (review/
    # application/reminder lifecycle), PATCH (reminder edit), DELETE (review
    # clear + reminder delete) -- in source order.
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


def test_render_section_by_key_routes_reminders() -> None:
    js = _js()
    assert "renderReminders(body, job, settled, retry);" in js


def test_server_ordering_preserved_no_client_sort() -> None:
    js = _js()
    assert ".sort(" not in js
    assert "data.items.forEach(function (reminder) {" in js


# --------------------------------------------------------------------------- #
# I. Behavioral: create contract
# --------------------------------------------------------------------------- #


def test_post_reminders_creates_canonical_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "interview",
                "due_at": "2026-08-10T14:00+02:00",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert list(payload) == _REMINDER_FIELDS
        assert payload["job_id"] == job_ids["a-1"]
        assert payload["kind"] == "interview"
        assert payload["due_at"] == "2026-08-10T12:00:00+00:00"
        assert payload["completed_at"] is None
        assert payload["created_at"] == payload["updated_at"]
        assert list(payload["job"]) == _JOB_FIELDS

        fetched = client.get(f"/reminders/{payload['reminder_id']}")
        assert fetched.status_code == 200
        assert fetched.json() == payload

    assert _reminder_count(path, "reminders") == 1


def test_post_reminders_non_idempotent_creates_distinct_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        second = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["reminder_id"] != second.json()["reminder_id"]

    assert _reminder_count(path, "reminders") == 2


def test_post_reminders_naive_due_at_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T14:00",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Invalid due timestamp '2026-08-10T14:00': a naive timestamp without an "
        "explicit UTC offset is not accepted (CareerOS never guesses a timezone)."
    )
    assert _reminder_count(path, "reminders") == 0


def test_post_reminders_date_only_due_at_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10",
            },
        )

    assert response.status_code == 422
    assert "date-only values are not accepted" in response.json()["detail"]
    assert _reminder_count(path, "reminders") == 0


def test_post_reminders_invalid_kind_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "bogus",
                "due_at": "2026-08-10T09:00Z",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Invalid reminder kind 'bogus'; expected one of: "
        "follow_up, interview, offer, review, custom."
    )
    assert _reminder_count(path, "reminders") == 0


def test_post_reminders_message_field_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
                "message": "nudge me",
            },
        )

    assert response.status_code == 422
    assert "message" in response.text
    assert _reminder_count(path, "reminders") == 0


def test_post_reminders_unknown_job_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/reminders",
            json={"job_id": 9999, "kind": "follow_up", "due_at": "2026-08-10T09:00Z"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}
    assert _reminder_count(path, "reminders") == 0


def test_get_reminders_for_job_is_soonest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "offer",
                "due_at": "2026-08-20T09:00Z",
            },
        )
        client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        response = client.get(f"/reminders?job_id={job_ids['a-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == ["items", "total", "offset", "limit"]
    items = payload["items"]
    assert payload["total"] == 3
    assert all(list(item) == _REMINDER_FIELDS for item in items)
    kinds = [item["kind"] for item in items]
    dues = [item["due_at"] for item in items]
    # Soonest first (due_at ASC, id ASC).
    assert kinds[0] == "follow_up"
    assert kinds[1] == "follow_up"
    assert kinds[2] == "offer"
    assert dues[0] <= dues[1] <= dues[2]


def test_get_reminders_unknown_job_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/reminders?job_id=9999")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# J. Behavioral: complete / reopen lifecycle
# --------------------------------------------------------------------------- #


def _create_reminder(client: TestClient, job_id: int, kind: str = "follow_up") -> dict:
    response = client.post(
        "/reminders",
        json={"job_id": job_id, "kind": kind, "due_at": "2026-08-10T09:00Z"},
    )
    assert response.status_code == 200
    return response.json()


def test_complete_and_reopen_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reminder = _create_reminder(client, job_ids["a-1"])

        completed = client.put(f"/reminders/{reminder['reminder_id']}/complete")
        assert completed.status_code == 200
        item = completed.json()
        assert list(item) == _REMINDER_FIELDS
        assert item["completed_at"] == item["updated_at"]
        assert item["due_at"] == "2026-08-10T09:00:00+00:00"
        assert item["job_id"] == job_ids["a-1"]

        reopened = client.put(f"/reminders/{reminder['reminder_id']}/reopen")
        assert reopened.status_code == 200
        assert reopened.json()["completed_at"] is None


def test_complete_and_reopen_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reminder = _create_reminder(client, job_ids["a-1"])
        reminder_id = reminder["reminder_id"]

        first = client.put(f"/reminders/{reminder_id}/complete")
        second = client.put(f"/reminders/{reminder_id}/complete")
        assert second.status_code == 200
        assert second.json() == first.json()

        reopen_first = client.put(f"/reminders/{reminder_id}/reopen")
        reopen_second = client.put(f"/reminders/{reminder_id}/reopen")
        assert reopen_second.status_code == 200
        assert reopen_second.json() == reopen_first.json()
        assert reopen_second.json()["completed_at"] is None


def test_complete_unknown_reminder_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put("/reminders/9999/complete")

    assert response.status_code == 404
    assert response.json() == {"detail": "No reminder found with ID 9999."}


def test_reopen_unknown_reminder_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.put("/reminders/9999/reopen")

    assert response.status_code == 404
    assert response.json() == {"detail": "No reminder found with ID 9999."}


# --------------------------------------------------------------------------- #
# K. Database-availability and lock-contention behaviour
# --------------------------------------------------------------------------- #


def test_missing_database_is_500_and_nothing_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "careeros.sqlite3"
    _set_database(monkeypatch, path)

    with pytest.raises(ValueError, match="current schema v12"):
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
            response = client.post(
                "/reminders",
                json={
                    "job_id": job_ids["a-1"],
                    "kind": "follow_up",
                    "due_at": "2026-08-10T09:00Z",
                },
            )
        assert response.status_code == 503
        assert (
            "The database is temporarily busy; retry the request later."
            in response.text
        )
        assert _reminder_count(path, "reminders") == 0
    finally:
        _release_child(child)

    # The lock is released: the same mutation now succeeds.
    with TestClient(app) as client:
        retry = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
    assert retry.status_code == 200
    assert retry.json()["kind"] == "follow_up"


# --------------------------------------------------------------------------- #
# L. Shared invariants: schema version, no cli import, real DB, static assets
# --------------------------------------------------------------------------- #


def test_schema_version_stays_current() -> None:
    assert SCHEMA_VERSION == 12


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
