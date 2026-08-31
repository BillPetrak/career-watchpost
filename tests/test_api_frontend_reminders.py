"""Focused tests for the Reminders overview view (``#reminders``).

Covers the read-only reminders-overview milestone: the new ``#reminders`` nav
item and view section, the recognized route (with the unknown-hash fallback
untouched), the exact three GET ``/reminders`` queries (overdue / due soon /
upcoming later) derived from ONE captured ``now`` and a 24-hour horizon, the
lossless/mutually-exclusive boundary semantics (exact ``now`` and exact
``now+24h`` both land in Due soon, just past the horizon in Upcoming,
completed reminders never queried), the read-only rendering of public fields
with a ``#job/<id>`` View-details link, browser-local time rendering from
offset-bearing ISO values (no invented seconds, no client-side timestamp
shortcut), the empty/error/loading behaviors (global empty state, omitted
empty sections, per-section retry, whole-view error when every section fails,
stale-response token), the GET-only same-origin safe-DOM contract, and the
standard hygiene invariants (``SCHEMA_VERSION`` 8, real default database
untouched, no ``careeros.cli`` import under the API).

Like the other frontend slices these checks are intentionally light and
stdlib/Python-test-only: this slice has no Node toolchain, so there is no
Jest/Vitest/Playwright.
"""

from __future__ import annotations

import ast
import importlib.resources
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

from careeros.api import app
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.reminders.service import add_reminder, complete_reminder
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

_STATIC_DIR = importlib.resources.files("careeros.api").joinpath("static")

_HTML_NAME = "index.html"
_JS_NAME = "app.js"
_CSS_NAME = "style.css"

# The single authoritative "now" the overview captures per render, and the
# 24-hour horizon derived from it (REMINDERS_HORIZON_MS).
_NOW_ISO = "2026-08-10T12:00:00Z"
_HORIZON_ISO = "2026-08-11T12:00:00Z"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _asset_bytes(name: str) -> bytes:
    return _STATIC_DIR.joinpath(name).read_bytes()


def _js() -> str:
    return _asset_bytes(_JS_NAME).decode("utf-8")


def _html() -> str:
    return _asset_bytes(_HTML_NAME).decode("utf-8")


def _css() -> str:
    return _asset_bytes(_CSS_NAME).decode("utf-8")


def _overview_slice(js: str) -> str:
    """The Reminders overview view source section, bounded by its comment."""
    start = js.index("// Reminders overview view (#reminders)")
    end = js.index("// Job Detail view (#job/<id>)", start)
    return js[start:end]


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(source: str, source_job_id: str, title: str) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Tech Corp",
        location="Berlin",
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed(path: Path, now: str = _NOW_ISO) -> dict[str, int]:
    """Seed four jobs and five open reminders spanning every overview section.

    Times (relative to ``now`` = 2026-08-10T12:00:00Z and the +24h horizon):
      follow_up   now - 1h     -> Overdue
      interview   exactly now  -> Due soon (after=now is inclusive)
      offer       exactly +24h -> Due soon (before=now+24h is inclusive)
      review      +24h + 1min  -> Upcoming
      custom      now - 1h     -> completed (must never be returned)

    Returns a mapping of reminder kind to reminders.id.
    """
    now_dt = datetime.fromisoformat(now)
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "py-1", "Python Developer"),
            _make_job("remotive", "go-2", "Go Engineer"),
            _make_job("remoteok", "fs-3", "Frontend Developer"),
            _make_job("workingnomads", "ds-4", "Data Scientist"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    add_reminder(
        connection,
        job_id=job_ids["py-1"],
        kind="follow_up",
        due_at=(now_dt - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        message="secret-reminder-message",
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["py-1"],
        kind="interview",
        due_at=now,
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["go-2"],
        kind="offer",
        due_at=(now_dt + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["go-2"],
        kind="review",
        due_at=(now_dt + timedelta(hours=24, minutes=1))
        .isoformat()
        .replace("+00:00", "Z"),
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["fs-3"],
        kind="custom",
        due_at=(now_dt - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        now=_EPOCH,
    )
    reminder_ids = {
        row["kind"]: row["id"]
        for row in connection.execute("SELECT id, kind FROM reminders ORDER BY id")
    }
    complete_reminder(connection, reminder_id=reminder_ids["custom"], now=_EPOCH)
    connection.close()
    return reminder_ids


def _overview_urls(now: str, horizon: str) -> dict[str, str]:
    """Rebuilds the three overview query strings exactly as the frontend does."""
    return {
        "overdue": "/reminders?" + urlencode({"state": "overdue", "as_of": now}),
        "dueSoon": "/reminders?"
        + urlencode({"state": "open", "after": now, "before": horizon}),
        "upcoming": "/reminders?" + urlencode({"state": "upcoming", "as_of": horizon}),
    }


def _fetch_overview(client: TestClient) -> dict[str, list[dict]]:
    urls = _overview_urls(_NOW_ISO, _HORIZON_ISO)
    sections: dict[str, list[dict]] = {}
    for key, url in urls.items():
        response = client.get(url)
        assert response.status_code == 200, (key, response.text)
        sections[key] = response.json()["items"]
    return sections


# --------------------------------------------------------------------------- #
# A. Navigation and routing
# --------------------------------------------------------------------------- #


def test_reminders_nav_item_added_after_review() -> None:
    html = _html()
    nav = html[html.index('<nav class="nav"') : html.index("</nav>")]
    review_pos = nav.index('id="nav-review"')
    reminders_pos = nav.index('id="nav-reminders"')
    assert '<a href="#reminders" id="nav-reminders">Reminders</a>' in nav
    assert review_pos < reminders_pos


def test_reminders_view_section_exists() -> None:
    html = _html()
    assert 'id="view-reminders" class="view" data-view="reminders" hidden' in html
    assert (
        'id="reminders-status" class="status" role="status" aria-live="polite"' in html
    )
    assert 'id="reminders-content" class="view-content"' in html


def test_reminders_route_recognized() -> None:
    js = _js()
    assert 'var REMINDERS = "reminders";' in js
    assert "raw === REMINDERS" in js
    assert re.search(r"else\s+if\s+\(\s*view\s*===\s*REMINDERS\s*\)", js)
    assert "await renderRemindersView();" in js
    assert "if (leaving === REMINDERS && name !== REMINDERS) {" in js
    assert "remindersRequestSeq++;" in js


def test_unknown_hash_fallback_preserved() -> None:
    js = _js()
    # Unknown/absent/non-view hashes still fall back exactly as before.
    assert "return null;" in js
    assert "view = HOME;" in js
    assert "view || visibleView() || HOME" in js


# --------------------------------------------------------------------------- #
# B. Section queries and classification contract
# --------------------------------------------------------------------------- #


def test_overview_queries_overdue_section() -> None:
    js = _js()
    urls = _overview_slice(js)
    assert "function reminderOverviewUrls(nowIso, horizonIso)" in urls
    assert 'overdue.set("state", "overdue");' in urls
    assert 'overdue.set("as_of", nowIso);' in urls
    assert 'overdue: "/reminders?" + overdue,' in urls


def test_overview_queries_due_soon_section() -> None:
    js = _js()
    urls = _overview_slice(js)
    assert 'dueSoon.set("state", "open");' in urls
    assert 'dueSoon.set("after", nowIso);' in urls
    assert 'dueSoon.set("before", horizonIso);' in urls
    assert 'dueSoon: "/reminders?" + dueSoon,' in urls


def test_overview_queries_upcoming_section() -> None:
    js = _js()
    urls = _overview_slice(js)
    assert 'upcoming.set("state", "upcoming");' in urls
    assert 'upcoming.set("as_of", horizonIso);' in urls
    assert 'upcoming: "/reminders?" + upcoming,' in urls


def test_single_authoritative_now_derives_horizon() -> None:
    js = _js()
    view = _overview_slice(js)
    assert "var REMINDERS_HORIZON_MS = 24 * 60 * 60 * 1000;" in view
    assert "var now = new Date();" in view
    assert "var nowIso = utcTimestamp(now);" in view
    assert "utcTimestamp(new Date(now.getTime() + REMINDERS_HORIZON_MS))" in view
    # Exactly one captured now per render -- the three section requests all
    # share it (and the derived horizon); nothing is re-computed per section.
    assert view.count("var now = new Date();") == 1


def test_no_client_side_classification_or_sorting() -> None:
    js = _js()
    view = _overview_slice(js)
    # The overview never re-implements the time comparison: classification is
    # entirely the endpoint's state/as_of/before/after semantics.
    assert "due_at <" not in view
    assert "due_at >" not in view
    assert ".sort(" not in view
    assert "data.items.forEach(function (reminder) {" in view
    # The three sections are named in display order.
    assert '{ key: "overdue", heading: "Overdue" },' in view
    assert '{ key: "dueSoon", heading: "Due soon" },' in view
    assert '{ key: "upcoming", heading: "Upcoming" },' in view


def test_no_completed_reminder_query() -> None:
    js = _js()
    assert "state=completed" not in js
    view = _overview_slice(js)
    # The overview only ever asks for open reminders (state=overdue/open) or
    # open-reminders-after-a-point (state=upcoming); the completed state is
    # never requested and completed_at is never read.
    assert "completed" not in view


# --------------------------------------------------------------------------- #
# C. Boundary behavior (endpoint-level, mirroring the exact overview queries)
# --------------------------------------------------------------------------- #


def test_exact_now_boundary_lands_in_due_soon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        sections = _fetch_overview(client)

    due_soon = {item["reminder_id"] for item in sections["dueSoon"]}
    assert ids["interview"] in due_soon
    assert ids["interview"] not in {item["reminder_id"] for item in sections["overdue"]}
    assert ids["interview"] not in {
        item["reminder_id"] for item in sections["upcoming"]
    }


def test_exact_horizon_boundary_lands_in_due_soon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        sections = _fetch_overview(client)

    due_soon = {item["reminder_id"] for item in sections["dueSoon"]}
    assert ids["offer"] in due_soon
    assert ids["offer"] not in {item["reminder_id"] for item in sections["upcoming"]}


def test_just_past_horizon_lands_in_upcoming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        sections = _fetch_overview(client)

    upcoming = {item["reminder_id"] for item in sections["upcoming"]}
    assert ids["review"] in upcoming
    assert ids["review"] not in {item["reminder_id"] for item in sections["dueSoon"]}


def test_sections_mutually_exclusive_and_lossless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        sections = _fetch_overview(client)

    overdue = {item["reminder_id"] for item in sections["overdue"]}
    due_soon = {item["reminder_id"] for item in sections["dueSoon"]}
    upcoming = {item["reminder_id"] for item in sections["upcoming"]}
    # Every open reminder appears in exactly one section.
    open_reminders = {ids["follow_up"], ids["interview"], ids["offer"], ids["review"]}
    assert overdue | due_soon | upcoming == open_reminders
    assert overdue.isdisjoint(due_soon)
    assert overdue.isdisjoint(upcoming)
    assert due_soon.isdisjoint(upcoming)
    assert len(overdue) + len(due_soon) + len(upcoming) == len(open_reminders)


def test_completed_reminders_never_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        sections = _fetch_overview(client)

    all_ids = {item["reminder_id"] for section in sections.values() for item in section}
    assert ids["custom"] not in all_ids


def test_section_server_order_rendered_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        sections = _fetch_overview(client)

    # The endpoint returns each section ordered due_at ASC, then id ASC; the
    # overview renders that order verbatim (no client sort exists anywhere).
    assert [item["kind"] for item in sections["overdue"]] == ["follow_up"]
    assert [item["kind"] for item in sections["dueSoon"]] == ["interview", "offer"]
    assert [item["kind"] for item in sections["upcoming"]] == ["review"]


# --------------------------------------------------------------------------- #
# D. Rendering
# --------------------------------------------------------------------------- #


def test_row_renders_public_fields_only() -> None:
    js = _js()
    view = _overview_slice(js)
    assert "function buildReminderOverviewRow(reminder)" in view
    assert "reminderKindLabel(reminder.kind)" in view
    assert "displayLocalDateTime(reminder.due_at)" in view
    assert "job.title" in view
    assert "job.company" in view
    assert 'textContent: "View details"' in view
    # Only the public reminder fields are read: the kind label, the due time,
    # the embedded job's id/title/company. No message, note, history, or
    # workflow state.
    assert "reminder.reminder_id" not in view


def test_view_details_links_to_job_detail() -> None:
    js = _js()
    view = _overview_slice(js)
    assert 'href: "#job/" + job.id' in view
    assert "isFiniteNumber(job.id) && job.id >= 1" in view


def test_no_mutation_controls_on_overview() -> None:
    js = _js()
    view = _overview_slice(js)
    for forbidden in (
        "completeReminder",
        "reopenReminder",
        "deleteReminder",
        "createReminder",
        "submitReminderEdit",
        "buildReminderActions",
        "buildReminderCreateForm",
        "postJson(",
        "putJson(",
        "patchJson(",
        "deleteJson(",
        "window.confirm(",
    ):
        assert forbidden not in view, forbidden


def test_local_time_rendering_from_offset_iso() -> None:
    js = _js()
    view = _overview_slice(js)
    assert "function displayLocalDateTime(value)" in view
    assert "Date.UTC(" in view
    assert "offsetMinutes * 60 * 1000" in view
    assert "local.getFullYear()" in view
    assert "local.getMonth()" in view
    assert "local.getDate()" in view
    assert "local.getHours()" in view
    assert "local.getMinutes()" in view
    assert 'return "—";' in view
    # Python restatement of the exact algorithm: the offset is parsed from the
    # string so the instant is exact, then formatted in the local timezone.
    cases = [
        ("2026-08-10T14:00:00+02:00", 2, "2026-08-10 14:00"),
        ("2026-08-10T12:00:00Z", 2, "2026-08-10 14:00"),
        ("2026-08-10T14:00:00+02:00", 0, "2026-08-10 12:00"),
        ("not-a-timestamp", 2, "—"),
    ]
    for value, local_tz_hours, expected in cases:
        assert _restate_display_local(value, local_tz_hours) == expected, value


def _restate_display_local(value: str, local_tz_hours: int) -> str:
    """Python restatement of app.js ``displayLocalDateTime`` for a fixed zone."""
    match = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
        r"(Z|[+-]\d{2}:\d{2})$",
        value,
    )
    if not match:
        return "—"
    offset_minutes = 0
    if match.group(7) != "Z":
        sign = -1 if match.group(7).startswith("-") else 1
        offset_minutes = sign * (
            int(match.group(7)[1:3]) * 60 + int(match.group(7)[4:6])
        )
    instant = datetime(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
        int(match.group(5)),
        int(match.group(6)) if match.group(6) else 0,
        tzinfo=UTC,
    ) - timedelta(minutes=offset_minutes)
    local = instant + timedelta(hours=local_tz_hours)
    return local.strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# E. Empty / error / loading
# --------------------------------------------------------------------------- #


def test_global_empty_state_when_all_empty() -> None:
    js = _js()
    view = _overview_slice(js)
    assert 'textContent: "No reminders."' in view
    assert "container.remove();" in view
    # The empty note is shown only when every section succeeded with no rows
    # and no section failed: the guard combines the three outcomes.
    assert "!renderedAny && !failedAny && succeededAny" in view


def test_empty_sections_omitted() -> None:
    js = _js()
    view = _overview_slice(js)
    assert "if (data.items.length === 0) {" in view
    assert "container.remove();" in view
    assert 'return "empty";' in view


def test_section_failure_retries_only_that_section() -> None:
    js = _js()
    view = _overview_slice(js)
    assert (
        "function fillRemindersSection(key, state, container, body, nowIso, horizonIso)"
        in view
    )
    assert 'state.status === "rejected"' in view
    assert (
        "async function retryRemindersSection(key, container, body, nowIso, horizonIso)"
        in view
    )
    assert "await fetchJson(urls[key]);" in view
    assert "sectionErrorBox(" in view
    # The Retry closure re-issues only the failing section's request.
    assert "retryRemindersSection(key, container, body, nowIso, horizonIso)" in view


def test_whole_view_error_when_all_fail() -> None:
    js = _js()
    view = _overview_slice(js)
    assert "async function renderRemindersView()" in view
    assert "Promise.allSettled(" in view
    assert "renderError(status, error.message, renderRemindersView)" in view
    assert (
        'results.overdue.status === "rejected"'
        " ? results.overdue.reason.message : null" in view
    )
    assert "doneLoading(status);" in view


def test_stale_response_guard() -> None:
    js = _js()
    view = _overview_slice(js)
    assert "var seq = ++remindersRequestSeq;" in view
    assert "var seq = remindersRequestSeq;" in view
    assert "if (seq !== remindersRequestSeq) return;" in view
    # navigate() bumps the token the moment the user leaves the view.
    assert "if (leaving === REMINDERS && name !== REMINDERS) {" in js
    assert "remindersRequestSeq++;" in js


# --------------------------------------------------------------------------- #
# F. Security, privacy, and hygiene
# --------------------------------------------------------------------------- #


def test_overview_get_only_same_origin_safe_dom() -> None:
    js = _js()
    view = _overview_slice(js)
    assert "await fetchJson(urls[key]);" in view
    assert "fetchJson(" in view
    for forbidden in (
        ".innerHTML",
        "innerHTML=",
        "insertAdjacentHTML",
        "document.write",
        ".outerHTML",
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "toISOString",
    ):
        assert forbidden not in view, forbidden
    # The overview adds no new HTTP method: it only uses the GET-only
    # fetchJson helper, so the exact method set is unchanged.
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    assert methods == ["POST", "POST", "PUT", "PATCH", "DELETE"]


def test_private_fields_never_rendered_or_requested() -> None:
    js = _js()
    view = _overview_slice(js)
    for forbidden in (
        ".description",
        ".source_job_id",
        ".group_id",
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
    ):
        assert forbidden not in view, forbidden


def test_no_filesystem_or_config_access() -> None:
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


def test_schema_version_stays_10() -> None:
    assert SCHEMA_VERSION == 10


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    database = tmp_path / "careeros.sqlite3"
    connection = connect(database)
    initialize_schema(connection)
    connection.close()
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


def test_static_assets_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "careeros.sqlite3"
    connection = connect(database)
    initialize_schema(connection)
    connection.close()
    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    with TestClient(app) as client:
        for path, expected_type in (
            ("/ui", "text/html"),
            ("/ui/app.js", "application/javascript"),
            ("/ui/style.css", "text/css"),
        ):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-type"].startswith(expected_type), path
