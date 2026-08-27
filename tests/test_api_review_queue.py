"""Tests for the CareerOS review-queue endpoint (eighth slice).

Covers ``POST /review-queue``: the response envelope and item field order, the
canonical nested group/job projection, deterministic score ordering and
tie-breaking, grouped vs flat duplicate handling, state filters, tracked
application exclusion and ``include_tracked``, filters applied before grouping,
``minimum_score`` trimming and total-before-pagination, database availability
semantics, request/profile validation limits, unknown-field tolerance
(including a ``profile_path`` probe), the guarantee that review decisions never
affect queue scores, and the privacy guarantee that reasons, score components,
profile terms, descriptions, notes, decision text, reminder messages, paths,
and exception details never appear in any response.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision

_SRC = Path(__file__).resolve().parents[1] / "src"

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

_ITEM_FIELDS = [
    "group",
    "match_score",
    "score",
    "review_decision",
    "application_status",
    "explanation",
]

_MAIN_PROFILE = {
    "desired_titles": ["Engineer"],
    "skills": ["Python"],
    "preferred_locations": ["Berlin"],
    "remote_preference": "remote",
}


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
    """Seed a two-member duplicate group plus two singletons.

    Against ``_MAIN_PROFILE`` the raw scores are: Senior Engineer group 53,
    Frontend Developer 25, Data Scientist 8. Grouping tables are recomputed so
    the two Acme Senior Engineer listings collapse into one canonical group.
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
                "Berlin, Germany",
                published_at="2026-08-01T09:00:00Z",
                description="Python backend development.",
            ),
            _make_job(
                "remoteok",
                "k-1",
                "Data Scientist",
                "Other Co",
                "Munich",
                remote_status=None,
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
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    ensure_grouped(connection)
    connection.close()
    return job_ids


def _seed_feedback_pair(path: Path) -> dict[str, int]:
    """Seed two identical Acme "Backend Engineer" jobs (no grouping tables).

    Both score 28 against a title+skill profile and share a publication time.
    A ``shortlist`` decision on the lower-ID job would lift the higher-ID job
    to 33 under review feedback — the API must never do that.
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
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.close()
    return job_ids


def _queue(client: TestClient, profile: object, **params: object) -> object:
    """POST /review-queue with the given inline profile and query params."""
    response = client.post("/review-queue", json={"profile": profile}, params=params)
    assert response.status_code == 200
    return response.json()


# --------------------------------------------------------------------------- #
# Normal response, field order, and deterministic ordering
# --------------------------------------------------------------------------- #


def test_review_queue_returns_ranked_normal_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE)

    assert payload["total"] == 3
    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["j-1"],
        job_ids["k-1"],
    ]
    assert [item["match_score"] for item in payload["items"]] == [53, 25, 8]
    assert [item["score"] for item in payload["items"]] == [53, 25, 8]
    assert all(item["review_decision"] is None for item in payload["items"])
    assert all(item["application_status"] is None for item in payload["items"])


def test_review_queue_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE)

    assert list(payload) == ["items", "total", "offset", "limit"]
    for item in payload["items"]:
        assert list(item) == _ITEM_FIELDS


def test_review_queue_nested_group_and_job_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE)

    for item in payload["items"]:
        group = item["group"]
        assert list(group) == _GROUP_FIELDS
        for job in (group["representative"], *group["members"]):
            assert list(job) == _JOB_FIELDS
            assert "description" not in job


def test_review_queue_deterministic_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _queue(client, _MAIN_PROFILE)
        second = _queue(client, _MAIN_PROFILE)

    assert first == second
    scores = [item["match_score"] for item in first["items"]]
    assert scores == sorted(scores, reverse=True)


def test_review_queue_deterministic_tie_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_feedback_pair(path)
    _set_database(monkeypatch, path)
    profile = {"desired_titles": ["Engineer"], "skills": ["Python"]}

    with TestClient(app) as client:
        payload = _queue(client, profile)

    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        sorted(job_ids.values())[0],
        sorted(job_ids.values())[1],
    ]
    assert len({item["match_score"] for item in payload["items"]}) == 1


def test_review_queue_empty_profile_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, {})

    assert payload["total"] == 3
    # All scores are 0, so ordering falls back to publication time (newest
    # first) then representative job ID.
    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert all(item["match_score"] == 0 for item in payload["items"])
    assert all(item["score"] == 0 for item in payload["items"])


# --------------------------------------------------------------------------- #
# Grouped vs flat
# --------------------------------------------------------------------------- #


def test_review_queue_grouped_collapses_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        grouped = _queue(client, _MAIN_PROFILE)
        flat = _queue(client, _MAIN_PROFILE, grouped="false")

    assert grouped["total"] == 3
    senior = next(
        item for item in grouped["items"] if item["group"]["group_id"] is not None
    )
    assert len(senior["group"]["members"]) == 2
    assert senior["group"]["sources"] == ["arbeitnow", "remotive"]
    assert senior["group"]["representative"]["id"] == job_ids["a-1"]

    assert flat["total"] == 4
    for item in flat["items"]:
        assert item["group"]["group_id"] is None
        assert len(item["group"]["members"]) == 1
    assert [item["group"]["representative"]["id"] for item in flat["items"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["j-1"],
        job_ids["k-1"],
    ]


# --------------------------------------------------------------------------- #
# State filters, tracked exclusion, and pre-grouping filters
# --------------------------------------------------------------------------- #


def test_review_queue_state_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["k-1"],
            decision="dismiss",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        unreviewed = _queue(client, _MAIN_PROFILE)
        shortlisted = _queue(client, _MAIN_PROFILE, state="shortlist")
        dismissed = _queue(client, _MAIN_PROFILE, state="dismiss")
        all_states = _queue(client, _MAIN_PROFILE, state="any")
        later = _queue(client, _MAIN_PROFILE, state="review_later")

    assert [item["group"]["representative"]["id"] for item in unreviewed["items"]] == [
        job_ids["j-1"]
    ]
    assert unreviewed["total"] == 1

    assert [item["group"]["representative"]["id"] for item in shortlisted["items"]] == [
        job_ids["a-1"]
    ]
    assert shortlisted["items"][0]["review_decision"] == "shortlist"

    assert [item["group"]["representative"]["id"] for item in dismissed["items"]] == [
        job_ids["k-1"]
    ]
    assert dismissed["items"][0]["review_decision"] == "dismiss"

    assert all_states["total"] == 3
    assert later == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_review_queue_excludes_tracked_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["j-1"],
            status="applied",
            note="secret-cv-note",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        default = _queue(client, _MAIN_PROFILE)
        include = _queue(client, _MAIN_PROFILE, include_tracked="true")

    assert [item["group"]["representative"]["id"] for item in default["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert default["total"] == 2

    assert [item["group"]["representative"]["id"] for item in include["items"]] == [
        job_ids["a-1"],
        job_ids["j-1"],
        job_ids["k-1"],
    ]
    assert include["total"] == 3
    applied_item = next(
        item
        for item in include["items"]
        if item["group"]["representative"]["id"] == job_ids["j-1"]
    )
    assert applied_item["application_status"] == "applied"
    assert "secret-cv-note" not in str(include)


def test_review_queue_filters_applied_before_grouping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        by_source = _queue(client, _MAIN_PROFILE, source="remotive")
        by_location = _queue(client, _MAIN_PROFILE, location="Berlin")
        by_remote = _queue(client, _MAIN_PROFILE, remote="true")
        by_type = _queue(client, _MAIN_PROFILE, employment_type="Contract")

    assert by_source["total"] == 1
    senior = by_source["items"][0]["group"]
    assert senior["representative"]["id"] == job_ids["r-1"]
    assert len(senior["members"]) == 1

    assert [item["group"]["representative"]["id"] for item in by_location["items"]] == [
        job_ids["a-1"],
        job_ids["j-1"],
    ]
    assert by_location["total"] == 2

    assert [item["group"]["representative"]["id"] for item in by_remote["items"]] == [
        job_ids["a-1"],
        job_ids["j-1"],
    ]
    assert by_remote["total"] == 2

    assert [item["group"]["representative"]["id"] for item in by_type["items"]] == [
        job_ids["k-1"]
    ]
    assert by_type["total"] == 1


# --------------------------------------------------------------------------- #
# Minimum score, pagination, and total-before-slicing
# --------------------------------------------------------------------------- #


def test_review_queue_minimum_score_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        high = _queue(client, _MAIN_PROFILE, minimum_score=30)
        none_pass = _queue(client, _MAIN_PROFILE, minimum_score=60)

    assert [item["group"]["representative"]["id"] for item in high["items"]] == [
        job_ids["a-1"]
    ]
    assert high["total"] == 1
    assert none_pass == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_review_queue_total_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _queue(client, _MAIN_PROFILE, limit=1)
        second_page = _queue(client, _MAIN_PROFILE, offset=1, limit=1)
        third_page = _queue(client, _MAIN_PROFILE, offset=2, limit=1)

    assert [item["group"]["representative"]["id"] for item in first_page["items"]] == [
        job_ids["a-1"]
    ]
    assert first_page["total"] == 3
    assert [item["group"]["representative"]["id"] for item in second_page["items"]] == [
        job_ids["j-1"]
    ]
    assert second_page["total"] == 3
    assert [item["group"]["representative"]["id"] for item in third_page["items"]] == [
        job_ids["k-1"]
    ]
    assert third_page["total"] == 3


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_review_queue_absent_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.post("/review-queue", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_review_queue_empty_database_returns_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.post("/review-queue", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_review_queue_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.post("/review-queue", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


def test_review_queue_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.post("/review-queue", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 500
    assert "run 'careeros collect" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Request and profile validation
# --------------------------------------------------------------------------- #


def test_review_queue_rejects_invalid_profile_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for profile in (
            "engineer",
            None,
            {"desired_titles": "Engineer"},
            {"skills": ["Python", 42]},
            {"remote_preference": 123},
            {"preferred_locations": [True]},
        ):
            response = client.post("/review-queue", json={"profile": profile})
            assert response.status_code == 422, profile


def test_review_queue_rejects_oversized_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)
    oversized = ["term"] * 101

    with TestClient(app) as client:
        response = client.post(
            "/review-queue", json={"profile": {"desired_titles": oversized}}
        )

    assert response.status_code == 422


def test_review_queue_rejects_oversized_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        long_term = "x" * 101
        long_preference = "x" * 51
        long_term_response = client.post(
            "/review-queue", json={"profile": {"skills": [long_term]}}
        )
        long_preference_response = client.post(
            "/review-queue",
            json={"profile": {"remote_preference": long_preference}},
        )

    assert long_term_response.status_code == 422
    assert long_preference_response.status_code == 422


def test_review_queue_rejects_whitespace_only_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/review-queue",
            json={"profile": {"desired_titles": ["Engineer", "   "]}},
        )

    assert response.status_code == 422


def test_review_queue_rejects_invalid_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for params in (
            {"state": "bogus"},
            {"minimum_score": -1},
            {"minimum_score": 101},
            {"limit": 0},
            {"limit": 101},
            {"offset": -1},
            {"remote": "maybe"},
        ):
            response = client.post(
                "/review-queue", json={"profile": _MAIN_PROFILE}, params=params
            )
            assert response.status_code == 422, params


def test_review_queue_unknown_fields_ignored_including_profile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    body = {
        "profile": {
            "desired_titles": ["Engineer"],
            "skills": ["Python"],
            "profile_path": "/etc/passwd",
            "unknown_profile_field": 123,
        },
        "profile_path": "/tmp/evil.sqlite3",
        "extra": [1, 2, 3],
    }
    with TestClient(app) as client:
        response = client.post("/review-queue", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    # Against the reduced title+skill profile the scores are 28/8/0, so the
    # Senior group leads, followed by Data Scientist then Frontend Developer.
    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
    ]
    assert "/etc/passwd" not in response.text
    assert "/tmp/evil.sqlite3" not in response.text


# --------------------------------------------------------------------------- #
# Feedback and privacy
# --------------------------------------------------------------------------- #


def test_review_queue_feedback_decisions_do_not_affect_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_feedback_pair(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)
    profile = {"desired_titles": ["Engineer"], "skills": ["Python"]}

    with TestClient(app) as client:
        payload = _queue(client, profile)

    # The reviewed lower-ID job is excluded by the unreviewed state filter, so
    # the queue holds exactly the unreviewed higher-ID job. Its score stays 28
    # (feedback would have lifted it to 33).
    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        job_ids["r-1"]
    ]
    assert payload["items"][0]["match_score"] == 28
    assert payload["items"][0]["review_decision"] is None
    assert "secret-review-note" not in str(payload)
    assert "shortlist" not in str(payload)
    assert "feedback" not in str(payload)


def test_review_queue_privacy_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="secret-cv-note",
            now=_EPOCH,
        )
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

    # A distinctive profile whose terms appear nowhere in the stored jobs, so
    # any echo of a profile term in the response would be detectable. The
    # decided/tracked Senior group is excluded by the default state and tracked
    # filters.
    distinctive_profile = {
        "desired_titles": ["NebulousRole"],
        "skills": ["Frobnicate"],
        "preferred_locations": ["Nowhereland"],
        "excluded_terms": ["mysticterm"],
    }
    with TestClient(app) as client:
        response = client.post("/review-queue", json={"profile": distinctive_profile})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    for item in payload["items"]:
        assert list(item) == _ITEM_FIELDS
        assert list(item["group"]) == _GROUP_FIELDS
        for job in (item["group"]["representative"], *item["group"]["members"]):
            assert list(job) == _JOB_FIELDS
            assert "description" not in job

    for leaked in (
        "NebulousRole",
        "Nowhereland",
        "mysticterm",
        "reasons",
        "score_components",
        "secret-cv-note",
        "secret-review-note",
        "secret-reminder-message",
        "shortlist",
        "dismiss",
        "review_later",
        "applied",
        "follow_up",
        "Python backend development.",
        "Python analysis.",
        "Build user interfaces.",
        "description",
        "note",
        "message",
        "conflict",
        str(path),
        "Traceback",
        "sqlite3",
    ):
        assert leaked not in response.text


# --------------------------------------------------------------------------- #
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(database_path)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        show = client.get(f"/jobs/{job_ids['a-1']}")
        missing = client.get("/jobs/9999")
        stats = client.get("/sources/stats")
        runs = client.get("/collection-runs")
        applications = client.get("/applications")
        reminders = client.get("/reminders")
        groups = client.get("/groups")
        summary = client.get("/groups/summary")
        ranked = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})
        queue = client.post("/review-queue", json={"profile": _MAIN_PROFILE})

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }

    assert jobs.status_code == 200
    assert jobs.json()["total"] == 4

    assert show.status_code == 200
    assert show.json()["title"] == "Senior Engineer"

    assert missing.status_code == 404
    assert missing.json() == {"detail": "No job found with ID 9999."}

    assert stats.status_code == 200
    assert stats.json()["total"] == len(COLLECTORS)

    assert runs.status_code == 200
    assert runs.json()["total"] == 0

    assert applications.status_code == 200
    assert applications.json()["total"] == 0

    assert reminders.status_code == 200
    assert reminders.json()["total"] == 0

    assert groups.status_code == 200
    assert groups.json()["total"] == 3

    assert summary.status_code == 200
    assert summary.json()["distinct_opportunities"] == 3

    assert ranked.status_code == 200
    assert ranked.json()["total"] == 4

    assert queue.status_code == 200
    assert queue.json()["total"] == 3


# --------------------------------------------------------------------------- #
# No CLI import
# --------------------------------------------------------------------------- #


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
