"""Focused source invariants for the H2 VP2 presentation-only polish."""

from __future__ import annotations

import importlib.resources
import re

_STATIC = importlib.resources.files("careeros.api").joinpath("static")


def _asset(name: str) -> str:
    return _STATIC.joinpath(name).read_text(encoding="utf-8")


def test_navigation_keeps_every_destination_in_frozen_grouped_order() -> None:
    html = _asset("index.html")
    desktop_start = html.index('<div class="nav-desktop">')
    desktop = html[desktop_start : html.index("</nav>", desktop_start)]
    expected = [
        ("nav-today", "#today", "Today"),
        ("nav-home", "#home", "Overview"),
        ("nav-jobs", "#jobs", "Jobs"),
        ("nav-review", "#review", "Review"),
        ("nav-applications", "#applications", "Applications"),
        ("nav-reviewed", "#reviewed", "Reviewed"),
        ("nav-reminders", "#reminders", "Reminders"),
        ("nav-saved-searches", "#saved-searches", "Saved Searches"),
        ("nav-materials", "#materials", "Materials"),
        ("nav-matching", "#matching", "Matching Preferences"),
    ]
    positions = []
    for nav_id, href, label in expected:
        marker = f'<a href="{href}" id="{nav_id}">{label}</a>'
        assert html.count(f'id="{nav_id}"') == 1
        assert desktop.count(marker) == 1
        positions.append(desktop.index(marker))
    assert positions == sorted(positions)
    assert ">Home</a>" not in html
    assert desktop.count('class="nav-group ') == 3


def test_compact_navigation_is_native_and_needs_no_menu_javascript() -> None:
    html = _asset("index.html")
    css = _asset("style.css")
    js = _asset("app.js")
    assert '<details class="nav-disclosure">' in html
    assert "<summary>Navigation</summary>" in html
    assert '<nav class="nav nav-compact" aria-label="Primary">' in html
    assert '<div class="nav-desktop">' in html
    assert '<nav class="nav" aria-label="Primary">' in html
    assert '<details class="nav-disclosure" open>' not in html
    assert "@media (max-width: 74rem)" in css
    assert ".nav-disclosure:not([open]) > .nav" in css
    assert ".nav-disclosure[open] > .nav" in css
    assert ".nav-disclosure > .nav" in css
    assert ".nav-desktop" in css
    assert "nav-disclosure" not in js
    assert "aria-current" in js
    assert ".nav a[href^='#']" in js


def test_overview_stats_pair_each_label_and_value_in_one_item() -> None:
    js = _asset("app.js")
    home = js[js.index("function renderHomeContent") : js.index("// Jobs view")]
    assert 'var card = el("div", { className: "stat-card" });' in home
    assert 'card.appendChild(el("span", { className: "stat-label"' in home
    assert "card.appendChild(value);" in home
    assert "statCards.appendChild(card);" in home
    old_pattern = r'statCards\.appendChild\(\s*el\("div", \{ className: "stat-card"'
    assert re.search(old_pattern, home) is None


def test_overview_sections_use_semantic_headings() -> None:
    js = _asset("app.js")
    home = js[js.index("function renderHomeContent") : js.index("// Jobs view")]
    for heading in ("Application pipeline", "Actions", "Reminders", "Source health"):
        assert f'el("h2", {{ textContent: "{heading}" }})' in home
        assert f'className: "chunk", textContent: "{heading}"' not in home


def test_failed_source_health_has_explicit_failure_presentation() -> None:
    js = _asset("app.js")
    css = _asset("style.css")
    health = js[js.index("function buildHealthChunk") : js.index("// Jobs view")]
    assert 'source.last_succeeded === false ? "health-failure"' in health
    assert 'textContent: "Status: " + note' in health
    assert "health-ok" not in health
    assert ".health-state.health-failure" in css
    assert "color: var(--danger);" in css


def test_today_sections_and_order_remain_frozen() -> None:
    js = _asset("app.js")
    start = js.index("// Today workspace (#today)")
    today = js[start : js.index("// Job Detail view (#job/<id>)", start)]
    calls = [
        'buildTodaySection("Overdue reminders", overdue, buildTodayReminderCard)',
        (
            'buildTodaySection("Overdue application work", applicationOverdue, '
            "buildTodayApplicationCard)"
        ),
        'buildTodaySection("Reminders due later today", due, buildTodayReminderCard)',
        (
            'buildTodaySection("Application work due later today", applicationDue, '
            "buildTodayApplicationCard)"
        ),
        (
            'buildTodaySection("Application attention", applicationWork, '
            "buildTodayApplicationCard)"
        ),
        (
            'buildTodaySection("Shortlisted, not started", unstarted, '
            "buildTodayShortlistCard)"
        ),
        "content.appendChild(buildTodayReviewSection(reviewNext))",
    ]
    positions = [today.index(call) for call in calls]
    assert positions == sorted(positions)
    assert 'if (total === 0) section.className += " today-section-empty";' in today
    assert "if (total === 0) section.appendChild" in today


def test_visual_tokens_and_static_asset_boundaries() -> None:
    css = _asset("style.css")
    for token in (
        "--font-sans",
        "--text-title",
        "--space-8",
        "--surface-subtle",
        "--accent-strong",
        "--radius-sm",
        "--control-height",
        "--content-max",
        "--focus-ring",
        "--shadow-raised",
    ):
        assert css.count(token) >= 1
    assert "@import" not in css
    assert "url(http" not in css
    assert "linear-gradient" not in css
    assert "radial-gradient" not in css


def test_global_focus_indicator_is_solid_opaque_and_high_contrast() -> None:
    css = _asset("style.css")
    selectors = (
        "a:focus-visible",
        "button:focus-visible",
        "input:focus-visible",
        "textarea:focus-visible",
        "select:focus-visible",
        "summary:focus-visible",
    )
    rules = re.findall(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", css)
    body = next(
        (
            rule_body
            for rule_selectors, rule_body in rules
            if set(selectors)
            <= {selector.strip() for selector in rule_selectors.split(",")}
        ),
        None,
    )
    assert body is not None
    assert re.search(r"outline:\s*2px solid var\(--focus-ring\);", body)
    assert re.search(r"outline-offset:\s*2px;", body)
    assert "outline: none" not in body
    assert "box-shadow" not in body
    skip_focus = re.search(r"\.skip-link:focus\s*\{(?P<body>[^}]+)\}", css)
    assert skip_focus is not None
    assert re.search(r"top:\s*4px;", skip_focus.group("body"))
    assert re.search(r"left:\s*4px;", skip_focus.group("body"))

    focus_token = re.search(r"--focus-ring:\s*var\(--(?P<name>[\w-]+)\);", css)
    assert focus_token is not None
    color_match = re.search(
        rf"--{re.escape(focus_token.group('name'))}:\s*"
        r"#(?P<hex>[0-9a-fA-F]{6});",
        css,
    )
    assert color_match is not None

    def luminance(hex_color: str) -> float:
        channels = [int(hex_color[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    focus_luminance = luminance(color_match.group("hex"))
    for adjacent_surface in ("ffffff", "f5f6f8"):
        surface_luminance = luminance(adjacent_surface)
        lighter, darker = sorted((surface_luminance, focus_luminance), reverse=True)
        contrast = (lighter + 0.05) / (darker + 0.05)
        assert contrast >= 3
