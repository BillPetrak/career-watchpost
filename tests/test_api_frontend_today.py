"""Static contract checks for the server-authoritative Today workspace."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

_STATIC = Path(str(resources.files("careeros.api").joinpath("static")))


def _asset(name: str) -> str:
    return _STATIC.joinpath(name).read_text(encoding="utf-8")


def _today_slice(js: str) -> str:
    start = js.index("// Today workspace (#today)")
    end = js.index("// Job Detail view (#job/<id>)", start)
    return js[start:end]


def test_today_is_first_nav_and_home_remains_default() -> None:
    html = _asset("index.html")
    nav = html[html.index('<nav class="nav"') : html.index("</nav>")]
    assert nav.index('id="nav-today"') < nav.index('id="nav-home"')
    js = _asset("app.js")
    assert 'var TODAY = "today";' in js
    assert "if (view === TODAY)" in js
    assert "view = HOME;" in js
    assert "view || visibleView() || HOME" in js


def test_today_uses_one_authoritative_get_and_exact_sections() -> None:
    js = _asset("app.js")
    today = _today_slice(js)
    assert 'fetchJson("/today?limit=10")' in today
    assert '"Overdue reminders"' in today
    assert '"Due later today"' in today
    assert '"Shortlisted, not started"' in today
    assert today.index('"Overdue reminders"') < today.index('"Due later today"')
    assert today.index('"Due later today"') < today.index('"Shortlisted, not started"')
    assert ".sort(" not in today
    assert "groupBy" not in today and "join(" not in today
    assert "getUTC" not in today and "Date.parse" not in today
    assert "classified by the server using UTC" in today


def test_today_safe_dom_and_public_fields_only() -> None:
    today = _today_slice(_asset("app.js"))
    assert "textContent" in today
    assert "innerHTML" not in today and "insertAdjacentHTML" not in today
    assert "reminder.message" not in today and "reminder.note" not in today
    assert "createElement" not in today or "el(" in today


def test_today_empty_states_and_focus_recovery() -> None:
    today = _today_slice(_asset("app.js"))
    assert "Nothing needs attention today." in today
    assert "Nothing here today." in today
    assert "today-boundary-note" in today
    assert "todayFocusTarget" in today
    assert "restoreTodayFocus" in today
    assert "data-reminder-id" in today
    assert "status.focus()" in today


def test_today_independent_generation_and_concurrency_guards() -> None:
    today = _today_slice(_asset("app.js"))
    for name in (
        "todayViewGeneration",
        "todayRequestSeq",
        "todayDataGeneration",
        "todayRefreshActive",
        "todayRefreshOwner",
        "todayRefreshDirty",
        "todayOperationSeq",
        "todayReminderInFlight",
    ):
        assert name in today or name in _asset("app.js")
    assert "todayRefreshDirty = true" in today
    assert "while (todayRefreshDirty" in today
    assert "if (todayRefreshOwner === owner)" in today
    assert "todayReminderInFlight[reminder.reminder_id]" in today
    assert "requestTodayRefresh(true)" in today


def test_today_put_retry_and_get_only_retry_contract() -> None:
    today = _today_slice(_asset("app.js"))
    assert 'putJson("/reminders/" + reminder.reminder_id + "/complete", {})' in today
    assert '"The reminder could not be completed."' in today
    assert "completeTodayReminder(card, reminder)" in today
    assert "error.status === 404" in today
    assert '"Reminder saved, but Today could not be refreshed.' in today
    # A refresh retry calls GET-only renderTodayView and does not replay PUT.
    assert "renderTodayView({ refresh: true })" in today


def test_today_job_detail_origin_preserves_existing_origins() -> None:
    js = _asset("app.js")
    assert "jobDetailOrigin = TODAY" in js
    assert 'href: "#today"' in js
    assert 'textContent: "Back to Today"' in js
    assert "jobDetailOrigin === REVIEWED" in js
    assert "jobDetailOrigin === APPLICATIONS" in js
    assert ' : "Back to jobs"' in js


def test_today_accessibility_contract() -> None:
    html = _asset("index.html")
    assert 'id="today-status" class="status" role="status" aria-live="polite"' in html
    assert 'id="today-content" class="view-content"' in html
    js = _asset("app.js")
    assert 'setLoading(status, "Loading today' in js
    assert 'status.parentNode.removeAttribute("aria-busy")' in js
    assert 'label: "Complete reminder"' in js
