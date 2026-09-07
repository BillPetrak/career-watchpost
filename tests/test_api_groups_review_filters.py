"""Tests for the GET /groups review-state filters milestone.

Covers the three new query parameters ``unreviewed``, ``include_decisions``,
and ``exclude_decisions`` on ``GET /groups``: no-parameter baseline
byte-identity (plain, keyword, and application-tracking-filtered), the
canonical group decision-resolution semantics (including conflict resolution
through the lowest ``jobs.id`` and a decision on a non-representative member
making the whole canonical group reviewed), unreviewed-only, include/exclude
single and multiple decisions, duplicates, explicit empty lists, invalid and
empty-string decisions with the canonical ``validate_decision`` 422 text, all
three pairwise conflict cases, filter-before-pagination/`total` semantics, AND
composition with every existing keyword/filter dimension (query, source,
remote, location, employment_type, untracked, include_statuses,
exclude_statuses), deterministic canonical ordering of survivors in both list
and keyword modes, representative/member ordering preservation, repeated
request byte-determinism, missing/uninitialized/malformed database behavior,
read-only/no-lock/no-side-effect guarantees, no ``.lock`` sidecar, operation
while the collection lock is held, a privacy leak scan proving decision values
influence selection but never appear as response fields, the
no-``careeros.cli``-import AST guard, ``SCHEMA_VERSION == 8``, the guarantee
that prior ``GET /groups`` keyword/filter/pagination behavior is unchanged,
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


def _seed(
    path: Path,
) -> tuple[dict[str, int], str, str]:
    """Seed two duplicate pairs plus two singletons with review decisions.

    Jobs and publish dates (all remote, Full-time, Berlin unless overridden):

    - a-1 (arbeitnow)  "Senior Engineer", Acme GmbH, Berlin,  2026-08-01 (shortlist)
    - r-1 (remotive)   "Senior Engineer", Acme, Berlin,       2026-08-01 (dismiss)
      (a-1 + r-1 group together; a-1 representative; conflict resolved to
      shortlist through the lowest id)
    - k-1 (remoteok)   "Data Scientist", Other Co, Munich,    2026-07-30
      (onsite, Contract; review_later)
    - j-1 (jobicy)     "Frontend Developer", Frontend Labs, Berlin, 2026-08-02
      (unreviewed)
    - e-1 (weworkremotely) "Endless Widgets", Acme, London,   2026-08-03
      (unreviewed representative)
    - w-2 (workingnomads)  "Endless Widgets", Acme, London,   2026-08-03
      (shortlist on the NON-representative member, making the group reviewed)

    Applications: a-1 = applied, r-1 = interview, w-2 = offer.

    Returns a mapping of source-job-id to ``jobs.id`` plus the canonical group
    IDs of the Senior Engineer pair and the Endless Widgets pair.
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
    set_decision(
        connection,
        job_id=job_ids["w-2"],
        decision="shortlist",
        now=_EPOCH,
    )
    ensure_grouped(connection)
    group_ids = {
        pair[0]: pair[1]
        for pair in connection.execute(
            "SELECT job_id, group_id FROM job_group_members"
        ).fetchall()
    }
    engineer_group = group_ids[job_ids["a-1"]]
    widgets_group = group_ids[job_ids["e-1"]]
    connection.close()
    return job_ids, engineer_group, widgets_group


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
# Baseline: no review params means unchanged GET /groups behavior
# --------------------------------------------------------------------------- #


def test_no_review_params_matches_existing_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
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
    assert _group_ids(payload) == [
        engineer_group,
        None,
        None,
        _group_id_for(path, "e-1"),
    ]
    for item in payload["items"]:
        assert list(item) == _GROUP_FIELDS
        assert list(item["representative"]) == _JOB_FIELDS
        for member in item["members"]:
            assert list(member) == _JOB_FIELDS


def test_unreviewed_false_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        omitted = _list(client)
        explicit = _list(client, unreviewed=False)

    assert omitted == explicit
    assert explicit["total"] == 4
    assert _ids(explicit) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["e-1"],
    ]


def test_no_review_params_tracking_listing_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        applied = _list(client, include_statuses=["applied"])
        untracked = _list(client, untracked=True)
        not_applied = _list(client, exclude_statuses=["applied"])

    assert applied["total"] == 1
    assert _ids(applied) == [job_ids["a-1"]]
    assert applied["items"][0]["group_id"] == engineer_group
    assert untracked["total"] == 2
    assert _ids(untracked) == [job_ids["k-1"], job_ids["j-1"]]
    assert not_applied["total"] == 3
    assert _ids(not_applied) == [job_ids["k-1"], job_ids["j-1"], job_ids["e-1"]]


# --------------------------------------------------------------------------- #
# unreviewed
# --------------------------------------------------------------------------- #


def test_unreviewed_true_keeps_only_unreviewed_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, unreviewed=True)

    # Only j-1 (unreviewed singleton) survives; the reviewed singleton k-1,
    # the reviewed multi-member engineer group, and the widgets group (reviewed
    # through its non-representative member) are all dropped.
    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]
    assert _group_ids(payload) == [None]
    assert engineer_group not in _group_ids(payload)
    assert widgets_group not in _group_ids(payload)


def test_unreviewed_true_keeps_only_groups_with_no_resolved_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, unreviewed=True)

    returned = _ids(payload)
    assert job_ids["j-1"] in returned
    assert job_ids["a-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert widgets_group not in _group_ids(payload)


def test_review_decision_on_non_representative_member_marks_group_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unreviewed = _list(client, unreviewed=True)
        shortlisted = _list(client, include_decisions=["shortlist"])

    # e-1 (representative) is unreviewed but w-2 (the other member) is
    # shortlisted; the canonical group is therefore reviewed.
    assert widgets_group not in _group_ids(unreviewed)
    assert job_ids["e-1"] in _ids(shortlisted)


# --------------------------------------------------------------------------- #
# include_decisions
# --------------------------------------------------------------------------- #


def test_include_decisions_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["e-1"]]


def test_include_decisions_multiple_or_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist", "review_later"])

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["a-1"], job_ids["k-1"], job_ids["e-1"]]


def test_include_decisions_excludes_unreviewed_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist"])

    returned = _ids(payload)
    assert job_ids["a-1"] in returned
    assert job_ids["j-1"] not in returned
    assert payload["total"] == 2


def test_include_decisions_resolves_conflict_by_lowest_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        dismissed = _list(client, include_decisions=["dismiss"])

    # r-1 is dismissed but a-1 (the lowest-id member) is shortlisted, so the
    # canonical engineer group resolves to shortlist and is NOT kept here.
    assert dismissed["total"] == 0
    assert dismissed["items"] == []
    assert job_ids["a-1"] not in _ids(dismissed)


def test_explicit_empty_include_decisions_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=[])

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["e-1"],
    ]


# --------------------------------------------------------------------------- #
# exclude_decisions
# --------------------------------------------------------------------------- #


def test_exclude_decisions_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["shortlist"])

    # Drops the engineer and widgets groups (resolved shortlist); keeps the
    # review_later singleton and the unreviewed singleton.
    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["k-1"], job_ids["j-1"]]


def test_exclude_decisions_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["shortlist", "review_later"])

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_exclude_decisions_leaves_unreviewed_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["shortlist", "review_later"])

    returned = _ids(payload)
    assert job_ids["j-1"] in returned
    assert job_ids["a-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert payload["total"] == 1


def test_exclude_decisions_absent_groups_keep_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["dismiss"])

    # No canonical group resolves to dismiss, so nothing is dropped.
    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["e-1"],
    ]


def test_explicit_empty_exclude_decisions_is_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=[])
        omitted = _list(client)

    assert payload == omitted
    assert payload["total"] == 4


# --------------------------------------------------------------------------- #
# Duplicate values
# --------------------------------------------------------------------------- #


def test_duplicate_include_decisions_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist", "shortlist"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["e-1"]]


def test_duplicate_exclude_decisions_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_decisions=["shortlist", "shortlist"])

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["k-1"], job_ids["j-1"]]


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


@pytest.mark.parametrize(
    "params", [{"include_decisions": ["bogus"]}, {"exclude_decisions": ["bogus"]}]
)
def test_invalid_review_decision_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: dict[str, object]
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, **params)

    assert response.status_code == 422
    assert response.json() == {"detail": _INVALID_DECISION_DETAIL}


@pytest.mark.parametrize(
    "params", [{"include_decisions": [""]}, {"exclude_decisions": [""]}]
)
def test_empty_string_review_decision_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, params: dict[str, object]
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _get(client, **params)

    assert response.status_code == 422
    assert response.json() == {"detail": _EMPTY_DECISION_DETAIL}


def test_conflicts_validated_before_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conflicts and invalid values are rejected against a valid database."""
    database_path = tmp_path / "careeros.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        assert _get(client, include_decisions=["bogus"]).status_code == 422
        assert (
            _get(
                client,
                include_decisions=["shortlist"],
                exclude_decisions=["dismiss"],
            ).status_code
            == 422
        )
        assert (
            _get(client, unreviewed=True, include_decisions=["shortlist"]).status_code
            == 422
        )


# --------------------------------------------------------------------------- #
# Composition with existing parameters
# --------------------------------------------------------------------------- #


def test_review_filter_composes_with_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        included = _list(client, query="Engineer", include_decisions=["shortlist"])
        unreviewed = _list(client, query="Frontend", unreviewed=True)

    assert included["total"] == 1
    assert _ids(included) == [job_ids["a-1"]]
    assert included["items"][0]["group_id"] == engineer_group
    assert unreviewed["total"] == 1
    assert _ids(unreviewed) == [job_ids["j-1"]]


def test_review_filter_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        arbeitnow = _list(client, source="arbeitnow", include_decisions=["shortlist"])
        jobicy = _list(client, source="jobicy", include_decisions=["shortlist"])
        remotive = _list(client, source="remotive", include_decisions=["dismiss"])

    # source="arbeitnow" keeps only a-1 of the engineer pair, resolving to
    # shortlist. source="remotive" keeps only r-1, resolving to dismiss.
    assert arbeitnow["total"] == 1
    assert _ids(arbeitnow) == [job_ids["a-1"]]
    assert jobicy["total"] == 0
    assert jobicy["items"] == []
    assert remotive["total"] == 1
    assert _ids(remotive) == [job_ids["r-1"]]


def test_review_filter_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, remote=True, unreviewed=True)

    # k-1 is onsite and dropped; among the surviving remote groups only the
    # unreviewed j-1 singleton remains.
    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_review_filter_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        berlin_unreviewed = _list(client, location="Berlin", unreviewed=True)
        munich = _list(client, location="Munich", include_decisions=["review_later"])

    assert berlin_unreviewed["total"] == 1
    assert _ids(berlin_unreviewed) == [job_ids["j-1"]]
    assert munich["total"] == 1
    assert _ids(munich) == [job_ids["k-1"]]


def test_review_filter_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(
            client, employment_type="Contract", include_decisions=["review_later"]
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]


def test_review_filter_composes_with_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reviewed_untracked = _list(
            client, untracked=True, include_decisions=["review_later"]
        )
        exclude_shortlist = _list(
            client, untracked=True, exclude_decisions=["shortlist"]
        )

    # Untracked groups are k-1 and j-1; k-1 resolves review_later, j-1 is
    # unreviewed.
    assert reviewed_untracked["total"] == 1
    assert _ids(reviewed_untracked) == [job_ids["k-1"]]
    # Neither untracked group resolves shortlist, so both survive.
    assert exclude_shortlist["total"] == 2
    assert _ids(exclude_shortlist) == [job_ids["k-1"], job_ids["j-1"]]


def test_review_filter_composes_with_include_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        applied_shortlist = _list(
            client,
            include_statuses=["applied"],
            include_decisions=["shortlist"],
        )
        offer_shortlist = _list(
            client,
            include_statuses=["offer"],
            include_decisions=["shortlist"],
        )

    # The engineer group resolves shortlist and is tracked applied; the widgets
    # group resolves shortlist and is tracked offer (via non-representative w-2).
    assert applied_shortlist["total"] == 1
    assert _ids(applied_shortlist) == [job_ids["a-1"]]
    assert offer_shortlist["total"] == 1
    assert _ids(offer_shortlist) == [job_ids["e-1"]]


def test_review_filter_composes_with_exclude_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, exclude_statuses=["applied"], unreviewed=True)

    # Dropping the applied engineer group leaves groups 2..4; only j-1 is
    # unreviewed among them.
    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["j-1"]]


def test_review_filter_multi_dimensional_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
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
            include_decisions=["shortlist"],
        )

    # The widgets pair collapses to the single w-2 member (workingnomads,
    # London) which is tracked offer and resolves shortlist.
    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["w-2"]]
    assert [m["id"] for m in payload["items"][0]["members"]] == [job_ids["w-2"]]


# --------------------------------------------------------------------------- #
# Pagination, total, and ordering
# --------------------------------------------------------------------------- #


def test_review_filter_applied_before_limit_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _list(
            client,
            include_decisions=["shortlist", "review_later"],
            limit=1,
            offset=0,
        )
        second_page = _list(
            client,
            include_decisions=["shortlist", "review_later"],
            limit=1,
            offset=1,
        )

    assert first_page["total"] == 3
    assert _ids(first_page) == [job_ids["a-1"]]
    assert second_page["total"] == 3
    assert _ids(second_page) == [job_ids["k-1"]]


def test_review_filter_total_is_post_filter_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        exclude = _list(client, exclude_decisions=["shortlist"], limit=1, offset=0)

    assert exclude["total"] == 2
    assert _ids(exclude) == [job_ids["k-1"]]


def test_out_of_range_offset_is_empty_page_with_correct_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist"], offset=10)

    assert payload["total"] == 2
    assert payload["items"] == []


def test_list_mode_ordering_of_survivors_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _list(client, include_decisions=["shortlist", "review_later"])

    # Ascending representative job id among survivors: a-1, k-1, e-1.
    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["a-1"], job_ids["k-1"], job_ids["e-1"]]


def test_keyword_mode_ordering_of_survivors_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        baseline = _list(client, query="Acme")
        filtered = _list(client, query="Acme", include_decisions=["shortlist"])

    # Keyword ordering (published_at DESC NULLS LAST, then representative id):
    # e-1 group (2026-08-03) then a-1 group (2026-08-01). Both resolve
    # shortlist, so survivors keep the exact same relative order.
    assert _ids(baseline) == [job_ids["e-1"], job_ids["a-1"]]
    assert _ids(filtered) == [job_ids["e-1"], job_ids["a-1"]]
    assert filtered["total"] == 2


def test_representative_and_members_preserved_after_review_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        baseline = _list(client)
        filtered = _list(client, include_decisions=["shortlist", "review_later"])

    baseline_group = _for_group(baseline, group_id=engineer_group)
    filtered_group = _for_group(filtered, group_id=engineer_group)
    assert (
        filtered_group["representative"]["id"] == baseline_group["representative"]["id"]
    )
    assert [m["id"] for m in filtered_group["members"]] == [
        m["id"] for m in baseline_group["members"]
    ]


def test_deterministic_repeated_requests_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _get(client, query="Engineer", include_decisions=["shortlist"])
        second = _get(client, query="Engineer", include_decisions=["shortlist"])

    assert first.status_code == 200
    assert first.text == second.text
    assert first.json() == second.json()


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


# --------------------------------------------------------------------------- #
# Read-only, locking, and side-effect guarantees
# --------------------------------------------------------------------------- #


def test_read_only_no_lock_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
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
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 6
        assert (
            connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 3
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
            == 4
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


def test_privacy_sentinel_leak_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
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
            _get(client, query="Engineer", include_decisions=["shortlist"]),
            _get(client, untracked=True, include_decisions=["review_later"]),
        ]

    # Decision values influence selection: the different filters return
    # different totals while still never exposing a decision field.
    totals = [response.json()["total"] for response in responses]
    assert 1 in totals
    assert 2 in totals

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
        "dismiss",
        "review_later",
        "applied",
        "interview",
        "offer",
        "follow_up",
        "unreviewed",
        "untracked",
        "include_decisions",
        "exclude_decisions",
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
        "score_components",
        str(path),
        str(_REAL_DATABASE),
        "Traceback",
        "sqlite3",
    )
    for response in responses:
        for marker in leaked:
            assert marker not in response.text


# --------------------------------------------------------------------------- #
# Regression: existing GET /groups behavior unchanged
# --------------------------------------------------------------------------- #


def test_existing_groups_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        groups = client.get("/groups")
        keywords = client.get("/groups", params={"query": "Acme"})
        filters = client.get(
            "/groups",
            params={"remote": "true", "location": "London", "source": "weworkremotely"},
        )
        paginated = client.get("/groups", params={"offset": 1, "limit": 1})
        summary = client.get("/groups/summary")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }
    assert groups.status_code == 200
    assert groups.json()["total"] == 4
    assert _ids(groups.json()) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["e-1"],
    ]
    assert keywords.status_code == 200
    keywords_payload = keywords.json()
    assert keywords_payload["total"] == 2
    assert _ids(keywords_payload) == [job_ids["e-1"], job_ids["a-1"]]
    assert filters.status_code == 200
    assert filters.json()["total"] == 1
    assert _ids(filters.json()) == [job_ids["e-1"]]
    assert paginated.status_code == 200
    assert paginated.json()["total"] == 4
    assert _ids(paginated.json()) == [job_ids["k-1"]]
    assert summary.status_code == 200
    assert summary.json() == {
        "total_jobs": 6,
        "groups": 2,
        "grouped_jobs": 4,
        "distinct_opportunities": 4,
    }


def test_existing_groups_tracking_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        untracked = _list(client, untracked=True)
        included = _list(client, include_statuses=["applied"])
        excluded = _list(client, exclude_statuses=["applied"])

    assert untracked["total"] == 2
    assert _ids(untracked) == [job_ids["k-1"], job_ids["j-1"]]
    assert included["total"] == 1
    assert _ids(included) == [job_ids["a-1"]]
    assert included["items"][0]["group_id"] == engineer_group
    assert excluded["total"] == 3
    assert _ids(excluded) == [job_ids["k-1"], job_ids["j-1"], job_ids["e-1"]]


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
