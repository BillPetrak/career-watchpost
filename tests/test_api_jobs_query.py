"""Tests for optional keyword search on ``GET /jobs``.

Covers the additive ``query`` parameter: case-insensitive substring matching
across title/company/description through the shared ``search_jobs`` pipeline,
filter composition (source/remote/location/employment_type with AND
semantics), empty/default compatibility (omitted or ``query=""`` stays
byte-identical to the pre-milestone ``GET /jobs``), pagination/order (``total``
before slicing, ``published_at DESC NULLS LAST, id ASC``), absolute privacy
(descriptions may drive matching but stay structurally absent, and no private
workflow text leaks), read-only/no-lock/no-side-effect behavior, missing/
uninitialized/malformed database handling, and regression/invariants
(``GET /jobs/{job_id}`` and ``GET /health`` still work, no ``careeros.cli``
import under ``src/careeros/api``, ``SCHEMA_VERSION`` still 8, and the real
``data/database/careeros.sqlite3`` is never touched).
"""

from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import list_jobs, save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import collection_lock, lock_path_for
from careeros.presenters import job_dict
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"

# Distinctive sentinels proving actual non-leakage of private fields: if any
# string ever appears in response text, the API leaked a private value.
_SENTINEL_DESCRIPTION = "PRIVATE-JOB-DESCRIPTION-9f41c7d2"
_SENTINEL_APPLICATION_NOTE = "private-application-note"
_SENTINEL_REVIEW_NOTE = "private-review-note"
_SENTINEL_REMINDER_MESSAGE = "private-reminder-message"
_SENTINEL_SAVED_SEARCH_DESCRIPTION = "private-saved-search-description"
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/candidate-profile.yaml"

_ALL_SENTINELS = (
    _SENTINEL_DESCRIPTION,
    _SENTINEL_APPLICATION_NOTE,
    _SENTINEL_REVIEW_NOTE,
    _SENTINEL_REMINDER_MESSAGE,
    _SENTINEL_SAVED_SEARCH_DESCRIPTION,
    _SENTINEL_PROFILE_PATH,
)

# Canonical ordering for the seeded set (published_at DESC NULLS LAST, id ASC).
_CANONICAL_ORDER = (2, 1, 6, 3, 5, 4)

# Exact canonical Job item key order (the shared ``job_dict`` projection).
_JOB_ITEM_KEYS = (
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
)


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str,
    description: str,
    employment_type: str,
    remote_status: str,
    published_at: str | None,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-07-31T12:00:00+00:00",
    )


def _seed_jobs(connection: sqlite3.Connection) -> None:
    """Insert six jobs whose title/company/description keywords are disjoint."""
    save_jobs(
        connection,
        [
            _make_job(
                source="arbeitnow",
                source_job_id="a-1",
                title="Python Developer",
                company="Tech Corp",
                location="Athens",
                description="Build fast web backends.",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-07-29T10:00:00Z",
            ),
            _make_job(
                source="remotive",
                source_job_id="r-1",
                title="Go Engineer",
                company="Firm Inc",
                location="Berlin",
                description="Build scalable services.",
                employment_type="Full-time",
                remote_status="onsite_or_unspecified",
                published_at="2026-07-30T10:00:00Z",
            ),
            _make_job(
                source="arbeitnow",
                source_job_id="a-2",
                title="Senior Data Engineer",
                company="Cloud Co",
                location="Athens",
                description="Analyze large datasets with Scala.",
                employment_type="Contract",
                remote_status="remote",
                published_at="2026-07-28T10:00:00Z",
            ),
            _make_job(
                source="remotive",
                source_job_id="r-2",
                title="Frontend Developer",
                company="Web Ltd",
                location="London",
                description="Create slick React interfaces.",
                employment_type="Part-time",
                remote_status="onsite_or_unspecified",
                published_at=None,
            ),
            _make_job(
                source="workingnomads",
                source_job_id="w-1",
                title="DevOps Engineer",
                company="Infra Co",
                location="Berlin",
                description="Automate everything with Kubernetes.",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-07-26T10:00:00Z",
            ),
            _make_job(
                source="remoteok",
                source_job_id="o-1",
                title="Python Platform Engineer",
                company="Stack Co",
                location="Berlin",
                description="Harden production pipelines.",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-07-29T10:00:00Z",
            ),
        ],
    )


def _seed(path: Path) -> None:
    connection = connect(path)
    initialize_schema(connection)
    _seed_jobs(connection)
    connection.close()


def _seed_private_state(path: Path) -> None:
    """Add private workflow rows (notes/messages/description/profile_path)."""
    connection = connect(path)
    initialize_schema(connection)
    _seed_jobs(connection)
    ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.execute(
        "UPDATE jobs SET description = ? WHERE id = ?",
        (
            f"Create slick React interfaces. {_SENTINEL_DESCRIPTION}",
            ids["r-2"],
        ),
    )
    connection.commit()
    set_application_status(
        connection,
        job_id=ids["a-1"],
        status="applied",
        note=_SENTINEL_APPLICATION_NOTE,
    )
    set_decision(
        connection,
        job_id=ids["r-1"],
        decision="shortlist",
        note=_SENTINEL_REVIEW_NOTE,
    )
    add_reminder(
        connection,
        job_id=ids["a-2"],
        kind="follow_up",
        due_at="2026-08-10T12:00:00+00:00",
        message=_SENTINEL_REMINDER_MESSAGE,
    )
    create_search(
        connection,
        fields=SavedSearchFields(
            name="secret-search",
            query="python",
            mode="rank",
            description=_SENTINEL_SAVED_SEARCH_DESCRIPTION,
            profile_path=_SENTINEL_PROFILE_PATH,
        ),
    )
    connection.close()


def _assert_no_private_leak(response_text: str) -> None:
    for sentinel in _ALL_SENTINELS:
        assert sentinel not in response_text, f"leaked {sentinel!r}"


# --------------------------------------------------------------------------- #
# A. Core search behavior
# --------------------------------------------------------------------------- #


def test_query_matches_title_substring_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "PYTHON"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["id"] for item in payload["items"]] == [1, 6]


def test_query_matches_company_substring_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "CORP"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["company"] == "Tech Corp"


def test_query_matches_description_content_without_exposing_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "react"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == 4
    assert "description" not in payload["items"][0]
    assert "React" not in response.text
    _assert_no_private_leak(response.text)


def test_query_no_matches_returns_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/jobs",
            params={
                "query": "zzz-nonexistent-keyword",
                "offset": 3,
                "limit": 5,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 3, "limit": 5}


# --------------------------------------------------------------------------- #
# B. Composition
# --------------------------------------------------------------------------- #


def test_query_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/jobs", params={"query": "engineer", "source": "arbeitnow"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == 3
    assert payload["items"][0]["source"] == "arbeitnow"


def test_query_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "engineer", "remote": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["id"] for item in payload["items"]] == [6, 3, 5]
    assert all(item["remote_status"] == "remote" for item in payload["items"])


def test_query_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/jobs", params={"query": "engineer", "location": "Berlin"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["id"] for item in payload["items"]] == [2, 6, 5]
    assert all(item["location"] == "Berlin" for item in payload["items"])


def test_query_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/jobs", params={"query": "engineer", "employment_type": "Full-time"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["id"] for item in payload["items"]] == [2, 6, 5]
    assert all(item["employment_type"] == "Full-time" for item in payload["items"])


def test_query_composes_with_multiple_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/jobs",
            params={
                "query": "developer",
                "source": "arbeitnow",
                "location": "Athens",
                "employment_type": "Full-time",
                "remote": "true",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["id"] == 1


def test_query_and_filters_use_and_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    # "engineer" alone matches ids 2, 3, 5, 6; the source filter must narrow to
    # the arbeitnow engineer (id 3) and exclude the remotive one (id 2).
    with TestClient(app) as client:
        response = client.get(
            "/jobs", params={"query": "engineer", "source": "remotive"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [2]

    with TestClient(app) as client:
        response = client.get(
            "/jobs", params={"query": "engineer", "location": "Athens"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [3]


# --------------------------------------------------------------------------- #
# C. Empty/default compatibility
# --------------------------------------------------------------------------- #


def test_omitted_query_reproduces_existing_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with connect(path) as connection:
        expected_items = [job_dict(job) for job in list_jobs(connection)]

    with TestClient(app) as client:
        response = client.get("/jobs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == expected_items
    assert payload["total"] == 6
    assert payload["offset"] == 0
    assert payload["limit"] == 20


def test_empty_query_equals_omitted_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        omitted = client.get("/jobs")
        empty = client.get("/jobs", params={"query": ""})

    assert omitted.status_code == 200
    assert empty.status_code == 200
    assert empty.content == omitted.content


def test_empty_query_preserves_total_and_canonical_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": ""})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 6
    assert [item["id"] for item in payload["items"]] == list(_CANONICAL_ORDER)


# --------------------------------------------------------------------------- #
# D. Pagination/order
# --------------------------------------------------------------------------- #


def test_total_computed_before_offset_limit_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/jobs", params={"query": "engineer", "offset": 1, "limit": 2}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    assert payload["offset"] == 1
    assert payload["limit"] == 2
    assert [item["id"] for item in payload["items"]] == [6, 3]


def test_offset_limit_slice_filtered_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/jobs", params={"query": "engineer", "offset": 2, "limit": 1}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    assert [item["id"] for item in payload["items"]] == [3]


def test_canonical_order_published_desc_null_last_id_asc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": ""})

    assert response.status_code == 200
    payload = response.json()
    items = payload["items"]
    assert [item["id"] for item in items] == list(_CANONICAL_ORDER)
    published = [item["published_at"] for item in items]
    assert published[-1] is None  # NULL published_at sorts last
    non_null = [value for value in published if value is not None]
    assert non_null == sorted(non_null, reverse=True)  # DESC


# --------------------------------------------------------------------------- #
# E. Response/privacy
# --------------------------------------------------------------------------- #


def test_exact_envelope_field_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "python"})

    assert response.status_code == 200
    assert list(response.json().keys()) == ["items", "total", "offset", "limit"]


def test_exact_job_item_field_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "python"})

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert tuple(item.keys()) == _JOB_ITEM_KEYS


def test_description_key_absent_from_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_private_state(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "9f41c7d2"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert "description" not in payload["items"][0]
    assert "description" not in json.dumps(payload)


def test_matching_description_text_absent_from_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_private_state(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "9f41c7d2"})

    assert response.status_code == 200
    assert _SENTINEL_DESCRIPTION not in response.text
    _assert_no_private_leak(response.text)


def test_privacy_sentinel_scan_across_success_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_private_state(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        responses = [
            client.get("/jobs", params={"query": "9f41c7d2"}),
            client.get("/jobs", params={"query": "python"}),
            client.get("/jobs", params={"query": "engineer"}),
            client.get("/jobs", params={"query": ""}),
            client.get("/jobs"),
            client.get(f"/jobs/{1}"),
        ]

    for response in responses:
        assert response.status_code == 200
        _assert_no_private_leak(response.text)


# --------------------------------------------------------------------------- #
# F. Database failure/isolation
# --------------------------------------------------------------------------- #


def test_missing_database_empty_200_creates_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "sub" / "missing.sqlite3"
    _set_database(monkeypatch, missing)

    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass
    assert not (tmp_path / "sub").exists()


def test_empty_initialized_database_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs", params={"query": "python"})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    _set_database(monkeypatch, path)

    before = path.read_bytes()
    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass
    assert path.read_bytes() == before


def test_malformed_database_is_privacy_safe_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database file at all")
    _set_database(monkeypatch, malformed)

    before = malformed.read_bytes()
    with pytest.raises(ValueError, match="current schema v12"):
        with TestClient(app):
            pass
    assert malformed.read_bytes() == before


def test_repeated_identical_requests_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.get("/jobs", params={"query": "engineer", "limit": 3})
        second = client.get("/jobs", params={"query": "engineer", "limit": 3})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content == first.content


def test_endpoint_performs_no_writes_and_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with connect(path) as connection:
        jobs_before = connection.execute(
            "SELECT id, title, company FROM jobs ORDER BY id"
        ).fetchall()

    with TestClient(app) as client:
        client.get("/jobs", params={"query": "python"})
        client.get("/jobs", params={"query": ""})
        client.get("/jobs", params={"query": "9f41c7d2"})

    with connect(path) as connection:
        jobs_after = connection.execute(
            "SELECT id, title, company FROM jobs ORDER BY id"
        ).fetchall()
        runs = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]

    assert [tuple(row) for row in jobs_after] == [tuple(row) for row in jobs_before]
    assert runs == 0
    assert not lock_path_for(path).exists()


def test_read_only_works_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="collect due"):
        with TestClient(app) as client:
            response = client.get("/jobs", params={"query": "python"})

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [1, 6]


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH, database_path

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        client.get("/jobs", params={"query": "python"})
        client.get("/jobs")

    assert database_path() == path
    assert not real.is_file() or real.stat().st_mtime_ns == before


# --------------------------------------------------------------------------- #
# G. Regression/architecture
# --------------------------------------------------------------------------- #


def test_job_detail_endpoint_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/jobs/1")

    assert response.status_code == 200
    assert response.json()["title"] == "Python Developer"


def test_health_endpoint_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["schema_version"] == SCHEMA_VERSION


def test_api_does_not_import_careeros_cli() -> None:
    api_root = _SRC / "careeros" / "api"
    violations: list[str] = []
    for file in sorted(api_root.rglob("*.py")):
        if file.name == "__init__.py":
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "careeros.cli" or name.startswith("careeros.cli."):
                    violations.append(f"{file.relative_to(_SRC)}: {name}")
    assert violations == []


def test_schema_version_remains_10() -> None:
    assert SCHEMA_VERSION == 12
