"""Tests for the GET /groups keyword + application-tracking filters milestone.

Covers the four new query parameters ``query``, ``untracked``,
``include_statuses``, and ``exclude_statuses`` on ``GET /groups``: no-parameter
baseline byte-identity, keyword search routed through ``search_grouped``
(matched before grouping, canonical ``published_at DESC NULLS LAST`` then
representative-id ordering, descriptions match but are never returned), the
three tracking filters with the exact ``GET /jobs``/``POST /ranked-jobs``
grouped semantics (whole-group removal only, representative never changes,
group status is the lowest-job-id tracked member's status), all three
pairwise conflict cases with the canonical 422 details, invalid and
empty-string statuses, filter-before-pagination/`total` semantics, composition
with every existing keyword/filter parameter, deterministic canonical ordering,
missing/uninitialized/malformed database behavior, read-only/no-lock/no-side
effect guarantees, operation while the collection lock is held, a privacy
sentinel leak scan, the no-``careeros.cli``-import AST guard,
``SCHEMA_VERSION == 8``, the guarantee that prior ``GET /groups``
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
from careeros.database.groups import ensure_grouped
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

_GROUP_FIELDS = [
    "group_id",
    "representative",
    "members",
    "sources",
    "urls",
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


def _seed(
    path: Path,
) -> tuple[dict[str, int], str]:
    """Seed two duplicate pairs plus two singletons and recompute groups.

    Jobs and publish dates (all remote, Full-time, Berlin unless overridden):

    - a-1 (arbeitnow)  "Senior Engineer", Acme GmbH, Berlin,    2026-08-01
    - r-1 (remotive)   "Senior Engineer", Acme, Berlin,         2026-08-01
      (a-1 + r-1 group together; a-1 is the representative)
    - k-1 (remoteok)   "Data Scientist", Other Co, Munich,      2026-07-30
      (onsite, Contract)
    - j-1 (jobicy)     "Frontend Developer", Frontend Labs, Berlin, 2026-08-02
    - e-1 (weworkremotely) "Endless Widgets", Acme, London,     2026-08-03
    - w-2 (workingnomads)  "Endless Widgets", Acme, London,     2026-08-03
      (e-1 + w-2 group together; e-1 is the representative)

    Returns a mapping of source-job-id to ``jobs.id`` and the canonical group
    ID of the Senior Engineer duplicate pair.
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
                "Acme GmbH",
                "Berlin",
                published_at="2026-08-01T10:00:00Z",
                description="Python backend development.",
            ),
            _make_job(
                "remotive",
                "r-1",
                "Senior Engineer",
                "Acme",
                "Berlin",
                published_at="2026-08-01T10:00:00Z",
                description="Python backend development.",
            ),
            _make_job(
                "remoteok",
                "k-1",
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
                "weworkremotely",
                "e-1",
                "Endless Widgets",
                "Acme",
                "London",
                published_at="2026-08-03T10:00:00Z",
                description="Distributed widgets.",
            ),
            _make_job(
                "workingnomads",
                "w-2",
                "Endless Widgets",
                "Acme",
                "London",
                published_at="2026-08-03T10:00:00Z",
                description="Distributed widgets.",
            ),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    ensure_grouped(connection)
    group_ids = {
        pair[0]: pair[1]
        for pair in connection.execute(
            "SELECT job_id, group_id FROM job_group_members"
        ).fetchall()
    }
    connection.close()
    return job_ids, group_ids[job_ids["a-1"]]


def _for_group(
    payload: dict[str, object],
    *,
    group_id: str,
) -> dict[str, object]:
    """Return the item whose group_id matches, or group_id is None sentinel."""
    return next(item for item in payload["items"] if item["group_id"] == group_id)  # type: ignore[return-value]


def _get(client: TestClient, **params: object) -> object:
    """GET /groups returning the raw response object."""
    return client.get("/groups", params=params)


def _list(client: TestClient, **params: object) -> dict[str, object]:
    """GET /groups asserting success and returning the JSON payload."""
    response = _get(client, **params)
    assert response.status_code == 200
    return response.json()


def _ids(payload: dict[str, object]) -> list[int]:
    """Extract representative job ids from a group-list payload."""
    return [  # type: ignore[return-value,union-attr]
        item["representative"]["id"] for item in payload["items"]
    ]


def _group_ids(payload: dict[str, object]) -> list[str | None]:
    """Extract group ids in list order from a group-list payload."""
    return [item["group_id"] for item in payload["items"]]  # type: ignore[return-value,union-attr]


def _group_id_for(path: Path, source_job_id: str) -> str:
    """Return the canonical group id of a seeded job by source_job_id."""
    with connect(path) as connection:
        row = connection.execute(
            "SELECT m.group_id FROM job_group_members m "
            "JOIN jobs j ON j.id = m.job_id "
            "WHERE j.source_job_id = ?",
            (source_job_id,),
        ).fetchone()
    assert row is not None
    return row["group_id"]


# --------------------------------------------------------------------------- #
# Baseline: no new params means unchanged GET /groups behavior
# --------------------------------------------------------------------------- #


def test_no_new_params_matches_existing_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client)

    assert list(payload) == ["items", "total", "offset", "limit"]
    assert payload["total"] == 4
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    # Ascending representative job id.
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["e-1"],
    ]
    assert _group_ids(payload) == [group_id, None, None, _group_id_for(path, "e-1")]
    for item in payload["items"]:
        assert list(item) == _GROUP_FIELDS
        assert list(item["representative"]) == _JOB_FIELDS
        for member in item["members"]:
            assert list(member) == _JOB_FIELDS


def test_no_new_params_members_sorted_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client)

    group = _for_group(payload, group_id=group_id)
    assert [m["id"] for m in group["members"]] == [job_ids["a-1"], job_ids["r-1"]]
    assert group["sources"] == ["arbeitnow", "remotive"]


def test_existing_filters_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        by_source = _list(client, source="arbeitnow")
        by_remote = _list(client, remote="true")
        by_location = _list(client, location="Berlin")
        by_type = _list(client, employment_type="Contract")

    assert by_source["total"] == 1
    assert by_source["items"][0]["group_id"] == group_id
    assert [m["id"] for m in by_source["items"][0]["members"]] == [job_ids["a-1"]]

    assert by_remote["total"] == 3
    assert job_ids["k-1"] not in _ids(by_remote)

    assert by_location["total"] == 2
    assert [m["id"] for m in by_location["items"][0]["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]

    assert by_type["total"] == 1
    assert by_type["items"][0]["representative"]["id"] == job_ids["k-1"]


# --------------------------------------------------------------------------- #
# Keyword search
# --------------------------------------------------------------------------- #


def test_query_title_match_collapses_duplicate_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Senior Engineer")

    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["group_id"] == group_id
    assert [m["id"] for m in item["members"]] == [job_ids["a-1"], job_ids["r-1"]]
    assert item["representative"]["id"] == job_ids["a-1"]


def test_query_company_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Frontend Labs")

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_query_description_match_without_leaking_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, query="Python analysis")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]
    assert "Python analysis." not in response.text  # type: ignore[union-attr]
    assert "description" not in payload["items"][0]
    assert "description" not in payload["items"][0]["representative"]


def test_query_nonmatching_returns_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, query="nonexistent-job-xyz")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_query_deterministic_ordering_published_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _list(client, query="Acme")
        second = _list(client, query="Acme")

    assert first == second
    # Members/singletons matching Acme, ordered by representative publish
    # time DESC (NULLS LAST), then representative id ASC: e-1 group
    # (2026-08-03), then a-1 group (2026-08-01, id before k-1/e-1 ... only
    # one 2026-08-01 group here).
    assert _ids(first) == [job_ids["e-1"], job_ids["a-1"]]
    assert _group_ids(first) == [_group_id_for(path, "e-1"), group_id]


def test_query_empty_defaults_to_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        omitted = _list(client)
        explicit = _list(client, query="")

    assert omitted == explicit
    assert _ids(omitted) == _ids(explicit)
    assert explicit["total"] == 4


def test_query_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Engineer", source="remotive")

    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["group_id"] == group_id
    assert [m["id"] for m in item["members"]] == [job_ids["r-1"]]


def test_query_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Engineer", remote="true")

    assert payload["total"] == 1
    assert payload["items"][0]["group_id"] == group_id
    assert [m["id"] for m in payload["items"][0]["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]


def test_query_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Python", location="Munich")

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]


def test_query_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        include = _list(client, query="Widgets", employment_type="Full-time")
        contract = _list(client, query="Python", employment_type="Contract")

    assert include["total"] == 1
    assert _ids(include) == [job_ids["e-1"]]
    assert contract["total"] == 1
    assert _ids(contract) == [job_ids["k-1"]]


def test_query_composes_with_multiple_base_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(
            client,
            query="Engineer",
            source="arbeitnow",
            remote="true",
            location="Berlin",
            employment_type="Full-time",
        )

    assert payload["total"] == 1
    assert payload["items"][0]["group_id"] == group_id
    assert [m["id"] for m in payload["items"][0]["members"]] == [job_ids["a-1"]]


# --------------------------------------------------------------------------- #
# untracked
# --------------------------------------------------------------------------- #


def test_untracked_true_keeps_only_groups_with_no_tracked_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    # Track a-1 (representative of the Engineer pair) and k-1.
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="secret-cv-note",
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["k-1"],
            status="interview",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, untracked=True)

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["e-1"]]
    assert _group_ids(payload) == [None, _group_id_for(path, "e-1")]
    assert job_ids["a-1"] not in _ids(payload)


def test_untracked_true_drops_tracked_via_non_representative_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    # Track only r-1 (non-representative member of the Engineer pair).
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["r-1"],
            status="offer",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, untracked=True)

    assert job_ids["a-1"] not in _ids(payload)
    assert group_id not in _group_ids(payload)
    assert payload["total"] == 3


def test_untracked_false_equals_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        omitted = _list(client)
        explicit = _list(client, untracked=False)

    assert omitted == explicit
    assert explicit["total"] == 4


def test_untracked_true_with_nothing_tracked_keeps_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, untracked=True)

    assert payload["total"] == 4
    assert [item["group_id"] for item in payload["items"]].count(None) == 2


# --------------------------------------------------------------------------- #
# include_statuses
# --------------------------------------------------------------------------- #


def _track_seed(
    tmp_path: Path,
) -> tuple[Path, dict[str, int], str]:
    """Seed, track a-1->applied, r-1->interview, e-1->offer; return seed."""
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    with connect(path) as connection:
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
        set_application_status(
            connection,
            job_id=job_ids["w-2"],
            status="offer",
            now=_EPOCH,
        )
    return path, job_ids, group_id


def test_include_statuses_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied"])

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_include_statuses_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied", "offer"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["e-1"]]
    assert _group_ids(payload) == [group_id, _group_id_for(path, "e-1")]


def test_include_statuses_duplicates_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied", "applied"])

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_include_statuses_excludes_untracked_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["applied"])

    returned = _ids(payload)
    assert job_ids["a-1"] in returned
    assert job_ids["j-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert payload["total"] == 1


def test_include_statuses_matches_through_non_representative_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=["offer"])

    # w-2 is the non-representative member of the e-1 group; canonical group
    # status is the lowest-id tracked member's status (w-2 here), so the
    # group survives with its representative e-1 preserved.
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["representative"]["id"] == job_ids["e-1"]
    assert [m["id"] for m in item["members"]] == [job_ids["e-1"], job_ids["w-2"]]


def test_include_statuses_empty_list_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_statuses=[])

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["e-1"],
    ]


# --------------------------------------------------------------------------- #
# exclude_statuses
# --------------------------------------------------------------------------- #


def test_exclude_statuses_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["applied"])

    assert payload["total"] == 3
    assert job_ids["a-1"] not in _ids(payload)
    assert _ids(payload) == [job_ids["k-1"], job_ids["j-1"], job_ids["e-1"]]


def test_exclude_statuses_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["applied", "offer"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["k-1"], job_ids["j-1"]]


def test_exclude_statuses_keeps_untracked_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["interview"])

    returned = _ids(payload)
    assert job_ids["j-1"] in returned
    assert job_ids["k-1"] in returned
    assert job_ids["a-1"] in returned
    assert job_ids["r-1"] not in _ids(payload)


def test_exclude_statuses_empty_list_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=[])
        omitted = _list(client)

    assert payload == omitted
    assert payload["total"] == 4
    assert job_ids["a-1"] in _ids(payload)


# --------------------------------------------------------------------------- #
# Validation: conflicts and invalid statuses
# --------------------------------------------------------------------------- #


def test_include_exclude_statuses_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
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
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, untracked=True, include_statuses=["applied"])

    assert response.status_code == 422
    assert response.json() == {"detail": _UNTRACKED_CONFLICT_DETAIL}


def test_untracked_exclude_statuses_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, untracked=True, exclude_statuses=["applied"])

    assert response.status_code == 422
    assert response.json() == {"detail": _UNTRACKED_CONFLICT_DETAIL}


def test_invalid_status_is_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, include_statuses=["bogus"])

    assert response.status_code == 422
    assert response.json() == {"detail": _INVALID_STATUS_DETAIL}


def test_empty_string_status_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, exclude_statuses=[""])

    assert response.status_code == 422
    assert response.json() == {"detail": _EMPTY_STATUS_DETAIL}


def test_invalid_pagination_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            response = _get(client, **params)
            assert response.status_code == 422, params


# --------------------------------------------------------------------------- #
# Composition with existing parameters
# --------------------------------------------------------------------------- #


def test_query_composes_with_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        untracked = _list(client, query="Engineer", untracked=True)
        included = _list(client, query="Engineer", include_statuses=["applied"])

    assert untracked["total"] == 0
    assert untracked["items"] == []
    assert included["total"] == 1
    assert _ids(included) == [job_ids["a-1"]]


def test_tracking_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        source_match = _list(client, source="arbeitnow", include_statuses=["applied"])
        source_drop = _list(client, source="remotive", include_statuses=["applied"])

    assert source_match["total"] == 1
    assert source_match["items"][0]["group_id"] == group_id
    # The tracked member r-1 (interview) is in remotive, so the group's only
    # remotive member is not applied and the group is dropped.
    assert source_drop["total"] == 0
    assert source_drop["items"] == []


def test_tracking_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, remote="true", untracked=True)

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_tracking_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        untracked = _list(client, location="Berlin", untracked=True)
        included = _list(client, location="London", include_statuses=["offer"])

    assert untracked["total"] == 1
    assert _ids(untracked) == [job_ids["j-1"]]
    assert included["total"] == 1
    assert _ids(included) == [job_ids["e-1"]]


def test_tracking_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        untracked = _list(client, employment_type="Contract", untracked=True)
        included = _list(
            client, employment_type="Contract", include_statuses=["interview"]
        )

    # k-1 is the only Contract job and is untracked.
    assert untracked["total"] == 1
    assert _ids(untracked) == [job_ids["k-1"]]
    # No Contract job is tracked with "interview", so nothing is included.
    assert included["total"] == 0
    assert included["items"] == []


def test_tracking_composes_with_several_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(
            client,
            query="Widgets",
            source="workingnomads",
            remote="true",
            location="London",
            untracked=False,
            include_statuses=["offer"],
        )

    # The e-1 group survives via its tracked non-representative member w-2;
    # because source=workingnomads keeps only w-2 of the pair, the filtered
    # group's representative becomes w-2 (the lowest-id remaining member).
    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["w-2"]]
    assert [m["id"] for m in payload["items"][0]["members"]] == [job_ids["w-2"]]


# --------------------------------------------------------------------------- #
# Pagination, total, and ordering
# --------------------------------------------------------------------------- #


def test_tracking_filter_applied_before_limit_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _list(client, untracked=True, limit=1, offset=0)
        second_page = _list(client, untracked=True, limit=1, offset=1)

    assert first_page["total"] == 2
    assert _ids(first_page) == [job_ids["k-1"]]
    assert second_page["total"] == 2
    assert _ids(second_page) == [job_ids["j-1"]]


def test_tracking_filter_total_is_post_filter_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["applied"], limit=1, offset=0)

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["k-1"]]


def test_query_filter_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, query="Acme", offset=1, limit=1)

    assert payload["total"] == 2
    assert len(payload["items"]) == 1


def test_out_of_range_offset_is_empty_page_with_correct_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, untracked=True, offset=10)

    assert payload["total"] == 2
    assert payload["items"] == []


def test_representative_and_members_preserved_after_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        baseline = _list(client)
        tracked = _list(client, include_statuses=["applied"])

    baseline_group = _for_group(baseline, group_id=group_id)
    tracked_group = _for_group(tracked, group_id=group_id)
    assert (
        tracked_group["representative"]["id"] == baseline_group["representative"]["id"]
    )
    assert [m["id"] for m in tracked_group["members"]] == [
        m["id"] for m in baseline_group["members"]
    ]


def test_deterministic_repeated_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _list(client, query="Engineer", include_statuses=["interview"])
        second = _list(client, query="Engineer", include_statuses=["interview"])

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
    path, _job_ids, _group_id = _track_seed(tmp_path)
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
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
        assert (
            connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM reminder_notifications"
            ).fetchone()[0]
            == 0
        )


def test_groups_listing_succeeds_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="test-holder"):
        with TestClient(app) as client:
            response = _get(client, untracked=True)

    assert response.status_code == 200
    assert response.json()["total"] == 2


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #


def test_privacy_sentinel_leak_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, _group_id = _track_seed(tmp_path)
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
            assert list(item) == _GROUP_FIELDS
            assert list(item["representative"]) == _JOB_FIELDS
            assert "description" not in item["representative"]
            for member in item["members"]:
                assert list(member) == _JOB_FIELDS
                assert "description" not in member

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
        str(path),
        str(_REAL_DATABASE),
        "Traceback",
        "sqlite3",
    )
    for response in responses:
        for marker in leaked:
            assert marker not in response.text


# --------------------------------------------------------------------------- #
# Regression: existing endpoints unchanged
# --------------------------------------------------------------------------- #


def test_existing_groups_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, job_ids, group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        groups = client.get("/groups")
        summary = client.get("/groups/summary")
        show = client.get(f"/groups/{group_id}")
        ranked = client.post(
            "/ranked-jobs",
            json={"profile": {"desired_titles": ["Engineer"]}},
            params={"grouped": "true"},
        )

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 6
    assert groups.status_code == 200
    assert groups.json()["total"] == 4
    assert summary.status_code == 200
    assert summary.json() == {
        "total_jobs": 6,
        "groups": 2,
        "grouped_jobs": 4,
        "distinct_opportunities": 4,
    }
    assert show.status_code == 200
    assert show.json()["representative"]["id"] == job_ids["a-1"]
    assert ranked.status_code == 200
    assert ranked.json()["total"] == 4


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
    path, _job_ids, _group_id = _track_seed(tmp_path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        _list(client, untracked=True)
        _list(client, include_statuses=["applied"])
        _list(client, exclude_statuses=["interview"])

    if real_bytes is None:
        assert not _REAL_DATABASE.exists()
    else:
        assert _REAL_DATABASE.read_bytes() == real_bytes
