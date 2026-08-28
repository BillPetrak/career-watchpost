"""Tests for the Job Detail per-reminder edit milestone (Edit -> Save/Cancel).

The Job Detail view (`#job/<id>`) gains an inline per-reminder edit flow that
changes a reminder's ``kind`` and/or ``due_at`` through the existing, fully
tested backend ``PATCH /reminders/{id}`` endpoint. This suite is deliberately
stdlib/Python-test-only (there is no Node toolchain), so most checks are
source-level invariants on ``app.js`` / ``style.css`` plus behavioral checks
through the FastAPI TestClient for the exact mutation contract the view sends.

Covered behaviour: an Edit control on EVERY reminder (open AND completed, never
a bulk/undo control) that precedes the lifecycle action; entering edit mode
replaces ONLY the clicked reminder's row with the inline form (kind select +
datetime-local due input + Save + Cancel, accessible labels, no reminder
message field) and focuses the kind select; the form pre-populates the current
kind and the due instant in the browser's local timezone via
``localDatetimeValue`` (the minimal explicit-offset reverse conversion with
minute granularity, never inventing seconds); the ORIGINAL values are captured
at edit entry and a Save compares against those, so a kind-only edit sends
exactly ``{"kind": ...}`` -- the untouched due time is never re-serialized and
a CLI-created reminder with second precision keeps its stored value
byte-for-byte -- while a due-only edit sends exactly ``{"due_at": ...}``
serialized with the offset for the ENTERED date (DST respected), and both
changed sends both fields; changing nothing is a local no-op (exit edit mode,
restore the row, ZERO network requests); a cancelled Save restores the display
row from the ORIGINAL server values and returns focus to the Edit button;
the actual edit mutation (``submitReminderEdit``) runs under the shared
``reminderOperationActive`` guard with controls disabled and the section
``aria-busy``, re-fetches ONLY ``GET /reminders?job_id={id}`` on success (no
optimistic update), reconciles a PATCH 404 as the desired "reminder absent"
state via the same reminder-only refresh with no error panel, keeps edit mode
and the entered values on a deterministic 422 (showing the server detail next
to the form, never auto-retrying the invalid payload), and shows at most one
section error panel on every other failure whose Retry re-issues the EXACT
failed PATCH payload (the retry closure owns the payload, not the form);
stale-response protection via the per-navigation ``detailRequestSeq`` token
(which ``navigate()`` also invalidates the moment the user LEAVES the Job
Detail view, so an in-flight reminder save/refresh can never update the DOM
after navigation away); ``patchJson`` is defined once and called once, always against
``/reminders/{id}``; ``PATCH`` is the only new HTTP method (the method-set
invariant is ``["POST", "POST", "PUT", "PATCH", "DELETE"]``); no storage APIs,
unsafe HTML sinks, private fields, or client-side sorting; the shared schema
version, no-``careeros.cli``-import, real-default-database, and static-asset
invariants.
"""

from __future__ import annotations

import ast
import importlib.resources
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import lock_path_for

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


def _edit_actions_slice(js: str) -> str:
    start = js.index("function buildReminderActions")
    end = js.index("// Deleting a reminder is destructive", start)
    return js[start:end]


def _edit_entry_slice(js: str) -> str:
    start = js.index("function startReminderEdit")
    end = js.index("function buildReminderEditForm", start)
    return js[start:end]


def _edit_form_slice(js: str) -> str:
    start = js.index("function buildReminderEditForm")
    end = js.index("function cancelReminderEdit", start)
    return js[start:end]


def _edit_cancel_slice(js: str) -> str:
    start = js.index("function cancelReminderEdit")
    end = js.index("function saveReminderEdit", start)
    return js[start:end]


def _edit_save_slice(js: str) -> str:
    start = js.index("function saveReminderEdit")
    end = js.index("async function submitReminderEdit", start)
    return js[start:end]


def _edit_submit_slice(js: str) -> str:
    start = js.index("async function submitReminderEdit")
    end = js.index("function replaceReminderEditError", start)
    return js[start:end]


def _edit_error_slice(js: str) -> str:
    start = js.index("function replaceReminderEditError")
    end = js.index("// Wiring", start)
    return js[start:end]


def _local_datetime_slice(js: str) -> str:
    start = js.index("function localDatetimeValue")
    end = js.index("function setReminderControlsDisabled", start)
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
# A. Edit control on every reminder (open AND completed), never a bulk control
# --------------------------------------------------------------------------- #


def test_edit_control_on_every_reminder() -> None:
    js = _js()
    actions = _edit_actions_slice(js)
    # The Edit control is unconditional: it exists for open AND completed
    # reminders alike (only the lifecycle button's label/action branches on
    # completed_at).
    assert 'className: "reminder-button reminder-edit-button"' in actions
    assert 'textContent: "Edit"' in actions
    assert 'editButton.setAttribute("aria-label", "Edit reminder")' in actions
    assert "startReminderEdit(group.parentNode, body, job, reminder);" in actions
    # The Edit control is appended BEFORE the lifecycle button is even built,
    # so its presence never depends on the completed state.
    edit_pos = actions.index("group.appendChild(editButton);")
    lifecycle_def = actions.index('var button = el("button", {')
    assert edit_pos < lifecycle_def


def test_edit_precedes_lifecycle_and_delete_controls() -> None:
    js = _js()
    actions = _edit_actions_slice(js)
    # Source order: Edit is appended first, then the single lifecycle action,
    # then the confirmed Delete control.
    edit_pos = actions.index("group.appendChild(editButton);")
    lifecycle_pos = actions.index("group.appendChild(button);")
    delete_pos = actions.index("group.appendChild(deleteButton);")
    assert edit_pos < lifecycle_pos < delete_pos
    assert 'className: "reminder-button delete-reminder"' in actions


# --------------------------------------------------------------------------- #
# B. Entering edit mode: only the clicked row, kind focus, shared guard
# --------------------------------------------------------------------------- #


def test_edit_entry_replaces_only_clicked_row() -> None:
    js = _js()
    entry = _edit_entry_slice(js)
    # The entry path replaces ONLY the clicked reminder's row contents with the
    # inline form; the section is never re-rendered and no other row is touched.
    assert "var form = buildReminderEditForm(body, job, reminder);" in entry
    assert "clearText(item);" in entry
    assert "item.appendChild(form);" in entry
    assert "renderReminders(" not in entry
    assert "refreshReminders(" not in entry
    assert "renderJobDetail(" not in entry


def test_edit_entry_focuses_kind_select() -> None:
    js = _js()
    entry = _edit_entry_slice(js)
    assert 'var kind = form.querySelector(".reminder-edit-kind");' in entry
    assert "if (kind) kind.focus();" in entry


def test_edit_entry_uses_shared_guard() -> None:
    js = _js()
    entry = _edit_entry_slice(js)
    assert "if (reminderOperationActive) return;" in entry
    # The guard check never flips the flag on entry: no operation starts here.
    assert "reminderOperationActive = true;" not in entry


# --------------------------------------------------------------------------- #
# C. The inline edit form: shape, accessibility, no message, prepopulation
# --------------------------------------------------------------------------- #


def test_edit_form_shape_and_accessibility() -> None:
    js = _js()
    form = _edit_form_slice(js)
    assert 'el("form", { className: "reminder-edit-form" })' in form
    assert 'setAttribute("aria-label", "Edit reminder")' in form
    assert '{ className: "reminder-label", textContent: "Kind" }' in form
    assert 'setAttribute("for", "reminder-edit-kind-" + reminder.reminder_id)' in form
    assert 'className: "reminder-input reminder-edit-kind",' in form
    assert 'setAttribute("name", "kind")' in form
    assert '{ className: "reminder-label", textContent: "Due" }' in form
    assert 'setAttribute("for", "reminder-edit-due-" + reminder.reminder_id)' in form
    assert 'className: "reminder-input reminder-edit-due",' in form
    assert 'type: "datetime-local",' in form
    assert 'setAttribute("name", "due_at")' in form
    assert 'setAttribute("required", "")' in form
    assert 'className: "reminder-add-button",\n      textContent: "Save"' in form
    assert 'className: "reminder-button",\n      textContent: "Cancel"' in form
    assert '{ className: "reminder-validation" }' in form
    # No reminder message field exists: the API never accepts or exposes one.
    assert "message" not in form


def test_edit_form_populates_all_five_kinds() -> None:
    js = _js()
    form = _edit_form_slice(js)
    assert "REMINDER_KINDS.forEach(function (entry) {" in form
    assert "{ textContent: entry.label }" in form
    assert "option.value = entry.value;" in form


def test_edit_form_prepopulates_from_server_values() -> None:
    js = _js()
    form = _edit_form_slice(js)
    # The current kind is pre-selected and the due instant is rendered in the
    # browser's local timezone via the reverse helper; the values always come
    # from the server-provided reminder, never from re-derived DOM state.
    assert "var originalKind = reminder.kind;" in form
    assert "var originalDueField = localDatetimeValue(reminder.due_at);" in form
    assert "kind.value = originalKind;" in form
    assert "due.value = originalDueField;" in form


def test_edit_form_captures_originals_and_passes_to_save() -> None:
    js = _js()
    form = _edit_form_slice(js)
    assert (
        "saveReminderEdit(form, body, job, reminder, originalKind, originalDueField);"
        in form
    )
    assert "cancelReminderEdit(form, body, job, reminder);" in form
    # The live due-hint updates as the user edits the due input.
    assert "updateDueHint(due, hint);" in form


# --------------------------------------------------------------------------- #
# D. Cancel is a true local no-op
# --------------------------------------------------------------------------- #


def test_cancel_restores_row_from_original_values() -> None:
    js = _js()
    cancel = _edit_cancel_slice(js)
    # Cancel rebuilds the display row from the ORIGINAL server values and
    # swaps it back in place, preserving the server order.
    assert "var fresh = buildReminderRow(body, job, reminder);" in cancel
    assert "item.parentNode.replaceChild(fresh, item);" in cancel
    # No network, no request helper, no section re-render.
    assert "patchJson" not in cancel
    assert "postJson" not in cancel
    assert "putJson" not in cancel
    assert "deleteJson" not in cancel
    assert "fetch" not in cancel
    assert "renderReminders(" not in cancel


def test_cancel_returns_focus_to_edit_button() -> None:
    js = _js()
    cancel = _edit_cancel_slice(js)
    assert 'var edit = fresh.querySelector(".reminder-edit-button");' in cancel
    assert "if (edit) edit.focus();" in cancel


# --------------------------------------------------------------------------- #
# E. Save: change detection and payload composition
# --------------------------------------------------------------------------- #


def test_no_change_save_is_local_noop_zero_network() -> None:
    js = _js()
    save = _edit_save_slice(js)
    # A no-change Save exits edit mode locally and makes ZERO network requests:
    # the early return happens BEFORE submitReminderEdit is ever reached.
    assert "var kindChanged = kind.value !== originalKind;" in save
    assert "var dueChanged = dueField !== originalDueField;" in save
    noop_pos = save.index("if (!kindChanged && !dueChanged) {")
    cancel_pos = save.index("cancelReminderEdit(form, body, job, reminder);")
    submit_pos = save.index("submitReminderEdit(form, body, job, reminder, payload);")
    assert noop_pos < cancel_pos < submit_pos
    assert "return;" in save
    # No request helper is reachable before the no-change early return.
    assert "patchJson" not in save


def test_kind_only_edit_sends_kind_without_reserializing_due() -> None:
    js = _js()
    save = _edit_save_slice(js)
    # The payload starts empty and each changed field is added independently:
    # a kind-only edit produces exactly {"kind": ...} and the untouched due
    # time is never re-serialized (stored seconds survive byte-for-byte).
    assert "var payload = {};" in save
    assert "if (kindChanged) payload.kind = kind.value;" in save
    due_pos = save.index("if (dueChanged) {")
    due_at_pos = save.index("payload.due_at = timestamp;")
    assert due_pos < due_at_pos


def test_due_only_and_both_changed_compose_payload() -> None:
    js = _js()
    save = _edit_save_slice(js)
    # The due branch is the ONLY place payload.due_at is set, and it runs only
    # when the due field actually changed, so a due-only edit sends exactly
    # {"due_at": ...} and both-changed sends both.
    assert "var timestamp = dueTimestamp(dueField);" in save
    assert "payload.due_at = timestamp;" in save
    # The due branch is inside the dueChanged guard.
    branch = save[
        save.index("if (dueChanged) {") : save.index("clearText(validation);")
    ]
    assert "payload.due_at = timestamp;" in branch


def test_invalid_local_due_is_client_validation_no_request() -> None:
    js = _js()
    save = _edit_save_slice(js)
    assert "if (timestamp === null) {" in save
    assert "validation.hidden = false;" in save
    message = (
        '"That due time is not valid in your local timezone; '
        'pick another date and time."'
    )
    assert message in save
    assert "due.focus();" in save
    # The invalid value is never sent: the branch returns before any submit.
    submit_pos = save.index("submitReminderEdit(form, body, job, reminder, payload);")
    assert save.index("if (timestamp === null) {") < submit_pos


def test_save_comparison_uses_captured_originals_not_reparsed_dom() -> None:
    js = _js()
    save = _edit_save_slice(js)
    # The comparison targets are the ORIGINAL values captured at edit entry,
    # never values re-derived from the DOM after entry.
    assert "kind.value !== originalKind" in save
    assert "dueField !== originalDueField" in save


# --------------------------------------------------------------------------- #
# F. The edit mutation: PATCH URL, refresh-only, guard, 404/422/other handling
# --------------------------------------------------------------------------- #


def test_submit_patches_reminder_and_refetches_only_section() -> None:
    js = _js()
    submit = _edit_submit_slice(js)
    assert 'await patchJson("/reminders/" + reminderId, payload);' in submit
    assert "await refreshReminders(body, job, seq);" in submit
    # Server-authoritative: no optimistic DOM mutation before the request.
    assert "renderReminders(" not in submit
    assert "renderJobDetail(" not in submit


def test_submit_uses_shared_guard_and_aria_busy() -> None:
    js = _js()
    submit = _edit_submit_slice(js)
    assert "if (reminderOperationActive) return;" in submit
    assert "reminderOperationActive = true;" in submit
    assert "setReminderControlsDisabled(body, true);" in submit
    assert 'section.setAttribute("aria-busy", "true");' in submit
    assert "reminderOperationActive = false;" in submit


def test_submit_stale_response_guard() -> None:
    js = _js()
    submit = _edit_submit_slice(js)
    assert "var seq = detailRequestSeq;" in submit
    assert (
        "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;" in submit
    )


def test_navigating_away_from_job_invalidates_in_flight_reminder_continuation() -> None:
    js = _js()
    # The stale-response race: a reminder Save is in flight (PATCH + reminder
    # refresh) when the user navigates back to Jobs. The refresh's seq was
    # captured at Save time, and the refresh only renders after awaiting its
    # own GET; navigating away must invalidate that captured seq so the late
    # reminder response can never pass the shared guard and update the DOM.
    submit = _edit_submit_slice(js)
    assert "var seq = detailRequestSeq;" in submit
    assert (
        "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;" in submit
    )
    # navigate() bumps the same token the moment the user LEAVES the Job Detail
    # view for another view, so every in-flight job-detail continuation
    # (reminder save/refresh included) is stale immediately -- not just when the
    # hash happens to read differently.
    start = js.index("async function navigate()")
    end = js.index("// DOM helpers", start)
    nav = js[start:end]
    assert "var leaving = visibleView();" in nav
    bump = "if (leaving === JOB && name !== JOB) {\n      detailRequestSeq++;"
    assert bump in nav
    # The invalidation happens BEFORE the target view is shown and is scoped to
    # actually leaving the job view: entering the job view (or staying on it,
    # e.g. a #main skip-link anchor) never bumps here -- renderJobDetail owns
    # the entry bump.
    assert nav.index(bump) < nav.index("showView(name);")
    assert "name !== JOB" in nav


def test_submit_404_reconciles_as_reminder_absent() -> None:
    js = _js()
    submit = _edit_submit_slice(js)
    # A PATCH 404 means the reminder is already gone -- the desired "reminder
    # absent" state holds -- so the reminder-only refresh runs with no panel.
    assert "if (error && error.status === 404) {" in submit
    four_oh_four = submit.index("error.status === 404")
    assert "await refreshReminders(body, job, seq);" in submit
    assert "return;" in submit
    # No error panel is produced for the 404 path.
    assert "replaceReminderEditError" not in submit[: submit.index("} catch (error) {")]
    assert submit.index("replaceReminderEditError") > four_oh_four


def test_submit_422_keeps_edit_mode_and_shows_server_detail() -> None:
    js = _js()
    submit = _edit_submit_slice(js)
    # A deterministic 422 keeps edit mode and the entered values: the controls
    # are re-enabled (shared with the other-failure path, so the user can
    # correct and Save again), the server detail is shown next to the form, and
    # the invalid payload is never auto-retried.
    assert "setReminderControlsDisabled(body, false);" in submit
    assert 'section.removeAttribute("aria-busy");' in submit
    assert "if (error && error.status === 422) {" in submit
    branch = submit[
        submit.index("if (error && error.status === 422) {") : submit.index(
            "replaceReminderEditError("
        )
    ]
    assert 'form.querySelector(".reminder-validation")' in branch
    assert "validation.hidden = false;" in branch
    assert "validation.textContent = error.message;" in branch
    assert "return;" in branch
    assert "replaceReminderEditError" not in branch
    assert "refreshReminders(" not in branch


def test_submit_other_failure_uses_single_panel_with_exact_payload_retry() -> None:
    js = _js()
    submit = _edit_submit_slice(js)
    # Every other failure (network/500/503) hands off to the single error panel
    # whose Retry re-issues the EXACT failed PATCH via the captured payload
    # (never re-reading the form, never re-confirming).
    assert "replaceReminderEditError(body, error, function () {" in submit
    assert "submitReminderEdit(form, body, job, reminder, payload);" in submit


def test_edit_error_panel_removes_existing_boxes_first() -> None:
    js = _js()
    error = _edit_error_slice(js)
    # One-error-panel-maximum: existing section error boxes are removed before a
    # fresh one is appended, so a failed edit never accumulates duplicate panels.
    assert 'body.querySelectorAll(".error-box").forEach(function (box) {' in error
    assert "box.remove();" in error
    assert '{ className: "error-box" }' in error
    assert 'textContent: "The reminder could not be saved."' in error
    assert '{ className: "retry-button", textContent: "Retry" }' in error
    assert "box.remove();" in error
    assert "retryAction();" in error


# --------------------------------------------------------------------------- #
# G. localDatetimeValue: minimal explicit-offset reverse conversion
# --------------------------------------------------------------------------- #


def test_local_datetime_value_reverse_conversion_present() -> None:
    js = _js()
    local = _local_datetime_slice(js)
    assert "function localDatetimeValue(value) {" in local
    # Seconds are optional on the input (the CLI may store them); the regex
    # accepts an explicit-offset timestamp with an optional seconds group.
    assert (
        r"/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
        r"(Z|[+-]\d{2}:\d{2})$/" in local.replace("\\d", "\\d")
    )
    # The UTC instant is reconstructed from the EXPLICIT offset (never today's
    # offset and never the local zone), then formatted with local components.
    assert "Date.UTC(year, month - 1, day, hour, minute, second)" in local
    assert "offsetMinutes * 60 * 1000" in local
    assert 'padStart(2, "0")' in local
    assert 'if (typeof value !== "string" || value.length === 0) return "";' in local
    assert 'if (!match) return "";' in local


def test_local_datetime_value_python_restatement() -> None:
    # Python-side restatement of the exact reverse conversion: parse the
    # explicit-offset timestamp (seconds optional) into the UTC instant, then
    # format it in a fixed local timezone as YYYY-MM-DDTHH:MM. Minute
    # granularity is intentional: datetime-local cannot express seconds.
    def reverse(value: str, local_offset_minutes: int) -> str:
        if not isinstance(value, str) or len(value) == 0:
            return ""
        match = re.fullmatch(
            r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
            r"(Z|[+-]\d{2}:\d{2})",
            value,
        )
        if not match:
            return ""
        year, month, day, hour, minute = (int(match[i]) for i in range(1, 6))
        second = int(match[6]) if match[6] else 0
        offset = match[7]
        offset_minutes = 0
        if offset != "Z":
            sign = -1 if offset[0] == "-" else 1
            offset_minutes = sign * (int(offset[1:3]) * 60 + int(offset[4:6]))
        # UTC instant from the explicit offset, then shift into the local zone.
        instant = datetime(
            year, month, day, hour, minute, second, tzinfo=UTC
        ) - timedelta(minutes=offset_minutes)
        local = instant + timedelta(minutes=local_offset_minutes)
        return local.strftime("%Y-%m-%dT%H:%M")

    # UTC+2 local zone (Europe/Berlin in summer).
    assert reverse("2026-08-10T14:00+02:00", 120) == "2026-08-10T14:00"
    assert reverse("2026-08-10T12:00:00Z", 120) == "2026-08-10T14:00"
    # Stored seconds are dropped only in the editor display, never in the wire
    # value (a kind-only Save never re-serializes the due time).
    assert reverse("2026-08-10T14:00:33+02:00", 120) == "2026-08-10T14:00"
    # Unusable / offset-less inputs yield the empty pre-population string.
    assert reverse("", 120) == ""
    assert reverse("2026-08-10T14:00", 120) == ""


def test_edit_due_serialization_reuses_shared_offset_contract() -> None:
    js = _js()
    save = _edit_save_slice(js)
    # The changed due is serialized with the SAME explicit-offset helper the
    # create form uses (offset derived from the ENTERED date, DST respected).
    assert "var timestamp = dueTimestamp(dueField);" in save


# --------------------------------------------------------------------------- #
# H. Hygiene: no unsafe sinks, private fields, sorting, or extra methods
# --------------------------------------------------------------------------- #


def test_edit_slice_has_no_unsafe_sinks_or_private_fields() -> None:
    js = _js()
    for label, start, end in (
        ("entry", "function startReminderEdit", "function buildReminderEditForm"),
        ("form", "function buildReminderEditForm", "function cancelReminderEdit"),
        ("cancel", "function cancelReminderEdit", "function saveReminderEdit"),
        ("save", "function saveReminderEdit", "async function submitReminderEdit"),
        (
            "submit",
            "async function submitReminderEdit",
            "function replaceReminderEditError",
        ),
        ("error", "function replaceReminderEditError", "// Wiring"),
    ):
        slice_ = js[js.index(start) : js.index(end)]
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
            assert forbidden not in slice_, (label, forbidden)


def test_no_private_filesystem_profile_or_log_access() -> None:
    combined = (_js() + _css()).lower()
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


def test_method_set_is_exactly_post_post_put_patch_delete_in_order() -> None:
    js = _js()
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    # POST /report (Review view), POST /reminders (create), PUT (review/
    # application/reminder lifecycle), PATCH (reminder edit), DELETE (review
    # clear + reminder delete) -- in source order.
    assert methods == ["POST", "POST", "PUT", "PATCH", "DELETE"]


def test_patch_json_defined_once_called_once_against_reminders() -> None:
    js = _js()
    assert "function patchJson(url, body) {" in js
    assert js.count("patchJson(") == 2
    assert 'await patchJson("/reminders/" + reminderId, payload);' in js
    # The only PATCH is the reminder edit; no other route is patched.
    assert 'await patchJson("/saved-searches' not in js
    # Matching Preview owns the sole ranked-jobs call site; reminder editing
    # still cannot issue or reuse it.
    assert js.count('"/ranked-jobs?grouped="') == 1
    assert '"/review-queue?grouped="' in js


def test_no_client_side_sort_and_server_order_preserved() -> None:
    js = _js()
    assert ".sort(" not in js
    assert "data.items.forEach(function (reminder) {" in js


# --------------------------------------------------------------------------- #
# I. Behavioral: PATCH contract the edit flow relies on
# --------------------------------------------------------------------------- #


def test_patch_kind_only_preserves_seconds_and_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T14:00:33Z",
            },
        )
        assert created.status_code == 200
        reminder_id = created.json()["reminder_id"]
        assert created.json()["due_at"] == "2026-08-10T14:00:33+00:00"

        completed = client.put(f"/reminders/{reminder_id}/complete")
        assert completed.status_code == 200
        assert completed.json()["completed_at"] is not None
        completed_at = completed.json()["completed_at"]

        # A kind-only edit (what the frontend sends for a kind-only Save) never
        # touches the due time: the stored seconds survive byte-for-byte, and
        # the completion state is preserved.
        edited = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
        assert edited.status_code == 200
        assert list(edited.json()) == _REMINDER_FIELDS
        assert edited.json()["kind"] == "offer"
        assert edited.json()["due_at"] == "2026-08-10T14:00:33+00:00"
        assert edited.json()["completed_at"] == completed_at
        assert edited.json()["updated_at"] != completed_at
        assert list(edited.json()["job"]) == _JOB_FIELDS

        detail = client.get(f"/reminders/{reminder_id}")
        assert detail.status_code == 200
        assert detail.json()["due_at"] == "2026-08-10T14:00:33+00:00"

    assert _reminder_count(path, "reminders") == 1


def test_patch_due_at_updates_and_normalizes_to_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T14:00:00Z",
            },
        )
        reminder_id = created.json()["reminder_id"]

        edited = client.patch(
            f"/reminders/{reminder_id}",
            json={"due_at": "2026-08-11T09:00+02:00"},
        )
        assert edited.status_code == 200
        assert edited.json()["kind"] == "follow_up"
        assert edited.json()["due_at"] == "2026-08-11T07:00:00+00:00"
        assert edited.json()["completed_at"] is None

    assert _reminder_count(path, "reminders") == 1


def test_patch_both_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        reminder_id = created.json()["reminder_id"]

        edited = client.patch(
            f"/reminders/{reminder_id}",
            json={"kind": "review", "due_at": "2026-08-12T10:00Z"},
        )
        assert edited.status_code == 200
        assert edited.json()["kind"] == "review"
        assert edited.json()["due_at"] == "2026-08-12T10:00:00+00:00"


def test_patch_unknown_reminder_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/reminders/9999", json={"kind": "offer"})

    assert response.status_code == 404
    assert response.json() == {"detail": "No reminder found with ID 9999."}


def test_patch_invalid_kind_or_due_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        reminder_id = created.json()["reminder_id"]

        bad_kind = client.patch(f"/reminders/{reminder_id}", json={"kind": "bogus"})
        assert bad_kind.status_code == 422
        assert "Invalid reminder kind 'bogus'" in bad_kind.json()["detail"]

        naive_due = client.patch(
            f"/reminders/{reminder_id}", json={"due_at": "2026-08-11T09:00"}
        )
        assert naive_due.status_code == 422
        assert (
            "naive timestamp without an explicit UTC offset"
            in (naive_due.json()["detail"])
        )

    # The failed PATCHes changed nothing.
    with TestClient(app) as client:
        detail = client.get(f"/reminders/{reminder_id}")
    assert detail.status_code == 200
    assert detail.json()["kind"] == "follow_up"
    assert detail.json()["due_at"] == "2026-08-10T09:00:00+00:00"


def test_patch_message_field_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        reminder_id = created.json()["reminder_id"]

        response = client.patch(
            f"/reminders/{reminder_id}",
            json={"kind": "offer", "message": "nudge me"},
        )
    assert response.status_code == 422
    assert _reminder_count(path, "reminders") == 1


def test_patch_idempotent_repeat_returns_equivalent_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        reminder_id = created.json()["reminder_id"]

        first = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
        second = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
        assert first.status_code == 200
        assert second.status_code == 200
        # Idempotent no-op: the repeated identical PATCH preserves timestamps.
        assert second.json()["updated_at"] == first.json()["updated_at"]


def test_patch_missing_database_is_500_and_nothing_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "careeros.sqlite3"
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.patch("/reminders/1", json={"kind": "offer"})

    assert response.status_code == 500
    assert "The database schema is unavailable" in response.text
    assert not path.exists()
    assert not path.parent.exists()
    assert not lock_path_for(path).exists()


def test_patch_lock_contention_maps_to_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_jobs(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        created = client.post(
            "/reminders",
            json={
                "job_id": job_ids["a-1"],
                "kind": "follow_up",
                "due_at": "2026-08-10T09:00Z",
            },
        )
        reminder_id = created.json()["reminder_id"]

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
        assert response.status_code == 503
        assert (
            "The database is temporarily busy; retry the request later."
            in response.text
        )
    finally:
        _release_child(child)

    # The lock is released: the same PATCH now succeeds.
    with TestClient(app) as client:
        retry = client.patch(f"/reminders/{reminder_id}", json={"kind": "offer"})
    assert retry.status_code == 200
    assert retry.json()["kind"] == "offer"


# --------------------------------------------------------------------------- #
# J. Shared invariants: schema version, no cli import, real DB, static assets
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
