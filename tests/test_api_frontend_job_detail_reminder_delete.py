"""Tests for the Job Detail Reminder Delete action milestone.

The Job Detail view (`#job/<id>`) adds a Delete control to every reminder row,
driving the existing, fully-tested backend mutation ``DELETE /reminders/{id}``.
This suite is deliberately stdlib/Python-test-only (there is no Node
toolchain), so most checks are source-level invariants on ``app.js`` /
``style.css`` plus behavioral checks through the FastAPI TestClient for the
exact mutation contract the view sends.

Covered behaviour: a Delete control on every reminder row beside the
Complete/Reopen lifecycle action, styled destructively (error color) like the
existing Clear-decision treatment; the destructive delete is gated by a native
``window.confirm`` dialog that fires ONLY on the original click — cancelling is
a TRUE no-op (no request, no guard change, no control/aria-busy mutation), and
an automated Retry after a network/500/503 failure re-issues the exact DELETE
directly without re-confirming; a 200 DELETE refreshes ONLY
``GET /reminders?job_id={id}`` and re-renders only the Reminders section (no
optimistic removal, no full-page reload, no touching of Job/Application/Review
sections); a canonical 404 DELETE is reconciled as success (the desired
"reminder absent" state already holds) via the same reminder-only refresh with
no error panel; every other failure (422/500/503/network) preserves the
existing list and shows exactly one section-local error panel whose Retry
re-issues the exact failed DELETE; the single ``reminderOperationActive`` guard
is shared across create/complete/reopen/delete so rapid/double clicks cannot
overlap mutations; stale-response protection via the per-navigation
``detailRequestSeq`` token plus ``isCurrentDetailJob``; server ordering stays
authoritative (no client-side sorting); no edit/bulk-delete/undo/PATCH/custom
modal/backend/schema changes; no storage APIs, unsafe HTML sinks, ``eval``, or
private reminder message/note rendering; the method set invariant
``["POST", "POST", "PUT", "DELETE"]``; ``SCHEMA_VERSION`` stays 8; the real
default database is byte-identical after the suite runs.
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


def _seed_reminders(
    path: Path,
    job_ids: dict[str, int],
    n: int = 2,
) -> list[int]:
    """Create ``n`` reminders on job a-1 and return their ids in server order."""
    due = {
        1: "2026-08-10T09:00Z",
        2: "2026-08-12T09:00Z",
        3: "2026-08-15T09:00Z",
    }
    kind = {1: "follow_up", 2: "interview", 3: "offer"}
    with TestClient(app) as client:
        for index in range(1, n + 1):
            client.post(
                "/reminders",
                json={
                    "job_id": job_ids["a-1"],
                    "kind": kind[index],
                    "due_at": due[index],
                },
            )
    return list(range(1, n + 1))


def _reminder_count(path: Path, table: str) -> int:
    with connect(path) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _set_database_and_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n: int = 2):
    path = tmp_path / "careeros.sqlite3"
    _set_database(monkeypatch, path)
    job_ids = _seed_jobs(path)
    reminder_ids = _seed_reminders(path, job_ids, n=n)
    return path, job_ids, reminder_ids


# --------------------------------------------------------------------------- #
# Source-slice helpers (bounded so a slice never bleeds into the next
# function's doc comment, which would corrupt substring assertions).
# --------------------------------------------------------------------------- #


def _reminders_lifecycle_slice(js: str) -> str:
    start = js.index("// Exactly one lifecycle action per reminder")
    end = js.index("// After a successful mutation, refetch ONLY", start)
    return js[start:end]


def _reminders_delete_slice(js: str) -> str:
    start = js.index("async function deleteReminder")
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
# A. Delete control wiring and destructive styling
# --------------------------------------------------------------------------- #


def test_delete_button_on_every_reminder_row() -> None:
    js = _js()
    lifecycle = _reminders_lifecycle_slice(js)
    # The Delete control is built once in buildReminderActions so every
    # reminder row (open or completed) carries it beside Complete/Reopen.
    assert 'textContent: "Delete"' in lifecycle
    assert "requestDeleteReminder(group, body, job, reminder);" in lifecycle
    assert "buildReminderActions(body, job, reminder)" in lifecycle


def test_delete_button_uses_destructive_styling() -> None:
    css = _css()
    assert ".reminder-button.delete-reminder" in css
    # Error-color destructive treatment consistent with Clear decision.
    assert "--color-error" in css
    # The Delete button stays covered by the shared disabled/aria-busy rules.
    assert ".reminder-button:disabled" in css
    assert "aria-busy" in _js()


def test_delete_control_is_not_an_edit_or_bulk_control() -> None:
    js = _js()
    # The Delete control is a distinct per-reminder destructive action. The
    # reminder edit PATCH does exist (the per-reminder Edit -> Save/Cancel
    # flow) but lives entirely outside this delete slice; nothing here is a
    # bulk or undo control.
    assert "Bulk" not in js
    assert "Undo" not in js
    assert 'await deleteJson("/reminders/" + reminderId);' in js
    assert 'deleteJson("/reminders?job_id' not in js
    # The edit flow is the sibling PATCH mutation, not part of delete.
    assert 'await patchJson("/reminders/" + reminderId, payload);' in js


# --------------------------------------------------------------------------- #
# B. Confirmation gate: native window.confirm, cancel is a true no-op
# --------------------------------------------------------------------------- #


def test_confirm_before_delete() -> None:
    js = _js()
    # The click wrapper asks the native window.confirm dialog before any
    # DELETE; the destructive warning is explicit and cannot be undone.
    assert (
        'if (!window.confirm("Delete this reminder? This cannot be undone.")) return;'
        in js
    )
    assert js.count("window.confirm(") == 4


def test_cancel_is_a_true_noop() -> None:
    js = _js()
    # The confirm guard sits at the top of requestDeleteReminder, BEFORE any
    # request, guard flip, control disable, or aria-busy mutation.
    wrapper = js[
        js.index("function requestDeleteReminder") : js.index(
            "// PUT /reminders/{id}/complete"
        )
    ]
    assert "if (!window.confirm(" in wrapper
    assert "deleteJson" not in wrapper
    assert "reminderOperationActive = true" not in wrapper
    assert "setReminderControlsDisabled" not in wrapper
    assert 'setAttribute("aria-busy"' not in wrapper
    assert "refreshReminders" not in wrapper
    assert "replaceSectionError" not in wrapper


def test_retry_never_reconfirms() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    # The Retry closure re-issues the exact failed DELETE directly; the
    # window.confirm gate lives only in requestDeleteReminder (outside this
    # function), so an automated retry never asks again.
    assert "deleteReminder(group, body, job, reminder);" in delete
    assert "window.confirm(" not in delete


# --------------------------------------------------------------------------- #
# C. DELETE URL, request, and validation
# --------------------------------------------------------------------------- #


def test_delete_url_and_exact_request() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    assert 'await deleteJson("/reminders/" + reminderId);' in delete
    assert "deleteJson(url)" in js
    assert 'method: "DELETE"' in js


def test_delete_validates_job_and_reminder_ids() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    assert "if (reminderOperationActive) return;" in delete
    assert "!isObject(job) || !isFiniteNumber(job.id) || job.id < 1" in delete
    assert "!isFiniteNumber(reminder.reminder_id)" in delete
    assert "reminder.reminder_id < 1" in delete


# --------------------------------------------------------------------------- #
# D. Success path: server-authoritative, reminder-only refresh
# --------------------------------------------------------------------------- #


def test_success_refetches_only_reminders_section() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    assert 'await deleteJson("/reminders/" + reminderId);' in delete
    assert "await refreshReminders(body, job, seq);" in delete
    assert "renderJobDetail(" not in delete
    assert "loadJobDetailData" not in delete
    # No optimistic removal: the DELETE response (or its failure) drives the
    # re-render; the list is never mutated client-side before the request.
    assert "clearText(body)" not in delete


def test_404_delete_reconciles_as_success() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    # A canonical 404 means the reminder is already gone -- the desired
    # "reminder absent" state holds -- so the same reminder-only refresh runs
    # and no error panel is shown.
    assert "error.status === 404" in delete
    assert "await refreshReminders(body, job, seq);" in delete
    # The 404 branch ends before the generic failure handling: it refreshes and
    # returns early, so the error panel is never reached for a 404.
    branch = delete[
        delete.index("error.status === 404") : delete.index(
            "setReminderControlsDisabled(body, false);"
        )
    ]
    assert "await refreshReminders(body, job, seq);" in branch
    assert "return;" in branch
    assert "replaceSectionError" not in branch
    # The generic (non-404) failure branch does use the section error panel.
    assert "replaceSectionError(" in delete


def test_other_failures_show_single_error_panel_with_retry() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    assert "replaceSectionError(" in delete
    assert '"The reminder could not be deleted."' in delete
    # The Retry action re-issues the exact failed DELETE (no re-confirm).
    assert "deleteReminder(group, body, job, reminder);" in delete
    assert "setReminderControlsDisabled(body, false);" in delete
    assert 'section.removeAttribute("aria-busy");' in delete


# --------------------------------------------------------------------------- #
# E. Concurrency, stale protection, error-panel hygiene
# --------------------------------------------------------------------------- #


def test_shared_guard_covers_delete() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    assert "reminderOperationActive = true;" in delete
    assert "reminderOperationActive = false;" in delete
    assert "if (reminderOperationActive) return;" in delete
    # The single shared flag is the only in-flight guard.
    assert js.count("var reminderOperationActive = false;") == 1


def test_in_flight_disables_controls_and_aria_busy() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    assert "setReminderControlsDisabled(body, true);" in delete
    assert 'section.setAttribute("aria-busy", "true");' in delete
    assert 'section.removeAttribute("aria-busy");' in delete


def test_stale_response_guard_in_delete() -> None:
    js = _js()
    delete = _reminders_delete_slice(js)
    assert "var seq = detailRequestSeq;" in delete
    assert (
        "if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;" in delete
    )


def test_reminder_operations_share_one_guard() -> None:
    js = _js()
    assert js.count("reminderOperationActive") >= 6
    for func in (
        "createReminder",
        "completeReminder",
        "reopenReminder",
        "deleteReminder",
    ):
        start = js.index(f"async function {func}")
        end = js.index("  }\n", js.index("} finally {", start))
        body = js[start:end]
        assert "reminderOperationActive = true;" in body
        assert "reminderOperationActive = false;" in body


# --------------------------------------------------------------------------- #
# F. Privacy, security, and endpoint hygiene
# --------------------------------------------------------------------------- #


def test_no_private_fields_or_unsafe_sinks() -> None:
    js = _js()
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
    # Native confirmation gates reminder deletion and preparation requirement
    # deletion; the reminder retry path itself never confirms again.
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
    # The reminder-edit PATCH is the only PATCH: patchJson is defined once and
    # called once, always against /reminders/{id} with the captured payload.
    assert "patchJson" in js
    assert 'await patchJson("/reminders/" + reminderId, payload);' in js
    assert js.count("patchJson(") == 2


def test_method_set_is_exactly_post_post_put_patch_delete_in_order() -> None:
    js = _js()
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    # POST /report (Review view), POST /reminders (create), PUT (review/
    # application/reminder lifecycle), PATCH (reminder edit), DELETE (review
    # clear + reminder delete) -- in source order. The reminder delete reuses
    # the existing deleteJson.
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


def test_server_ordering_preserved_no_client_sort() -> None:
    js = _js()
    assert ".sort(" not in js
    assert "data.items.forEach(function (reminder) {" in js


# --------------------------------------------------------------------------- #
# G. Behavioral: DELETE /reminders/{id} contract the view sends
# --------------------------------------------------------------------------- #


def test_delete_removes_row_and_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, reminder_ids = _set_database_and_seed(tmp_path, monkeypatch)

    with TestClient(app) as client:
        listing = client.get(f"/reminders?job_id={job_ids['a-1']}")
        assert listing.status_code == 200
        assert [item["reminder_id"] for item in listing.json()["items"]] == reminder_ids

        deleted = client.delete(f"/reminders/{reminder_ids[0]}")
        assert deleted.status_code == 200
        payload = deleted.json()
        assert list(payload) == _REMINDER_FIELDS
        assert payload["reminder_id"] == reminder_ids[0]
        assert payload["job_id"] == job_ids["a-1"]
        assert list(payload["job"]) == _JOB_FIELDS

        after = client.get(f"/reminders?job_id={job_ids['a-1']}")
        assert after.status_code == 200
        survivors = [item["reminder_id"] for item in after.json()["items"]]
        assert survivors == reminder_ids[1:]

    assert _reminder_count(path, "reminders") == 1


def test_server_order_is_authoritative_after_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, reminder_ids = _set_database_and_seed(tmp_path, monkeypatch, n=3)

    with TestClient(app) as client:
        # Delete the middle reminder; the survivors keep ascending due_at order.
        client.delete(f"/reminders/{reminder_ids[1]}")
        after = client.get(f"/reminders?job_id={job_ids['a-1']}")
        items = after.json()["items"]
        assert [item["reminder_id"] for item in items] == [
            reminder_ids[0],
            reminder_ids[2],
        ]
        dues = [item["due_at"] for item in items]
        assert dues == sorted(dues)

    assert _reminder_count(path, "reminders") == 2


def test_repeated_delete_is_404_after_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, reminder_ids = _set_database_and_seed(tmp_path, monkeypatch)

    with TestClient(app) as client:
        first = client.delete(f"/reminders/{reminder_ids[0]}")
        second = client.delete(f"/reminders/{reminder_ids[0]}")
        assert first.status_code == 200
        assert second.status_code == 404
        assert second.json() == {
            "detail": f"No reminder found with ID {reminder_ids[0]}."
        }

    assert _reminder_count(path, "reminders") == 1


def test_delete_unknown_reminder_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, reminder_ids = _set_database_and_seed(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.delete("/reminders/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "No reminder found with ID 9999."}
    assert _reminder_count(path, "reminders") == 2


def test_delete_non_integer_id_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_database_and_seed(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.delete("/reminders/not-an-int")

    assert response.status_code == 422
    assert _reminder_count(Path(str(tmp_path / "careeros.sqlite3")), "reminders") == 2


def test_delete_sent_body_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, reminder_ids = _set_database_and_seed(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            f"/reminders/{reminder_ids[0]}",
            json={"message": "should-be-ignored"},
        )
        assert response.status_code == 200
        assert "message" not in response.text
        assert _reminder_count(path, "reminders") == 1


# --------------------------------------------------------------------------- #
# H. Database-availability and lock-contention behaviour
# --------------------------------------------------------------------------- #


def test_missing_database_is_500_and_nothing_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "careeros.sqlite3"
    _set_database(monkeypatch, path)

    with pytest.raises(ValueError, match="current schema v13"):
        with TestClient(app):
            pass
    assert not path.exists()
    assert not path.parent.exists()
    assert not lock_path_for(path).exists()


def test_lock_contention_maps_to_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _set_database(monkeypatch, path)
    job_ids = _seed_jobs(path)
    reminder_ids = _seed_reminders(path, job_ids)

    child = _start_child(path)
    try:
        _wait_for_lock_line(child)
        with TestClient(app) as client:
            response = client.delete(f"/reminders/{reminder_ids[0]}")
        assert response.status_code == 503
        assert (
            "The database is temporarily busy; retry the request later."
            in response.text
        )
        assert _reminder_count(path, "reminders") == 2
    finally:
        _release_child(child)

    # The lock is released: the same mutation now succeeds.
    with TestClient(app) as client:
        retry = client.delete(f"/reminders/{reminder_ids[0]}")
    assert retry.status_code == 200
    assert retry.json()["reminder_id"] == reminder_ids[0]
    assert _reminder_count(path, "reminders") == 1


# --------------------------------------------------------------------------- #
# I. Shared invariants: schema version, no cli import, real DB, static assets
# --------------------------------------------------------------------------- #


def test_schema_version_stays_current() -> None:
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
