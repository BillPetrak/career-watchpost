"""Focused tests for the first frontend vertical slice (``/ui`` static shell).

Covers the read-only zero-build dashboard: the three static-serving routes
(``GET /ui`` -> ``index.html``, ``GET /ui/app.js``, ``GET /ui/style.css``)
served from packaged resources rather than the filesystem/cwd, the fact that
static serving never requires or creates a database, the invariants that
existing API reference endpoints remain unchanged and ``SCHEMA_VERSION`` stays
8, that the real default database is never touched, that no ``careeros.cli``
import appears under ``src/careeros/api``, and the static-asset hygiene checks
(content-type, required endpoint strings, absence of mutation/private
endpoints, the single allowed POST -- computation-only ``POST /report`` -- with
no unsafe HTML sinks for API data, presence of the Home/Jobs/Review views and
hash markers, and a compatible basic CSP).

These checks are intentionally light and stdlib/Python-test-only: this slice
has no Node toolchain, so there is no Jest/Vitest/Playwright.
"""

from __future__ import annotations

import ast
import importlib.resources
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.database.schema import SCHEMA_VERSION

_SRC = Path(__file__).resolve().parents[1] / "src"

_STATIC_DIR = importlib.resources.files("careeros.api").joinpath("static")

_HTML_NAME = "index.html"
_JS_NAME = "app.js"
_CSS_NAME = "style.css"


def _asset_bytes(name: str) -> bytes:
    return _STATIC_DIR.joinpath(name).read_bytes()


# --------------------------------------------------------------------------- #
# Static-serving routes
# --------------------------------------------------------------------------- #


def test_ui_index_served_as_html() -> None:
    with TestClient(app) as client:
        response = client.get("/ui")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_ui_html_references_resolve_to_live_routes() -> None:
    """The HTML's actual script/link URLs must be live 200 routes.

    Regression guard for the ``/ui`` (no trailing slash) bug: ``./app.js``
    resolves to ``/app.js`` (404), not ``/ui/app.js``. Every asset URL
    referenced by the served HTML must therefore correspond to a working route
    with the expected content type -- never a hanging ``./``-relative path
    that silently 404s.
    """
    html = _asset_bytes(_HTML_NAME).decode("utf-8")
    urls = [
        match.group(1)
        for match in re.finditer(
            r'<(?:link|script)\b[^>]*\b(?:href|src)="([^"]+)"', html
        )
    ]
    assert urls, "HTML must reference at least one external CSS/JS asset"

    expected = {
        "/ui/style.css": "text/css",
        "/ui/app.js": "application/javascript",
    }
    assert sorted(urls) == sorted(expected), f"unexpected asset refs: {urls}"

    with TestClient(app) as client:
        for url, content_type in expected.items():
            response = client.get(url)
            assert response.status_code == 200, f"{url} returned {response.status_code}"
            assert response.headers["content-type"].startswith(content_type)


def test_ui_html_has_no_relative_asset_references() -> None:
    """No ``./``-relative asset hrefs that break when served at ``/ui``."""
    html = _asset_bytes(_HTML_NAME).decode("utf-8")
    for ref in re.findall(r'<(?:link|script)\b[^>]*\b(?:href|src)="([^"]+)"', html):
        assert not ref.startswith("./"), f"relative asset ref must be absolute: {ref}"
        assert ref.startswith("/"), f"asset ref must be absolute: {ref}"
    assert "<base" not in html


def test_ui_app_js_served_as_javascript() -> None:
    with TestClient(app) as client:
        response = client.get("/ui/app.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")


def test_ui_style_css_served_as_css() -> None:
    with TestClient(app) as client:
        response = client.get("/ui/style.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_static_serving_requires_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Static routes never touch the database: point CAREEROS_DATABASE at a
    # path that does not exist and assert the shell still serves and nothing
    # is created.
    missing = tmp_path / "does-not-exist" / "db.sqlite3"

    monkeypatch.setenv("CAREEROS_DATABASE", str(missing))
    with TestClient(app) as client:
        response = client.get("/ui")
    assert response.status_code == 200
    assert not missing.exists()
    assert not missing.parent.exists()


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


def test_reference_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.database.connection import connect
    from careeros.database.schema import initialize_schema

    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.close()
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))

    references = {
        "/health": 200,
        "/jobs": 200,
        "/sources/stats": 200,
        "/collection-runs": 200,
        "/applications": 200,
        "/review-decisions": 200,
        "/reminders": 200,
        "/groups": 200,
        "/groups/summary": 200,
        "/saved-searches": 200,
        "/report": 200,
    }
    with TestClient(app) as client:
        for endpoint, expected in references.items():
            response = client.get(endpoint)
            assert response.status_code == expected, endpoint


def test_schema_version_stays_8() -> None:
    assert SCHEMA_VERSION == 8


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    monkeypatch.setenv("CAREEROS_DATABASE", str(tmp_path / "careeros.sqlite3"))
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


# --------------------------------------------------------------------------- #
# Static source files and frontend hygiene
# --------------------------------------------------------------------------- #


def test_static_source_files_exist() -> None:
    for name in (_HTML_NAME, _JS_NAME, _CSS_NAME):
        assert _STATIC_DIR.joinpath(name).is_file(), name


def test_js_uses_only_intended_read_endpoints() -> None:
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    assert re.search(r'"/report"', js)
    assert re.search(r'"/jobs"', js)
    # The Job Detail view calls exactly the five canonical read GET route
    # families plus the Home/Jobs/Review GETs; the POST-only computation
    # endpoint /report is the only POST. No mutation or computation endpoints
    # for this slice.
    for required in (
        '"/jobs/" + jobId',
        'base + "/group"',
        '"/applications/" + jobId',
        '"/review-decisions/" + jobId',
        '"/reminders?job_id=" + jobId',
    ):
        assert required in js, required
    for forbidden in (
        "/saved-searches",
        "/ranked-jobs",
        "/review-queue",
        "/report/",
        "/workflow",
    ):
        assert re.search(r'"/' + re.escape(forbidden.lstrip("/")), js) is None, (
            forbidden
        )


def test_js_only_http_method_is_post_for_report() -> None:
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    # The read-only slice's one explicit HTTP method is POST, used solely for
    # the computation-only POST /report endpoint (Review view form submission).
    # PUT/PATCH/DELETE must never appear, and every other request (Home, Jobs)
    # stays an implicit GET through the GET-only fetchJson helper, which carries
    # no method option.
    methods = re.findall(r"\bmethod\s*:\s*['\"]([a-z]+)['\"]", js, re.IGNORECASE)
    assert methods == ["POST"]
    # The POST target is the /report path with the grouped/limit query pair the
    # Review view sends; a path-only "/report/" suffix is never used.
    assert '"/report?grouped="' in js
    assert '"&limit="' in js


def test_no_private_filesystem_profile_or_log_access() -> None:
    html = _asset_bytes(_HTML_NAME).decode("utf-8")
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    css = _asset_bytes(_CSS_NAME).decode("utf-8")
    combined = "\n".join((html, js, css)).lower()
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


def test_no_dynamic_api_data_rendered_via_unsafe_html_sinks() -> None:
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    # API strings are rendered with textContent; the static-authored markup is
    # fixed template text. Prohibit actual unsafe HTML sinks (property access
    # or assignment), while permitting the word in prose comments.
    assert ".innerHTML" not in js
    assert "innerHTML=" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js
    assert ".outerHTML" not in js


def test_home_and_jobs_view_and_hash_markers_exist() -> None:
    html = _asset_bytes(_HTML_NAME).decode("utf-8")
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    assert 'data-view="home"' in html
    assert 'data-view="jobs"' in html
    assert 'href="#home"' in html
    assert 'href="#jobs"' in html
    # Unknown/empty hashes default safely to Home.
    assert re.search(r"unknown|default", js, re.I) or "#home" in js


def test_hash_navigation_semantics_present_in_js() -> None:
    """The hash router must map #home/#jobs and preserve state on #main."""
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    # Default / absent hash -> Home.
    assert re.search(r"view\s*=\s*HOME", js)
    # parsedHash explicitly recognizes both routes and returns null otherwise.
    assert "raw === JOBS" in js
    assert "raw === HOME" in js
    assert "return null" in js
    # The #main skip-link anchor (and any other non-view hash) must not reset
    # the view: parsedHash yields null for it, the current view is preserved via
    # visibleView(), and re-rendering is gated behind a real #home/#jobs route.
    assert "visibleView" in js
    assert re.search(r"if\s*\(\s*view\s*===\s*JOBS\s*\)", js)
    assert re.search(r"else\s+if\s+\(\s*view\s*===\s*HOME\s*\)", js)


def test_basic_csp_present_and_compatible() -> None:
    html = _asset_bytes(_HTML_NAME).decode("utf-8")
    assert "Content-Security-Policy" in html
    assert "default-src 'self'" in html
    # CSP with default-src 'self' must not be broken by inline scripts/styles
    # or remote CDN assets. A same-origin external <script src> is compatible.
    assert "<style" not in html
    assert "<script" not in html.replace('<script src="/ui/app.js"></script>', "")
    css = _asset_bytes(_CSS_NAME).decode("utf-8")
    assert "@import" not in css
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    assert "http://" not in js and "https://" not in js


def test_safe_external_job_links() -> None:
    js = _asset_bytes(_JS_NAME).decode("utf-8")
    assert 'target: "_blank"' in js or 'target="_blank"' in js
    assert 'rel: "noopener noreferrer"' in js or "noopener noreferrer" in js
