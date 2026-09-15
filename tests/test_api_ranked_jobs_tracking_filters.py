"""Tests for the POST /ranked-jobs application-tracking filters milestone.

Covers the three optional request-body fields ``untracked``,
``include_statuses``, and ``exclude_statuses``: null/omitted/empty-list
semantics, single/multiple/duplicate statuses, invalid statuses, all three
pairwise conflict cases with the canonical 422 details, filter-before-score and
filter-before-limit/pagination behavior, grouped-ranking behavior (including
tracking resolved through a non-representative member), interactions with every
existing query/filter/score/pagination parameter, missing/uninitialized/malformed
database behavior, read-only/no-lock/no-side-effect guarantees, operation while
the collection lock is held, a privacy leak scan, deterministic repeated
requests, the guarantee that prior ranked-jobs/grouped-ranked-jobs/application
behavior is unchanged, the no-``careeros.cli``-import AST guard,
``SCHEMA_VERSION == 8``, and the guarantee that the real database is untouched.
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

_MAIN_PROFILE = {
    "desired_titles": ["Engineer"],
    "skills": ["Python"],
    "preferred_locations": ["Berlin"],
    "remote_preference": "remote",
}

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
    published_at: str = "2026-08-01T10:00:00Z",
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

    Against ``_MAIN_PROFILE`` the raw scores are: Senior Engineer (a-1) 53,
    Backend Engineer (k-1) 53, Frontend Developer (j-1) 25, Data Scientist
    (r-1) 8. a-1 is applied and r-1 is interview; k-1 and j-1 are untracked.
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


def _seed_grouped(path: Path) -> tuple[dict[str, int], str]:
    """Seed a duplicate Backend Engineer pair plus two singletons.

    The pair (a-1, r-1) groups under Acme/Backend Engineer/Berlin; r-1 is the
    tracked member (applied), so tracking is resolved through a member that is
    NOT the group's representative (a-1). Returns the job-id mapping and the
    canonical group ID of the duplicate pair.
    """
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job(
                "arbeitnow",
                "a-1",
                "Backend Engineer",
                "Acme",
                "Berlin",
                published_at="2026-08-01T10:00:00Z",
                description="Python services.",
            ),
            _make_job(
                "remotive",
                "r-1",
                "Backend Engineer",
                "Acme",
                "Berlin",
                published_at="2026-08-01T10:00:00Z",
                description="Python services.",
            ),
            _make_job(
                "jobicy",
                "j-1",
                "Senior Engineer",
                "Acme",
                "Berlin",
                published_at="2026-08-02T10:00:00Z",
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
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    set_application_status(
        connection,
        job_id=job_ids["r-1"],
        status="applied",
        note="secret-cv-note",
        now=_EPOCH,
    )
    ensure_grouped(connection)
    group_id = connection.execute(
        "SELECT group_id FROM job_group_members WHERE job_id = ?",
        (job_ids["a-1"],),
    ).fetchone()[0]
    connection.close()
    return job_ids, group_id


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


def _ids(payload: dict[str, object]) -> list[int]:
    """Extract the representative job ids from a flat or grouped payload."""
    ids: list[int] = []
    for item in payload["items"]:  # type: ignore[union-attr]
        if "job" in item:  # type: ignore[operator]
            ids.append(item["job"]["id"])  # type: ignore[index]
        else:
            ids.append(item["group"]["representative"]["id"])  # type: ignore[index]
    return ids


# --------------------------------------------------------------------------- #
# Baseline, null/omitted/empty-list semantics
# --------------------------------------------------------------------------- #


def test_baseline_without_filters_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE})

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["r-1"],
    ]
    assert [item["score"] for item in payload["items"]] == [53, 53, 25, 8]  # type: ignore[index]


def test_untracked_true_keeps_only_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE, "untracked": True})

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["k-1"], job_ids["j-1"]]


def test_untracked_false_equals_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE, "untracked": False})

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["r-1"],
    ]


def test_explicit_nulls_are_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    body = {
        "profile": _MAIN_PROFILE,
        "untracked": None,
        "include_statuses": None,
        "exclude_statuses": None,
    }
    with TestClient(app) as client:
        payload = _rank(client, body)

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["r-1"],
    ]


def test_empty_lists_are_no_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    body = {
        "profile": _MAIN_PROFILE,
        "untracked": False,
        "include_statuses": [],
        "exclude_statuses": [],
    }
    with TestClient(app) as client:
        payload = _rank(client, body)

    assert payload["total"] == 4
    assert _ids(payload) == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["r-1"],
    ]


# --------------------------------------------------------------------------- #
# Status-list filtering
# --------------------------------------------------------------------------- #


def test_include_statuses_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "include_statuses": ["applied"]}
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_include_statuses_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_statuses": ["applied", "interview"]},
        )

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["a-1"], job_ids["r-1"]]
    assert [item["score"] for item in payload["items"]] == [53, 8]  # type: ignore[index]


def test_exclude_statuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "exclude_statuses": ["applied"]}
        )

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["k-1"], job_ids["j-1"], job_ids["r-1"]]


def test_duplicate_statuses_are_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_statuses": ["applied", "applied"]},
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


def test_invalid_statuses_are_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    bodies = [
        {"include_statuses": ["bogus"]},
        {"exclude_statuses": ["bogus"]},
        {"include_statuses": ["applied", "bogus"]},
        {"include_statuses": [""]},
        {"exclude_statuses": [""]},
    ]
    with TestClient(app) as client:
        for body in bodies:
            response = _post(client, {"profile": _MAIN_PROFILE, **body})
            assert response.status_code == 422, body
            expected = (
                _EMPTY_STATUS_DETAIL
                if any("" in v for v in body.values())
                else _INVALID_STATUS_DETAIL
            )
            assert response.json() == {"detail": expected}, body


def test_status_list_type_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    bodies = [
        {"untracked": 123},
        {"untracked": [1]},
        {"include_statuses": "applied"},
        {"include_statuses": ["applied", 42]},
        {"exclude_statuses": True},
    ]
    with TestClient(app) as client:
        for body in bodies:
            response = _post(client, {"profile": _MAIN_PROFILE, **body})
            assert response.status_code == 422, body


# --------------------------------------------------------------------------- #
# Pairwise conflict validation
# --------------------------------------------------------------------------- #


def test_untracked_include_statuses_conflict(
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
                "untracked": True,
                "include_statuses": ["applied"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": _UNTRACKED_CONFLICT_DETAIL}


def test_untracked_exclude_statuses_conflict(
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
                "untracked": True,
                "exclude_statuses": ["applied"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": _UNTRACKED_CONFLICT_DETAIL}


def test_include_exclude_statuses_conflict(
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
                "include_statuses": ["applied"],
                "exclude_statuses": ["rejected"],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": _INCLUDE_EXCLUDE_CONFLICT_DETAIL}


def test_untracked_false_does_not_conflict_with_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {
                "profile": _MAIN_PROFILE,
                "untracked": False,
                "include_statuses": ["applied"],
            },
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]


# --------------------------------------------------------------------------- #
# Filter-before-score and filter-before-pagination
# --------------------------------------------------------------------------- #


def test_tracking_filter_applied_before_score_and_min_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unfiltered_high = _rank(client, {"profile": _MAIN_PROFILE}, minimum_score=30)
        untracked_all = _rank(client, {"profile": _MAIN_PROFILE, "untracked": True})
        untracked_high = _rank(
            client,
            {"profile": _MAIN_PROFILE, "untracked": True},
            minimum_score=30,
        )

    # Without the filter, the applied job (a-1, 53) clears the minimum score.
    assert _ids(unfiltered_high) == [job_ids["a-1"], job_ids["k-1"]]
    assert unfiltered_high["total"] == 2

    # The tracking filter removes tracked jobs before scoring: a-1 (53) is
    # gone entirely and the candidate count reflects only the untracked set.
    assert _ids(untracked_all) == [job_ids["k-1"], job_ids["j-1"]]
    assert untracked_all["total"] == 2

    # minimum_score then applies to the already-filtered candidate set: only
    # k-1 (53) survives; j-1 (25) is below the cutoff.
    assert _ids(untracked_high) == [job_ids["k-1"]]
    assert untracked_high["total"] == 1
    assert [item["score"] for item in untracked_high["items"]] == [53]  # type: ignore[index]


def test_tracking_filter_applied_before_limit_and_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, limit=1
        )
        second_page = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, offset=1, limit=1
        )

    assert _ids(first_page) == [job_ids["k-1"]]
    assert first_page["total"] == 2
    assert _ids(second_page) == [job_ids["j-1"]]
    assert second_page["total"] == 2


# --------------------------------------------------------------------------- #
# Grouped ranking behavior
# --------------------------------------------------------------------------- #


def test_grouped_baseline_collapses_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_grouped(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": _MAIN_PROFILE}, grouped="true")

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["j-1"], job_ids["a-1"], job_ids["k-1"]]


def test_grouped_untracked_drops_tracked_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_grouped(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, grouped="true"
        )

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]


def test_grouped_include_status_via_non_representative_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed_grouped(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_statuses": ["applied"]},
            grouped="true",
        )

    assert payload["total"] == 1
    item = payload["items"][0]
    group = item["group"]
    # The tracked member (r-1) is NOT the representative (a-1); the group is
    # still treated as tracked and kept as a whole.
    assert group["representative"]["id"] == job_ids["a-1"]
    assert [member["id"] for member in group["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    assert group["group_id"] == group_id


def test_grouped_exclude_status_drops_tracked_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_grouped(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(
            client,
            {"profile": _MAIN_PROFILE, "exclude_statuses": ["applied"]},
            grouped="true",
        )

    assert payload["total"] == 2
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]


def test_grouped_tracking_filter_with_min_score_and_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_grouped(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        high = _rank(
            client,
            {"profile": _MAIN_PROFILE, "untracked": True},
            grouped="true",
            minimum_score=30,
        )
        page = _rank(
            client,
            {"profile": _MAIN_PROFILE, "untracked": True},
            grouped="true",
            limit=1,
        )

    assert _ids(high) == [job_ids["j-1"]]
    assert high["total"] == 1
    assert _ids(page) == [job_ids["j-1"]]
    assert page["total"] == 2


# --------------------------------------------------------------------------- #
# Interactions with existing query/filter/score/pagination parameters
# --------------------------------------------------------------------------- #


def test_tracking_filter_composes_with_existing_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        untracked_source = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, source="remoteok"
        )
        untracked_remote = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, remote="true"
        )
        untracked_munich = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, location="Munich"
        )
        untracked_scientist = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, query="Scientist"
        )
        applied_query = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_statuses": ["applied"]},
            query="Engineer",
        )
        applied_location = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_statuses": ["applied"]},
            location="Berlin",
        )
        excluded_contract = _rank(
            client,
            {"profile": _MAIN_PROFILE, "exclude_statuses": ["applied"]},
            employment_type="Contract",
        )
        applied_min_score = _rank(
            client,
            {"profile": _MAIN_PROFILE, "include_statuses": ["applied"]},
            minimum_score=60,
        )

    assert _ids(untracked_source) == [job_ids["k-1"]]
    assert untracked_source["total"] == 1
    assert _ids(untracked_remote) == [job_ids["k-1"], job_ids["j-1"]]
    assert untracked_remote["total"] == 2
    assert untracked_munich["total"] == 0
    assert untracked_scientist["total"] == 0
    assert _ids(applied_query) == [job_ids["a-1"]]
    assert applied_query["total"] == 1
    assert _ids(applied_location) == [job_ids["a-1"]]
    assert applied_location["total"] == 1
    assert _ids(excluded_contract) == [job_ids["r-1"]]
    assert excluded_contract["total"] == 1
    assert applied_min_score["total"] == 0


def test_tracking_filter_with_empty_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"profile": {}, "untracked": True})

    assert payload["total"] == 2
    # Empty profile: every surviving job scores 0; deterministic order is then
    # published_at DESC, id ASC.
    assert _ids(payload) == [job_ids["j-1"], job_ids["k-1"]]
    assert all(item["match_score"] == 0 for item in payload["items"])  # type: ignore[index]


def test_tracking_filter_works_in_both_flat_and_grouped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_grouped(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        flat = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, grouped="false"
        )
        grouped = _rank(
            client, {"profile": _MAIN_PROFILE, "untracked": True}, grouped="true"
        )

    assert flat["total"] == 3
    assert _ids(flat) == [job_ids["j-1"], job_ids["a-1"], job_ids["k-1"]]
    assert grouped["total"] == 2
    assert _ids(grouped) == [job_ids["j-1"], job_ids["k-1"]]


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_absent_database_is_empty_without_creating_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v12"):
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
    with pytest.raises(ValueError, match="current schema v12"):
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
    with pytest.raises(ValueError, match="current schema v12"):
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
        _rank(client, {"profile": _MAIN_PROFILE})
        _rank(client, {"profile": _MAIN_PROFILE, "untracked": True})
        _rank(client, {"profile": _MAIN_PROFILE, "include_statuses": ["applied"]})
        _rank(client, {"profile": _MAIN_PROFILE, "exclude_statuses": ["interview"]})

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
    # The tracking state established during seeding is unchanged.
    with connect(path) as connection:
        statuses = {
            row["job_id"]: row["current_status"]
            for row in connection.execute(
                "SELECT job_id, current_status FROM applications"
            )
        }
        assert statuses == {job_ids["a-1"]: "applied", job_ids["r-1"]: "interview"}


def test_ranked_jobs_succeeds_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="test-holder"):
        with TestClient(app) as client:
            response = _post(client, {"profile": _MAIN_PROFILE, "untracked": True})

    assert response.status_code == 200
    assert response.json()["total"] == 2


# --------------------------------------------------------------------------- #
# Determinism and privacy
# --------------------------------------------------------------------------- #


def test_deterministic_repeated_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    body = {"profile": _MAIN_PROFILE, "include_statuses": ["applied", "interview"]}
    with TestClient(app) as client:
        first = _rank(client, body)
        second = _rank(client, body)

    assert first == second


def test_privacy_leak_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=_seed_job_id(path),
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=_seed_job_id(path),
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
            _post(client, {"profile": distinctive_profile, "untracked": True}),
            _post(
                client,
                {"profile": distinctive_profile, "include_statuses": ["applied"]},
            ),
            _post(
                client,
                {"profile": distinctive_profile, "exclude_statuses": ["rejected"]},
            ),
        ]

    for response in responses:
        assert response.status_code == 200
        assert list(response.json()) == ["items", "total", "offset", "limit"]
        for item in response.json()["items"]:
            assert list(item) == ["job", "match_score", "score", "explanation"]
            assert list(item["job"]) == _JOB_FIELDS
            assert "description" not in item["job"]

    leaked = (
        "NebulousRole",
        "Nowhereland",
        "mysticterm",
        "reasons",
        "score_components",
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
        str(path),
        str(_REAL_DATABASE),
        "Traceback",
        "sqlite3",
    )
    for response in responses:
        for marker in leaked:
            assert marker not in response.text


def _seed_job_id(path: Path) -> int:
    """Return the id of the a-1 job from an already-seeded flat database."""
    with connect(path) as connection:
        row = connection.execute(
            "SELECT id FROM jobs WHERE source_job_id = 'a-1'"
        ).fetchone()
    assert row is not None
    return row["id"]


# --------------------------------------------------------------------------- #
# Regression: prior behavior unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_and_ranked_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        applications = client.get("/applications")
        groups = client.get("/groups")
        review_queue = client.post("/review-queue", json={"profile": _MAIN_PROFILE})
        flat_ranked = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }
    assert jobs.status_code == 200
    assert jobs.json()["total"] == 4
    assert applications.status_code == 200
    assert applications.json()["total"] == 2
    assert groups.status_code == 200
    assert groups.json()["total"] == 4
    assert review_queue.status_code == 200
    assert flat_ranked.status_code == 200
    assert flat_ranked.json()["total"] == 4
    for item in flat_ranked.json()["items"]:
        assert list(item) == ["job", "match_score", "score", "explanation"]


def test_grouped_prior_behavior_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_grouped(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        grouped = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE},
            params={"grouped": "true"},
        )
        flat = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})

    assert grouped.status_code == 200
    assert grouped.json()["total"] == 3
    assert [
        item["group"]["representative"]["id"] for item in grouped.json()["items"]
    ] == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert flat.status_code == 200
    assert flat.json()["total"] == 4


# --------------------------------------------------------------------------- #
# Schema, import-boundary, and real-database invariants
# --------------------------------------------------------------------------- #


def test_schema_version_is_ten() -> None:
    assert SCHEMA_VERSION == 12


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
        _rank(client, {"profile": _MAIN_PROFILE, "untracked": True})
        _rank(client, {"profile": _MAIN_PROFILE, "include_statuses": ["applied"]})

    if real_bytes is None:
        assert not _REAL_DATABASE.exists()
    else:
        assert _REAL_DATABASE.read_bytes() == real_bytes
