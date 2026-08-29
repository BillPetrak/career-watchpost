"""Tests for the GET /jobs application-tracking filters milestone.

Covers the three new query parameters ``untracked``, ``include_statuses``, and
``exclude_statuses``: no-filter baseline byte-identity, untracked-only,
include/exclude single and multiple statuses, duplicate statuses, all three
pairwise conflict cases with the canonical 422 details, invalid and
empty-string statuses, filter-before-pagination/`total` semantics, composition
with every existing keyword/filter parameter, deterministic canonical ordering,
missing/uninitialized/malformed database behavior, read-only/no-lock/no-side-
effect guarantees, operation while the collection lock is held, a privacy leak
scan, the no-``careeros.cli``-import AST guard, ``SCHEMA_VERSION == 8``, the
guarantee that prior GET /jobs keyword/filter/pagination behavior is unchanged,
and the guarantee that the real database is untouched.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import collection_lock
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision
from tests.support import TestClient

_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_REAL_DATABASE = _REPO_ROOT / "data" / "database" / "careeros.sqlite3"

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

_INVALID_STATUS_DETAIL = (
    "Invalid application status 'bogus'; expected one of: interested, applied, "
    "interview, offer, rejected, withdrawn."
)
_EMPTY_STATUS_DETAIL = (
    "Invalid application status ''; expected one of: interested, applied, "
    "interview, offer, rejected, withdrawn."
)
_UNTRACKED_CONFLICT_DETAIL = (
    "untracked cannot be combined with include_statuses or exclude_statuses."
)
_INCLUDE_EXCLUDE_CONFLICT_DETAIL = (
    "include_statuses and exclude_statuses cannot be combined."
)


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
    published_at: str,
    description: str = "Build things.",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=f"https://example.com/{source_job_id}",
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed(path: Path) -> dict[str, int]:
    """Seed four flat jobs; track a-1 (applied) and r-1 (interview).

    a-1 and r-1 are tracked (applied / interview); j-1 and k-1 are untracked.
    Canonical GET /jobs order (``published_at DESC NULLS LAST, id ASC``) is
    j-1 (2026-08-02), a-1 (2026-08-01), r-1 (2026-07-30), k-1 (2026-07-01).
    Returns a mapping of source-job-id to ``jobs.id``.
    """
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job(
                "arbeitnow",
                "a-1",
                "Senior Engineer",
                "Acme",
                "Berlin",
                published_at="2026-08-01T10:00:00Z",
                description="Python backend development.",
            ),
            _make_job(
                "remotive",
                "r-1",
                "Data Scientist",
                "Other Co",
                "Munich",
                remote_status="onsite",
                employment_type="Contract",
                published_at="2026-07-30T10:00:00Z",
                description="Python analysis.",
            ),
            _make_job(
                "jobicy",
                "j-1",
                "Frontend Developer",
                "Frontend Labs",
                "Berlin",
                published_at="2026-08-02T10:00:00Z",
                description="Build user interfaces.",
            ),
            _make_job(
                "remoteok",
                "k-1",
                "Backend Engineer",
                "Acme",
                "Berlin",
                published_at="2026-07-01T10:00:00Z",
                description="Python and SQL services.",
            ),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    set_application_status(
        connection,
        job_id=job_ids["a-1"],
        status="applied",
        note="secret-cv-note",
        now=_EPOCH,
    )
    set_application_status(
        connection,
        job_id=job_ids["r-1"],
        status="interview",
        note="secret-interview-note",
        now=_EPOCH,
    )
    connection.close()
    return job_ids


def _get(client: TestClient, **params: object) -> object:
    """GET /jobs returning the raw response object."""
    return client.get("/jobs", params=params)


def _list(client: TestClient, **params: object) -> dict[str, object]:
    """GET /jobs asserting success and returning the JSON payload."""
    response = _get(client, **params)
    assert response.status_code == 200
    return response.json()


def _ids(payload: dict[str, object]) -> list[int]:
    """Extract the flat job ids from a job-list payload."""
    return [item["id"] for item in payload["items"]]  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Baseline: no tracking params means unchanged GET /jobs behavior
# --------------------------------------------------------------------------- #


def test_no_tracking_params_matches_existing_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client)

    assert list(payload) == ["items", "total", "offset", "limit"]
    assert payload["total"] == 4
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert _ids(payload) == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]
    for item in payload["items"]:  # type: ignore[union-attr]
        assert list(item) == _JOB_FIELDS


def test_no_tracking_params_keyword_listing_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Engineer")

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["k-1"]]


# --------------------------------------------------------------------------- #
# untracked
# --------------------------------------------------------------------------- #


def test_untracked_true_keeps_only_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="", untracked=True)

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]
    assert job_ids["a-1"] not in _ids(payload)
    assert job_ids["r-1"] not in _ids(payload)


def test_untracked_false_equals_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, untracked=False)

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]


# --------------------------------------------------------------------------- #
# include_statuses
# --------------------------------------------------------------------------- #


def test_include_statuses_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied"])

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_include_statuses_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied", "interview"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["r-1"]]


def test_include_statuses_excludes_untracked_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied"])

    returned = _ids(payload)
    assert job_ids["a-1"] in returned
    assert job_ids["j-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert payload["total"] == 1


# --------------------------------------------------------------------------- #
# exclude_statuses
# --------------------------------------------------------------------------- #


def test_exclude_statuses_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["applied"])

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["j-1"], job_ids["r-1"], job_ids["k-1"]]


def test_exclude_statuses_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["applied", "interview"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]


def test_exclude_statuses_leaves_untracked_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["interview"])

    returned = _ids(payload)
    assert job_ids["j-1"] in returned
    assert job_ids["k-1"] in returned
    assert job_ids["r-1"] not in returned
    assert job_ids["a-1"] in returned


# --------------------------------------------------------------------------- #
# Duplicate and empty/no-op values
# --------------------------------------------------------------------------- #


def test_duplicate_statuses_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied", "applied"])

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_omitted_statuses_are_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client)

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]


# --------------------------------------------------------------------------- #
# Validation: conflicts and invalid statuses
# --------------------------------------------------------------------------- #


def test_include_exclude_statuses_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(
            client,
            include_statuses=["applied"],
            exclude_statuses=["interview"],
        )

    assert response.status_code == 422
    assert response.json() == {"detail": _INCLUDE_EXCLUDE_CONFLICT_DETAIL}


def test_untracked_include_statuses_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, untracked=True, include_statuses=["applied"])

    assert response.status_code == 422
    assert response.json() == {"detail": _UNTRACKED_CONFLICT_DETAIL}


def test_untracked_exclude_statuses_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, untracked=True, exclude_statuses=["applied"])

    assert response.status_code == 422
    assert response.json() == {"detail": _UNTRACKED_CONFLICT_DETAIL}


def test_invalid_status_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, include_statuses=["bogus"])

    assert response.status_code == 422
    assert response.json() == {"detail": _INVALID_STATUS_DETAIL}


def test_empty_string_status_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, exclude_statuses=[""])

    assert response.status_code == 422
    assert response.json() == {"detail": _EMPTY_STATUS_DETAIL}


# --------------------------------------------------------------------------- #
# Composition with existing parameters
# --------------------------------------------------------------------------- #


def test_tracking_filter_composes_with_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Engineer", untracked=True)

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]


def test_tracking_filter_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        included = _list(client, source="arbeitnow", include_statuses=["applied"])
        untracked = _list(client, source="arbeitnow", untracked=True)

    assert included["total"] == 1
    assert _ids(included) == [job_ids["a-1"]]
    assert untracked["total"] == 0
    assert untracked["items"] == []


def test_tracking_filter_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, remote=True, untracked=True)

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]


def test_tracking_filter_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, location="Berlin", untracked=True)

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]


def test_tracking_filter_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        included = _list(
            client, employment_type="Contract", include_statuses=["interview"]
        )
        untracked = _list(client, employment_type="Contract", untracked=True)

    assert included["total"] == 1
    assert _ids(included) == [job_ids["r-1"]]
    assert untracked["total"] == 0
    assert untracked["items"] == []


def test_tracking_filter_composes_with_several_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(
            client,
            query="Engineer",
            remote=True,
            location="Berlin",
            untracked=True,
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]


# --------------------------------------------------------------------------- #
# Pagination, total, and ordering
# --------------------------------------------------------------------------- #


def test_tracking_filter_applied_before_limit_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _list(client, untracked=True, limit=1, offset=0)
        second_page = _list(client, untracked=True, limit=1, offset=1)

    # total is the full post-filter count before slicing; ordering canonical.
    assert first_page["total"] == 2
    assert _ids(first_page) == [job_ids["j-1"]]
    assert second_page["total"] == 2
    assert _ids(second_page) == [job_ids["k-1"]]


def test_tracking_filter_total_is_post_filter_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        exclude = _list(client, exclude_statuses=["applied"], limit=1, offset=0)

    assert exclude["total"] == 3
    assert _ids(exclude) == [job_ids["j-1"]]


def test_deterministic_repeated_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _list(client, include_statuses=["applied", "interview"])
        second = _list(client, include_statuses=["applied", "interview"])

    assert first == second
    assert _ids(first) == _ids(second)


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_absent_database_is_empty_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v9"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v9"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v9"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Read-only, locking, and side-effect guarantees
# --------------------------------------------------------------------------- #


def test_read_only_no_lock_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    before = hashlib.sha256(path.read_bytes()).digest()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        _list(client)
        _list(client, untracked=True)
        _list(client, include_statuses=["applied"])
        _list(client, exclude_statuses=["interview"])

    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["careeros.sqlite3"]

    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4
        assert (
            connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reminder_notifications"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM saved_searches").fetchone()[0] == 0
        )
    with connect(path) as connection:
        statuses = {
            row["job_id"]: row["current_status"]
            for row in connection.execute(
                "SELECT job_id, current_status FROM applications"
            )
        }
        assert statuses == {job_ids["a-1"]: "applied", job_ids["r-1"]: "interview"}


def test_jobs_listing_succeeds_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="test-holder"):
        with TestClient(app) as client:
            response = _get(client, untracked=True)

    assert response.status_code == 200
    assert response.json()["total"] == 2


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_privacy_leak_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
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
            due_at="2026-08-10T14:00:00+00:00",
            message="secret-reminder-message",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        responses = [
            _get(client, untracked=True),
            _get(client, include_statuses=["applied"]),
            _get(client, exclude_statuses=["rejected"]),
            _get(client, query="Engineer", untracked=True),
        ]

    for response in responses:
        assert response.status_code == 200
        assert list(response.json()) == ["items", "total", "offset", "limit"]
        for item in response.json()["items"]:
            assert list(item) == _JOB_FIELDS
            assert "description" not in item

    leaked = (
        "secret-cv-note",
        "secret-interview-note",
        "secret-review-note",
        "secret-reminder-message",
        "shortlist",
        "applied",
        "interview",
        "follow_up",
        "untracked",
        "include_statuses",
        "exclude_statuses",
        "Python backend development.",
        "Python analysis.",
        "description",
        "note",
        "decision",
        "message",
        "current_status",
        "reasons",
        "score",
        "group_id",
        str(path),
        str(_REAL_DATABASE),
        "Traceback",
        "sqlite3",
    )
    for response in responses:
        for marker in leaked:
            assert marker not in response.text


# --------------------------------------------------------------------------- #
# Regression: existing GET /jobs behavior unchanged
# --------------------------------------------------------------------------- #


def test_existing_jobs_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        query_jobs = client.get("/jobs", params={"query": "Engineer"})
        filtered = client.get(
            "/jobs",
            params={"remote": "true", "location": "Berlin", "source": "arbeitnow"},
        )
        paginated = client.get("/jobs", params={"offset": 1, "limit": 1})
        applications = client.get("/applications")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 4
    assert _ids(jobs.json()) == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]
    assert query_jobs.status_code == 200
    assert query_jobs.json()["total"] == 2
    assert _ids(query_jobs.json()) == [job_ids["a-1"], job_ids["k-1"]]
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert _ids(filtered.json()) == [job_ids["a-1"]]
    assert paginated.status_code == 200
    assert paginated.json()["total"] == 4
    assert _ids(paginated.json()) == [job_ids["a-1"]]
    assert applications.status_code == 200
    assert applications.json()["total"] == 2


# --------------------------------------------------------------------------- #
# Schema, import-boundary, and real-database invariants
# --------------------------------------------------------------------------- #


def test_schema_version_is_nine() -> None:
    assert SCHEMA_VERSION == 9


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


def test_real_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_bytes = _REAL_DATABASE.read_bytes() if _REAL_DATABASE.is_file() else None
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        _list(client, untracked=True)
        _list(client, include_statuses=["applied"])
        _list(client, exclude_statuses=["interview"])

    if real_bytes is None:
        assert not _REAL_DATABASE.exists()
    else:
        assert _REAL_DATABASE.read_bytes() == real_bytes
