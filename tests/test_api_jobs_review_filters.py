"""Tests for the GET /jobs review-state filters milestone.

Covers the three new query parameters ``unreviewed``, ``include_decisions``,
and ``exclude_decisions``: no-filter baseline byte-identity (plain, keyword,
and application-tracking-filtered), unreviewed-only, include/exclude single
and multiple decisions, duplicates, explicit empty lists, invalid and
empty-string decisions with the canonical validate_decision 422 text, all three
pairwise conflict cases, filter-before-pagination/`total` semantics, AND
composition with every existing keyword/filter dimension (query, source,
remote, location, employment_type, untracked, include_statuses,
exclude_statuses), deterministic canonical ordering of survivors, repeated
request determinism, missing/initialized-empty/uninitialized/malformed database
behavior, read-only/no-lock/no-side-effect guarantees, operation while the
collection lock is held, a privacy leak scan, the no-``careeros.cli``-import
AST guard, ``SCHEMA_VERSION == 8``, the guarantee that prior GET /jobs
keyword/filter/pagination behavior is unchanged, and the guarantee that the
real database is untouched.
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

_INVALID_DECISION_DETAIL = (
    "Invalid review decision 'bogus'; expected one of: shortlist, dismiss, "
    "review_later."
)
_EMPTY_DECISION_DETAIL = (
    "Invalid review decision ''; expected one of: shortlist, dismiss, review_later."
)
_UNREVIEWED_CONFLICT_DETAIL = (
    "unreviewed cannot be combined with include_decisions or exclude_decisions."
)
_INCLUDE_EXCLUDE_DECISION_CONFLICT_DETAIL = (
    "include_decisions and exclude_decisions cannot be combined."
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
    """Seed four flat jobs with application tracking and review decisions.

    Tracked: a-1 = applied, r-1 = interview (j-1 and k-1 are untracked).
    Reviewed: a-1 = shortlist, r-1 = dismiss, k-1 = review_later (j-1 is
    unreviewed). Canonical GET /jobs order (``published_at DESC NULLS LAST,
    id ASC``) is j-1 (2026-08-02), a-1 (2026-08-01), r-1 (2026-07-30),
    k-1 (2026-07-01). Returns a mapping of source-job-id to ``jobs.id``.
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
    set_decision(
        connection,
        job_id=job_ids["a-1"],
        decision="shortlist",
        note="secret-review-note",
        now=_EPOCH,
    )
    set_decision(
        connection,
        job_id=job_ids["r-1"],
        decision="dismiss",
        now=_EPOCH,
    )
    set_decision(
        connection,
        job_id=job_ids["k-1"],
        decision="review_later",
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
# Baseline: no review params means unchanged GET /jobs behavior
# --------------------------------------------------------------------------- #


def test_no_review_params_matches_existing_listing(
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


def test_no_review_params_keyword_listing_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Engineer")

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["k-1"]]


def test_no_review_params_tracking_listing_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        applied = _list(client, include_statuses=["applied"])
        untracked = _list(client, untracked=True)
        not_interview = _list(client, exclude_statuses=["interview"])

    assert applied["total"] == 1
    assert _ids(applied) == [job_ids["a-1"]]
    assert untracked["total"] == 2
    assert _ids(untracked) == [job_ids["j-1"], job_ids["k-1"]]
    assert not_interview["total"] == 3
    assert _ids(not_interview) == [job_ids["j-1"], job_ids["a-1"], job_ids["k-1"]]


# --------------------------------------------------------------------------- #
# unreviewed
# --------------------------------------------------------------------------- #


def test_unreviewed_false_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, unreviewed=False)

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]


def test_unreviewed_true_keeps_only_unreviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, unreviewed=True)

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]
    assert job_ids["a-1"] not in _ids(payload)
    assert job_ids["r-1"] not in _ids(payload)
    assert job_ids["k-1"] not in _ids(payload)


# --------------------------------------------------------------------------- #
# include_decisions
# --------------------------------------------------------------------------- #


def test_include_decisions_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist"])

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_include_decisions_multiple_or_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist", "dismiss"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["r-1"]]


def test_include_decisions_excludes_unreviewed_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["dismiss"])

    returned = _ids(payload)
    assert job_ids["r-1"] in returned
    assert job_ids["j-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert payload["total"] == 1


# --------------------------------------------------------------------------- #
# exclude_decisions
# --------------------------------------------------------------------------- #


def test_exclude_decisions_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["dismiss"])

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["j-1"], job_ids["a-1"], job_ids["k-1"]]


def test_exclude_decisions_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["shortlist", "dismiss"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]


def test_exclude_decisions_leaves_unreviewed_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["shortlist", "review_later"])

    returned = _ids(payload)
    assert job_ids["j-1"] in returned
    assert job_ids["r-1"] in returned
    assert job_ids["a-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert payload["total"] == 2


# --------------------------------------------------------------------------- #
# Duplicate and empty/no-op values
# --------------------------------------------------------------------------- #


def test_duplicate_include_decisions_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist", "shortlist"])

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_duplicate_exclude_decisions_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["dismiss", "dismiss"])

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["j-1"], job_ids["a-1"], job_ids["k-1"]]


def test_explicit_empty_include_decisions_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=[])

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]


def test_explicit_empty_exclude_decisions_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=[])

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]


# --------------------------------------------------------------------------- #
# Validation: conflicts and invalid decisions
# --------------------------------------------------------------------------- #


def test_include_exclude_decisions_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(
            client,
            include_decisions=["shortlist"],
            exclude_decisions=["dismiss"],
        )

    assert response.status_code == 422
    assert response.json() == {"detail": _INCLUDE_EXCLUDE_DECISION_CONFLICT_DETAIL}


def test_unreviewed_include_decisions_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, unreviewed=True, include_decisions=["shortlist"])

    assert response.status_code == 422
    assert response.json() == {"detail": _UNREVIEWED_CONFLICT_DETAIL}


def test_unreviewed_exclude_decisions_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, unreviewed=True, exclude_decisions=["shortlist"])

    assert response.status_code == 422
    assert response.json() == {"detail": _UNREVIEWED_CONFLICT_DETAIL}


def test_invalid_review_decision_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, include_decisions=["bogus"])

    assert response.status_code == 422
    assert response.json() == {"detail": _INVALID_DECISION_DETAIL}


def test_empty_string_review_decision_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, exclude_decisions=[""])

    assert response.status_code == 422
    assert response.json() == {"detail": _EMPTY_DECISION_DETAIL}


# --------------------------------------------------------------------------- #
# Composition with existing parameters
# --------------------------------------------------------------------------- #


def test_review_filter_composes_with_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Frontend", unreviewed=True)

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_review_filter_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        included = _list(client, source="arbeitnow", include_decisions=["shortlist"])
        empty = _list(client, source="jobicy", include_decisions=["shortlist"])

    assert included["total"] == 1
    assert _ids(included) == [job_ids["a-1"]]
    assert empty["total"] == 0
    assert empty["items"] == []


def test_review_filter_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, remote=True, unreviewed=True)

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_review_filter_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unreviewed = _list(client, location="Berlin", unreviewed=True)

    assert unreviewed["total"] == 1
    assert _ids(unreviewed) == [job_ids["j-1"]]


def test_review_filter_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(
            client,
            employment_type="Contract",
            include_decisions=["dismiss"],
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["r-1"]]


def test_review_filter_composes_with_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reviewed_untracked = _list(
            client, untracked=True, include_decisions=["review_later"]
        )
        exclude_shortlist = _list(
            client, untracked=True, exclude_decisions=["shortlist"]
        )

    assert reviewed_untracked["total"] == 1
    assert _ids(reviewed_untracked) == [job_ids["k-1"]]
    assert exclude_shortlist["total"] == 2
    assert _ids(exclude_shortlist) == [job_ids["j-1"], job_ids["k-1"]]


def test_review_filter_composes_with_include_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(
            client,
            include_statuses=["applied"],
            include_decisions=["shortlist"],
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_review_filter_composes_with_exclude_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["applied"], unreviewed=True)

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_review_filter_multi_dimensional_composition(
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
            exclude_decisions=["shortlist"],
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]


# --------------------------------------------------------------------------- #
# Pagination, total, and ordering
# --------------------------------------------------------------------------- #


def test_review_filter_applied_before_limit_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _list(
            client,
            exclude_decisions=["shortlist", "review_later"],
            limit=1,
            offset=0,
        )
        second_page = _list(
            client,
            exclude_decisions=["shortlist", "review_later"],
            limit=1,
            offset=1,
        )

    assert first_page["total"] == 2
    assert _ids(first_page) == [job_ids["j-1"]]
    assert second_page["total"] == 2
    assert _ids(second_page) == [job_ids["r-1"]]


def test_review_filter_total_is_post_filter_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        exclude = _list(client, exclude_decisions=["shortlist"], limit=1, offset=0)

    assert exclude["total"] == 3
    assert _ids(exclude) == [job_ids["j-1"]]


def test_ordering_of_survivors_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(
            client,
            include_decisions=["shortlist", "dismiss", "review_later"],
        )

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["a-1"], job_ids["r-1"], job_ids["k-1"]]


def test_deterministic_repeated_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _list(client, include_decisions=["shortlist", "review_later"])
        second = _list(client, include_decisions=["shortlist", "review_later"])

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

    with pytest.raises(ValueError, match="current schema v11"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_initialized_empty_database_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        unreviewed = _list(client, unreviewed=True)
        include = _list(client, include_decisions=["shortlist"])

    assert unreviewed == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert include == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v11"):
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
    with pytest.raises(ValueError, match="current schema v11"):
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
        _list(client, unreviewed=True)
        _list(client, include_decisions=["shortlist"])
        _list(client, exclude_decisions=["review_later"])
        _list(client, query="Engineer", unreviewed=True)

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
            connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reminder_notifications"
            ).fetchone()[0]
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
        decisions = {
            row["job_id"]: row["decision"]
            for row in connection.execute(
                "SELECT job_id, decision FROM review_decisions"
            )
        }
        assert decisions == {
            job_ids["a-1"]: "shortlist",
            job_ids["r-1"]: "dismiss",
            job_ids["k-1"]: "review_later",
        }


def test_jobs_listing_succeeds_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="test-holder"):
        with TestClient(app) as client:
            response = _get(client, unreviewed=True)

    assert response.status_code == 200
    assert response.json()["total"] == 1


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_privacy_leak_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
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
            _get(client, unreviewed=True),
            _get(client, include_decisions=["shortlist"]),
            _get(client, exclude_decisions=["review_later"]),
            _get(client, query="Engineer", unreviewed=True),
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
        "dismiss",
        "review_later",
        "applied",
        "interview",
        "follow_up",
        "untracked",
        "unreviewed",
        "include_decisions",
        "exclude_decisions",
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
        detail = client.get(f"/jobs/{job_ids['a-1']}")
        applications = client.get("/applications")
        decisions = client.get("/review-decisions")

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
    assert detail.status_code == 200
    assert list(detail.json()) == _JOB_FIELDS
    assert applications.status_code == 200
    assert applications.json()["total"] == 2
    assert decisions.status_code == 200
    assert decisions.json()["total"] == 3


def test_review_filter_cross_dimension_no_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        applied_shortlist = _list(
            client,
            include_statuses=["applied"],
            include_decisions=["shortlist"],
        )
        excluding_reviewed = _list(
            client, exclude_statuses=["applied"], exclude_decisions=["shortlist"]
        )

    assert applied_shortlist["total"] == 1
    assert _ids(applied_shortlist) == [job_ids["a-1"]]
    assert excluding_reviewed["total"] == 3
    assert _ids(excluding_reviewed) == [
        job_ids["j-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]


# --------------------------------------------------------------------------- #
# Schema, import-boundary, and real-database invariants
# --------------------------------------------------------------------------- #


def test_schema_version_is_ten() -> None:
    assert SCHEMA_VERSION == 11


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
        _list(client, unreviewed=True)
        _list(client, include_decisions=["shortlist"])
        _list(client, exclude_decisions=["dismiss"])

    if real_bytes is None:
        assert not _REAL_DATABASE.exists()
    else:
        assert _REAL_DATABASE.read_bytes() == real_bytes
