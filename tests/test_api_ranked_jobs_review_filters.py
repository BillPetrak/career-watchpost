"""Tests for the POST /ranked-jobs review-state filters milestone.

Covers the three optional request-body fields ``unreviewed``,
``include_decisions``, and ``exclude_decisions``: flat/grouped no-filter
baseline byte-identity, explicit-null/empty-list/``false`` no-filter
semantics, unreviewed-only, include/exclude single and multiple decisions,
the canonical group decision-resolution semantics (a decision on a
non-representative member making the whole group reviewed, and conflicting
member decisions resolving to the lowest ``jobs.id`` through the shared
``resolve_group_decision``), duplicates, invalid and empty-string decisions
with the canonical ``validate_decision`` 422 text, all three pairwise conflict
cases, structural body-type failures remaining Pydantic 422, unknown body
fields staying inert, filter-after-``minimum_score``/filter-before-pagination
and ``total`` semantics, AND composition with every existing keyword/filter/
score dimension (query, source, remote, location, employment_type, untracked,
include_statuses, exclude_statuses, minimum_score), flat/grouped survivor
ordering preservation with no re-sort, repeated request byte-determinism,
missing (200 empty even with an otherwise-invalid review body) /
uninitialized / malformed / initialized-empty database behavior, read-only
no-lock no-side-effect guarantees, operation while the collection lock is
held, a privacy leak scan, response field-order/projection preservation, the
no-``careeros.cli``-import AST guard, ``SCHEMA_VERSION == 8``, the guarantee
that prior ranked-jobs flat/grouped/application-filter behavior is unchanged,
and the guarantee that the real database is untouched.
"""

from __future__ import annotations

import ast
import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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

_MAIN_PROFILE = {
    "desired_titles": ["Engineer"],
    "skills": ["Python"],
    "preferred_locations": ["Berlin"],
    "remote_preference": "remote",
}

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

    - a-1 (arbeitnow)  "Senior Engineer", Acme GmbH, Berlin, 2026-08-01
      (shortlist; applied)
    - r-1 (remotive)   "Senior Engineer", Acme, Berlin,      2026-08-01
      (dismiss; interview)
      (a-1 + r-1 group together; a-1 representative; conflict resolves to
      shortlist through the lowest id)
    - k-1 (remoteok)   "Data Scientist", Other Co, Munich,    2026-07-30
      (onsite, Contract; review_later; untracked)
    - j-1 (jobicy)     "Frontend Developer", Frontend Labs, Berlin, 2026-08-02
      (unreviewed; untracked)
    - e-1 (weworkremotely) "Endless Widgets", Acme, London,   2026-08-03
      (unreviewed representative; untracked)
    - w-2 (workingnomads)  "Endless Widgets", Acme, London,   2026-08-03
      (shortlist on the NON-representative member, making the group reviewed;
      offer)

    Against ``_MAIN_PROFILE`` the raw match scores are: a-1 53, r-1 53, j-1 25,
    e-1 10, w-2 10, k-1 8; the canonical grouped order is engineer group (53),
    j-1 (25), widgets group (10), k-1 (8). Returns a mapping of source-job-id to
    ``jobs.id`` plus the canonical group IDs of the Senior Engineer pair and the
    Endless Widgets pair.
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


def _post(
    client: TestClient,
    body: object,
    **params: object,
) -> object:
    """POST /ranked-jobs returning the raw response object."""
    return client.post("/ranked-jobs", json=body, params=params)


def _rank(client: TestClient, body: object, **params: object) -> dict[str, object]:
    """POST /ranked-jobs asserting success and returning the JSON payload."""
    response = _post(client, body, **params)
    assert response.status_code == 200
    return response.json()


def _flat_ids(payload: dict[str, object]) -> list[int]:
    """Extract job ids from a flat ranked payload, in output order."""
    return [item["job"]["id"] for item in payload["items"]]  # type: ignore[index,return-value,union-attr]


def _group_reps(payload: dict[str, object]) -> list[int]:
    """Extract representative job ids from a grouped ranked payload."""
    return [  # type: ignore[return-value,union-attr,index]
        item["group"]["representative"]["id"] for item in payload["items"]
    ]


def _group_ids(payload: dict[str, object]) -> list[str | None]:
    """Extract group ids from a grouped ranked payload, in output order."""
    return [item["group"]["group_id"] for item in payload["items"]]  # type: ignore[return-value,union-attr,index]


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
# Baseline and no-filter semantics
# --------------------------------------------------------------------------- #


def test_flat_baseline_byte_identical_without_review_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE})
        response = _post(client, {"profile": _MAIN_PROFILE})

    assert list(payload) == ["items", "total", "offset", "limit"]
    assert payload["total"] == 6
    assert payload["offset"] == 0
    assert payload["limit"] == 20
    assert _flat_ids(payload) == [
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["w-2"],
        job_ids["k-1"],
    ]
    assert [item["score"] for item in payload["items"]] == [53, 53, 25, 10, 10, 8]  # type: ignore[index]
    for item in payload["items"]:
        assert list(item) == ["job", "match_score", "score"]
        assert list(item["job"]) == _JOB_FIELDS
        assert "description" not in item["job"]

    # The raw response text is byte-identical to the unmodified endpoint.
    assert response.status_code == 200
    assert response.text == _post(client, {"profile": _MAIN_PROFILE}).text  # type: ignore[union-attr]


def test_grouped_baseline_byte_identical_without_review_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE}, grouped="true")

    assert list(payload) == ["items", "total", "offset", "limit"]
    assert payload["total"] == 4
    assert _group_reps(payload) == [
        job_ids["a-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["k-1"],
    ]
    assert _group_ids(payload) == [
        engineer_group,
        None,
        _group_id_for(path, "e-1"),
        None,
    ]
    for item in payload["items"]:
        assert list(item) == ["group", "match_score", "score"]
        group = item["group"]
        assert list(group) == _GROUP_FIELDS
        assert list(group["representative"]) == _JOB_FIELDS
        for member in group["members"]:
            assert list(member) == _JOB_FIELDS
            assert "description" not in member


def test_explicit_nulls_are_no_review_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    body = {
        "profile": _MAIN_PROFILE,
        "unreviewed": None,
        "include_decisions": None,
        "exclude_decisions": None,
    }
    with TestClient(app) as client:
        payload = _rank(client, body)
        baseline = _rank(client, {"profile": _MAIN_PROFILE})

    assert payload == baseline
    assert payload["total"] == 6
    assert _flat_ids(payload) == [job_ids["a-1"], job_ids["r-1"]] + [
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["w-2"],
        job_ids["k-1"],
    ]


def test_empty_decision_lists_are_no_review_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    body = {"profile": _MAIN_PROFILE, "include_decisions": [], "exclude_decisions": []}
    with TestClient(app) as client:
        payload = _rank(client, body)
        baseline = _rank(client, {"profile": _MAIN_PROFILE})

    assert payload == baseline
    assert payload["total"] == 6


def test_unreviewed_false_matches_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE, "unreviewed": False})
        baseline = _rank(client, {"profile": _MAIN_PROFILE})

    assert payload == baseline
    assert _flat_ids(baseline) == [
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["w-2"],
        job_ids["k-1"],
    ]


# --------------------------------------------------------------------------- #
# unreviewed
# --------------------------------------------------------------------------- #


def test_flat_unreviewed_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE, "unreviewed": True})

    # Only j-1 and e-1 are unreviewed; ordering (match_score DESC, pub DESC,
    # id ASC) is preserved. ALL reviewed members (including w-2) are dropped.
    assert payload["total"] == 2
    assert _flat_ids(payload) == [job_ids["j-1"], job_ids["e-1"]]


def test_grouped_unreviewed_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "unreviewed": True}, grouped="true"
        )

    # Only the unreviewed j-1 singleton survives; the widgets group is reviewed
    # through its non-representative member (w-2) and is dropped whole.
    assert payload["total"] == 1
    assert _group_reps(payload) == [job_ids["j-1"]]
    assert widgets_group not in _group_ids(payload)


def test_flat_unreviewed_excludes_every_reviewed_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE, "unreviewed": True})

    returned = _flat_ids(payload)
    assert job_ids["j-1"] in returned
    assert job_ids["e-1"] in returned
    assert job_ids["a-1"] not in returned
    assert job_ids["r-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert job_ids["w-2"] not in returned


# --------------------------------------------------------------------------- #
# Grouped decision-resolution semantics
# --------------------------------------------------------------------------- #


def test_grouped_non_representative_reviewed_member_marks_group_reviewed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unreviewed = _rank(
            client, {"profile": _MAIN_PROFILE, "unreviewed": True}, grouped="true"
        )
        shortlisted = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]},
            grouped="true",
        )

    # e-1 (representative) is unreviewed but w-2 (the other member) is
    # shortlisted; the canonical widgets group is therefore reviewed.
    assert widgets_group not in _group_ids(unreviewed)
    assert widgets_group in _group_ids(shortlisted)
    assert job_ids["e-1"] in _group_reps(shortlisted)


def test_grouped_conflicting_decisions_follow_resolve_group_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        shortlisted = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]},
            grouped="true",
        )
        dismissed = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["dismiss"]},
            grouped="true",
        )

    # r-1 is dismissed but a-1 (the lowest-id member) is shortlisted, so the
    # canonical engineer group resolves to shortlist: it is kept for shortlist
    # and NOT kept for dismiss.
    assert engineer_group in _group_ids(shortlisted)
    assert job_ids["a-1"] in _group_reps(shortlisted)
    assert dismissed["total"] == 0
    assert dismissed["items"] == []
    assert engineer_group not in _group_ids(dismissed)


def test_grouped_review_preserves_members_and_representative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        baseline = _rank(client, {"profile": _MAIN_PROFILE}, grouped="true")
        filtered = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]},
            grouped="true",
        )

    baseline_group = next(
        item["group"]
        for item in baseline["items"]
        if item["group"]["group_id"] == engineer_group  # type: ignore[index]
    )
    filtered_group = next(
        item["group"]
        for item in filtered["items"]
        if item["group"]["group_id"] == engineer_group  # type: ignore[index]
    )
    assert (
        filtered_group["representative"]["id"] == baseline_group["representative"]["id"]
    )
    assert [m["id"] for m in filtered_group["members"]] == [
        m["id"] for m in baseline_group["members"]
    ]
    for member in filtered_group["members"]:
        assert member["id"] in (job_ids["a-1"], job_ids["r-1"])


# --------------------------------------------------------------------------- #
# include_decisions
# --------------------------------------------------------------------------- #


def test_flat_include_decisions_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]}
        )

    assert payload["total"] == 2
    assert _flat_ids(payload) == [job_ids["a-1"], job_ids["w-2"]]


def test_flat_include_decisions_multiple_or_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_decisions": ["shortlist", "review_later"],
            },
        )

    assert payload["total"] == 3
    assert _flat_ids(payload) == [
        job_ids["a-1"],
        job_ids["w-2"],
        job_ids["k-1"],
    ]


def test_grouped_include_decisions_multiple_or_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_decisions": ["shortlist", "review_later"],
            },
            grouped="true",
        )

    assert payload["total"] == 3
    assert _group_reps(payload) == [
        job_ids["a-1"],
        job_ids["e-1"],
        job_ids["k-1"],
    ]


def test_include_decisions_excludes_unreviewed_while_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "include_decisions": ["dismiss"]}
        )

    # Reviewed-only: unreviewed j-1 and e-1 are excluded while the include
    # restriction is active.
    assert payload["total"] == 1
    assert _flat_ids(payload) == [job_ids["r-1"]]
    assert job_ids["j-1"] not in _flat_ids(payload)
    assert job_ids["e-1"] not in _flat_ids(payload)


# --------------------------------------------------------------------------- #
# exclude_decisions
# --------------------------------------------------------------------------- #


def test_flat_exclude_decisions_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "exclude_decisions": ["shortlist"]}
        )

    assert payload["total"] == 4
    assert _flat_ids(payload) == [
        job_ids["r-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["k-1"],
    ]


def test_flat_exclude_decisions_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "exclude_decisions": ["shortlist", "review_later"],
            },
        )

    assert payload["total"] == 3
    assert _flat_ids(payload) == [job_ids["r-1"], job_ids["j-1"], job_ids["e-1"]]


def test_grouped_exclude_decisions_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "exclude_decisions": ["shortlist", "review_later"],
            },
            grouped="true",
        )

    assert payload["total"] == 1
    assert _group_reps(payload) == [job_ids["j-1"]]


def test_exclude_decisions_leaves_unreviewed_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "exclude_decisions": ["shortlist", "review_later"],
            },
        )

    returned = _flat_ids(payload)
    assert job_ids["j-1"] in returned
    assert job_ids["e-1"] in returned
    assert job_ids["a-1"] not in returned
    assert job_ids["k-1"] not in returned
    assert payload["total"] == 3


# --------------------------------------------------------------------------- #
# Duplicate values
# --------------------------------------------------------------------------- #


def test_duplicate_include_decisions_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_decisions": ["shortlist", "shortlist"],
            },
        )

    assert payload["total"] == 2
    assert _flat_ids(payload) == [job_ids["a-1"], job_ids["w-2"]]


def test_duplicate_exclude_decisions_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "exclude_decisions": ["shortlist", "shortlist"],
            },
        )

    assert payload["total"] == 4
    assert _flat_ids(payload) == [
        job_ids["r-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["k-1"],
    ]


# --------------------------------------------------------------------------- #
# Validation: conflicts, invalid decisions, structural types, unknown fields
# --------------------------------------------------------------------------- #


def test_include_exclude_decisions_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _post(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_decisions": ["shortlist"],
                "exclude_decisions": ["dismiss"],
            },
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
        response = _post(
            client,
            {
                "profile": _MAIN_PROFILE,
                "unreviewed": True,
                "include_decisions": ["shortlist"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": _UNREVIEWED_CONFLICT_DETAIL}


def test_unreviewed_exclude_decisions_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _post(
            client,
            {
                "profile": _MAIN_PROFILE,
                "unreviewed": True,
                "exclude_decisions": ["shortlist"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": _UNREVIEWED_CONFLICT_DETAIL}


@pytest.mark.parametrize(
    "body",
    [
        {"include_decisions": ["bogus"]},
        {"exclude_decisions": ["bogus"]},
        {"include_decisions": ["shortlist", "bogus"]},
    ],
)
def test_invalid_review_decision_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _post(client, {"profile": _MAIN_PROFILE, **body})

    assert response.status_code == 422
    assert response.json() == {"detail": _INVALID_DECISION_DETAIL}


@pytest.mark.parametrize(
    "body",
    [
        {"include_decisions": [""]},
        {"exclude_decisions": [""]},
    ],
)
def test_empty_string_review_decision_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _post(client, {"profile": _MAIN_PROFILE, **body})

    assert response.status_code == 422
    assert response.json() == {"detail": _EMPTY_DECISION_DETAIL}


@pytest.mark.parametrize(
    "body",
    [
        {"unreviewed": 123},
        {"unreviewed": [1]},
        {"include_decisions": "shortlist"},
        {"include_decisions": ["shortlist", 42]},
        {"exclude_decisions": True},
        {"exclude_decisions": [None]},
    ],
)
def test_review_filter_structural_type_failures_are_pydantic_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _post(client, {"profile": _MAIN_PROFILE, **body})

    assert response.status_code == 422
    assert "detail" in response.json()
    assert isinstance(response.json()["detail"], list)


def test_unknown_body_fields_remain_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    noisier = {
        "profile": _MAIN_PROFILE,
        "unreviewed": True,
        "bogus_field": "x",
        "profile_path": "/tmp/sneaky.yaml",
        "decision": "shortlist",
        "reasons": ["nope"],
    }
    with TestClient(app) as client:
        clean = _rank(client, {"profile": _MAIN_PROFILE, "unreviewed": True})
        noisy_response = _post(client, noisier)

    assert noisy_response.status_code == 200
    assert noisy_response.json() == clean
    assert _flat_ids(clean) == [job_ids["j-1"], job_ids["e-1"]]


# --------------------------------------------------------------------------- #
# Composition with existing parameters
# --------------------------------------------------------------------------- #


def test_review_filter_composes_with_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        acme_shortlisted = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]},
            query="Acme",
        )
        acme_unreviewed = _rank(
            client,
            {"profile": _MAIN_PROFILE, "unreviewed": True},
            query="Acme",
        )

    # query="Acme" matches a-1, r-1, e-1 and w-2 by company; shortlist keeps
    # a-1 and w-2, unreviewed keeps only e-1.
    assert acme_shortlisted["total"] == 2
    assert _flat_ids(acme_shortlisted) == [job_ids["a-1"], job_ids["w-2"]]
    assert acme_unreviewed["total"] == 1
    assert _flat_ids(acme_unreviewed) == [job_ids["e-1"]]


def test_review_filter_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        arbeitnow = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]},
            source="arbeitnow",
        )
        remotive = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["dismiss"]},
            source="remotive",
        )
        jobicy = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]},
            source="jobicy",
        )

    assert arbeitnow["total"] == 1
    assert _flat_ids(arbeitnow) == [job_ids["a-1"]]
    assert remotive["total"] == 1
    assert _flat_ids(remotive) == [job_ids["r-1"]]
    assert jobicy["total"] == 0
    assert jobicy["items"] == []


def test_review_filter_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "unreviewed": True}, remote="true"
        )

    # k-1 is onsite and dropped; among the surviving remote jobs only j-1 and
    # e-1 are unreviewed.
    assert payload["total"] == 2
    assert _flat_ids(payload) == [job_ids["j-1"], job_ids["e-1"]]


def test_review_filter_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        berlin = _rank(
            client, {"profile": _MAIN_PROFILE, "unreviewed": True}, location="Berlin"
        )
        munich = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["review_later"]},
            location="Munich",
        )

    assert berlin["total"] == 1
    assert _flat_ids(berlin) == [job_ids["j-1"]]
    assert munich["total"] == 1
    assert _flat_ids(munich) == [job_ids["k-1"]]


def test_review_filter_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["review_later"]},
            employment_type="Contract",
        )

    assert payload["total"] == 1
    assert _flat_ids(payload) == [job_ids["k-1"]]


def test_review_filter_composes_with_application_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        reviewed = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "untracked": True,
                "include_decisions": ["review_later"],
            },
        )
        excluded = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "untracked": True,
                "exclude_decisions": ["shortlist"],
            },
        )

    # Untracked jobs are k-1, j-1 and e-1 (a-1 applied, r-1 interview, w-2
    # offer); k-1 resolves review_later and j-1/e-1 are unreviewed.
    assert reviewed["total"] == 1
    assert _flat_ids(reviewed) == [job_ids["k-1"]]
    # No untracked job resolves shortlist, so all three survive the exclude.
    assert excluded["total"] == 3
    assert _flat_ids(excluded) == [job_ids["j-1"], job_ids["e-1"], job_ids["k-1"]]


def test_review_filter_composes_with_include_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        applied = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_statuses": ["applied"],
                "include_decisions": ["shortlist"],
            },
        )
        offer = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_statuses": ["offer"],
                "include_decisions": ["shortlist"],
            },
        )

    assert applied["total"] == 1
    assert _flat_ids(applied) == [job_ids["a-1"]]
    assert offer["total"] == 1
    assert _flat_ids(offer) == [job_ids["w-2"]]


def test_review_filter_composes_with_exclude_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "exclude_statuses": ["applied"],
                "unreviewed": True,
            },
        )

    # Dropping the applied a-1 leaves r-1, k-1, j-1, e-1, w-2; among those
    # only j-1 and e-1 are unreviewed.
    assert payload["total"] == 2
    assert _flat_ids(payload) == [job_ids["j-1"], job_ids["e-1"]]


def test_review_filter_composes_with_minimum_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        all_min = _rank(
            client, {"profile": _MAIN_PROFILE, "unreviewed": True}, minimum_score=10
        )
        high_min = _rank(
            client, {"profile": _MAIN_PROFILE, "unreviewed": True}, minimum_score=30
        )

    # minimum_score=10 drops k-1 (8); the unreviewed survivors j-1 and e-1 pass.
    assert all_min["total"] == 2
    assert _flat_ids(all_min) == [job_ids["j-1"], job_ids["e-1"]]
    # minimum_score=30 drops everything unreviewed (j-1 25, e-1 10).
    assert high_min["total"] == 0
    assert high_min["items"] == []


def test_review_filter_multi_dimensional_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_statuses": ["offer"],
                "include_decisions": ["shortlist"],
            },
            query="Widgets",
            source="workingnomads",
            remote="true",
            location="London",
        )

    assert payload["total"] == 1
    assert _flat_ids(payload) == [job_ids["w-2"]]


# --------------------------------------------------------------------------- #
# Pagination, total, and ordering
# --------------------------------------------------------------------------- #


def test_flat_pagination_after_review_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    body = {"profile": _MAIN_PROFILE, "exclude_decisions": ["shortlist"]}
    with TestClient(app) as client:
        first = _rank(client, body, limit=1, offset=0)
        second = _rank(client, body, limit=1, offset=1)

    # Survivors r-1, j-1, e-1, k-1 (total 4); pages slice after filtering.
    assert first["total"] == 4
    assert _flat_ids(first) == [job_ids["r-1"]]
    assert second["total"] == 4
    assert _flat_ids(second) == [job_ids["j-1"]]


def test_grouped_pagination_after_review_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    body = {
        "profile": _MAIN_PROFILE,
        "include_decisions": ["shortlist", "review_later"],
    }
    with TestClient(app) as client:
        first = _rank(client, body, grouped="true", limit=1, offset=0)
        second = _rank(client, body, grouped="true", limit=1, offset=1)

    # Survivors engineer group, widgets group, k-1 (total 3 by match score).
    assert first["total"] == 3
    assert _group_reps(first) == [job_ids["a-1"]]
    assert second["total"] == 3
    assert _group_reps(second) == [job_ids["e-1"]]


def test_total_is_post_minimum_score_and_post_review_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        min_only = _rank(client, {"profile": _MAIN_PROFILE}, minimum_score=25, limit=1)
        min_and_review = _rank(
            client,
            {"profile": _MAIN_PROFILE, "exclude_decisions": ["shortlist"]},
            minimum_score=25,
        )

    # After minimum_score=25 the survivors are a-1, r-1, j-1 (3), but the
    # review filter then drops shortlisted a-1, leaving r-1 and j-1 (2).
    assert min_only["total"] == 3
    assert min_and_review["total"] == 2
    assert _flat_ids(min_and_review) == [job_ids["r-1"], job_ids["j-1"]]


def test_out_of_range_offset_empty_page_with_correct_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]},
            offset=10,
        )

    assert payload["total"] == 2
    assert payload["items"] == []


def test_flat_survivor_ordering_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        baseline = _rank(client, {"profile": _MAIN_PROFILE})
        filtered = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_decisions": ["shortlist", "review_later"],
            },
        )

    assert _flat_ids(baseline) == [
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["w-2"],
        job_ids["k-1"],
    ]
    # Survivors a-1, w-2, k-1 keep the exact same relative order.
    assert _flat_ids(filtered) == [job_ids["a-1"], job_ids["w-2"], job_ids["k-1"]]


def test_grouped_survivor_ordering_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        baseline = _rank(client, {"profile": _MAIN_PROFILE}, grouped="true")
        filtered = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "include_decisions": ["shortlist", "review_later"],
            },
            grouped="true",
        )

    assert _group_reps(baseline) == [
        job_ids["a-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["k-1"],
    ]
    # Survivors engineer, widgets, k-1 keep the exact same relative order.
    assert _group_reps(filtered) == [job_ids["a-1"], job_ids["e-1"], job_ids["k-1"]]


def test_deterministic_repeated_requests_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    body = {
        "profile": _MAIN_PROFILE,
        "exclude_decisions": ["shortlist", "review_later"],
    }
    with TestClient(app) as client:
        first = _post(client, body, query="Acme")
        second = _post(client, body, query="Acme")

    assert first.status_code == 200
    assert first.text == second.text
    assert first.json() == second.json()


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_absent_database_returns_200_empty_even_with_invalid_review_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    bodies = [
        {"profile": _MAIN_PROFILE, "unreviewed": True, "include_decisions": ["bogus"]},
        {
            "profile": _MAIN_PROFILE,
            "include_decisions": ["shortlist"],
            "exclude_decisions": ["dismiss"],
        },
        {"profile": _MAIN_PROFILE, "unreviewed": True, "exclude_decisions": [""]},
        {"profile": _MAIN_PROFILE, "unreviewed": True},
    ]
    with TestClient(app) as client:
        for body in bodies:
            response = _post(client, body)
            assert response.status_code == 200, body
            assert response.json() == {
                "items": [],
                "total": 0,
                "offset": 0,
                "limit": 20,
            }

    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = _post(client, {"profile": _MAIN_PROFILE, "unreviewed": True})

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


def test_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = _post(
            client, {"profile": _MAIN_PROFILE, "exclude_decisions": ["dismiss"]}
        )

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


def test_initialized_empty_database_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        unreviewed = _rank(client, {"profile": _MAIN_PROFILE, "unreviewed": True})
        include = _rank(
            client, {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]}
        )

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
        _rank(client, {"profile": _MAIN_PROFILE})
        _rank(client, {"profile": _MAIN_PROFILE, "unreviewed": True})
        _rank(client, {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]})
        _rank(
            client,
            {"profile": _MAIN_PROFILE, "exclude_decisions": ["review_later"]},
            grouped="true",
        )

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
    # The review state established during seeding is unchanged.
    with connect(path) as connection:
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
            job_ids["w-2"]: "shortlist",
        }


def test_ranked_jobs_succeeds_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="test-holder"):
        with TestClient(app) as client:
            response = _post(client, {"profile": _MAIN_PROFILE, "unreviewed": True})

    assert response.status_code == 200
    assert response.json()["total"] == 2


# --------------------------------------------------------------------------- #
# Determinism and privacy
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

    distinctive_profile = {
        "desired_titles": ["NebulousRole"],
        "skills": ["Frobnicate"],
        "preferred_locations": ["Nowhereland"],
        "excluded_terms": ["mysticterm"],
    }
    with TestClient(app) as client:
        responses = [
            _post(client, {"profile": distinctive_profile, "unreviewed": True}),
            _post(
                client,
                {"profile": distinctive_profile, "include_decisions": ["shortlist"]},
            ),
            _post(
                client,
                {"profile": distinctive_profile, "exclude_decisions": ["review_later"]},
            ),
            _post(
                client,
                {
                    "profile": distinctive_profile,
                    "include_decisions": ["shortlist"],
                },
                query="Acme",
                grouped="true",
            ),
        ]

    # Decision values influence selection: different filters on the same data
    # return different totals while never exposing a decision value.
    # unreviewed keeps j-1/e-1 (2); include shortlist keeps a-1/w-2 (2);
    # exclude review_later drops k-1 (5); grouped include shortlist under
    # query="Acme" keeps the engineer and widgets canonical groups (2).
    totals = [response.json()["total"] for response in responses]
    assert totals == [2, 2, 5, 2]
    assert len(set(totals)) == 2

    for response in responses:
        assert response.status_code == 200
        assert list(response.json()) == ["items", "total", "offset", "limit"]
        for item in response.json()["items"]:
            if "job" in item:
                assert list(item) == ["job", "match_score", "score"]
                assert list(item["job"]) == _JOB_FIELDS
                assert "description" not in item["job"]
            else:
                assert list(item) == ["group", "match_score", "score"]
                assert list(item["group"]) == _GROUP_FIELDS
                assert list(item["group"]["representative"]) == _JOB_FIELDS
                assert "description" not in item["group"]["representative"]
                for member in item["group"]["members"]:
                    assert "description" not in member

    leaked = (
        "NebulousRole",
        "Frobnicate",
        "Nowhereland",
        "mysticterm",
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
        "review_decision",
        "message",
        "current_status",
        "reasons",
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
# Regression: prior ranked-jobs behavior unchanged
# --------------------------------------------------------------------------- #


def test_existing_flat_and_grouped_and_application_filter_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _engineer_group, _widgets_group = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        flat_plain = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})
        flat_review = client.post(
            "/ranked-jobs",
            json={
                "profile": _MAIN_PROFILE,
                "unreviewed": None,
                "include_decisions": [],
                "exclude_decisions": None,
            },
        )
        grouped_plain = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE},
            params={"grouped": "true"},
        )
        grouped_review = client.post(
            "/ranked-jobs",
            json={
                "profile": _MAIN_PROFILE,
                "unreviewed": False,
                "include_decisions": None,
                "exclude_decisions": [],
            },
            params={"grouped": "true"},
        )
        untracked = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE, "untracked": True},
        )
        included = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE, "include_statuses": ["applied"]},
        )
        excluded = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE, "exclude_statuses": ["applied"]},
        )

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }
    # No-filter (explicit nulls/empties) is byte-identical to prior behavior.
    assert flat_plain.text == flat_review.text
    assert grouped_plain.text == grouped_review.text
    assert flat_plain.json()["total"] == 6
    assert grouped_plain.json()["total"] == 4
    assert [
        item["group"]["representative"]["id"] for item in grouped_plain.json()["items"]
    ] == [
        job_ids["a-1"],
        job_ids["j-1"],
        job_ids["e-1"],
        job_ids["k-1"],
    ]
    # Application-tracking filters behave exactly as before.
    assert untracked.status_code == 200
    assert untracked.json()["total"] == 3
    assert included.status_code == 200
    assert included.json()["total"] == 1
    assert excluded.status_code == 200
    assert excluded.json()["total"] == 5
    for item in flat_plain.json()["items"]:
        assert list(item) == ["job", "match_score", "score"]


# --------------------------------------------------------------------------- #
# Schema, import-boundary, and real-database invariants
# --------------------------------------------------------------------------- #


def test_schema_version_is_eight() -> None:
    assert SCHEMA_VERSION == 8


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
        _rank(client, {"profile": _MAIN_PROFILE, "unreviewed": True})
        _rank(client, {"profile": _MAIN_PROFILE, "include_decisions": ["shortlist"]})
        _rank(
            client,
            {"profile": _MAIN_PROFILE, "exclude_decisions": ["dismiss"]},
            grouped="true",
        )

    if real_bytes is None:
        assert not _REAL_DATABASE.exists()
    else:
        assert _REAL_DATABASE.read_bytes() == real_bytes
