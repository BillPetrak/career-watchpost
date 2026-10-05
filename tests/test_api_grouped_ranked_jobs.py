"""Tests for grouped ranking on the ranked-jobs endpoint.

Covers ``POST /ranked-jobs`` with ``grouped=true``: duplicate collapse into one
result per canonical opportunity, the response envelope and exact item/group/
nested-job field order, deterministic grouped ranking and tie-breaking, filters
applied to candidate member rows before grouping, pagination and
``total``-before-slicing semantics, ``minimum_score`` filtering, database
availability semantics, request/profile validation limits, unknown-field
tolerance (including a ``profile_path`` probe), the guarantee that review
decisions never affect API scores, the privacy guarantee that reasons, score
components, review decisions, application status, notes, descriptions, profile
terms, paths, and exception details never appear in any response, the guarantee
that flat ``POST /ranked-jobs`` behavior is unchanged, and the guarantee that
existing endpoints are unchanged.
"""

from __future__ import annotations

import ast
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
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision
from tests.support import TestClient

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


def _seed(path: Path) -> tuple[dict[str, int], str]:
    """Seed a duplicate pair plus two singletons and recompute groups.

    Against ``_MAIN_PROFILE`` the grouped scores are: the Senior-Engineer-style
    singleton and the Backend-Engineer duplicate group both 53 (the singleton
    publishes one day later, so it ranks first), and the Data Scientist 8.
    Returns a mapping of source-job-id to ``jobs.id`` and the canonical group
    ID of the Backend Engineer duplicate pair.
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
    ensure_grouped(connection)
    group_id = connection.execute(
        "SELECT group_id FROM job_group_members WHERE job_id = ?",
        (job_ids["a-1"],),
    ).fetchone()[0]
    connection.close()
    return job_ids, group_id


def _seed_feedback_pair(path: Path) -> dict[str, int]:
    """Seed two identical Backend Engineer listings that group together.

    Both members carry the same facts, so the group scores 28 against a
    title+skill profile. A ``shortlist`` decision on one member must never
    alter the group's score or order through the API.
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
    ensure_grouped(connection)
    connection.close()
    return job_ids


def _rank(
    client: TestClient,
    profile: object,
    *,
    grouped: bool = True,
    **params: object,
) -> object:
    """POST /ranked-jobs with the given inline profile and query params."""
    response = client.post(
        "/ranked-jobs",
        json={"profile": profile},
        params={"grouped": "true" if grouped else "false", **params},
    )
    assert response.status_code == 200
    return response.json()


# --------------------------------------------------------------------------- #
# Normal response, field order, and deterministic ordering
# --------------------------------------------------------------------------- #


def test_grouped_collapses_duplicates_into_opportunities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE)

    assert payload["total"] == 3
    representatives = [
        item["group"]["representative"]["id"] for item in payload["items"]
    ]
    assert representatives == [job_ids["j-1"], job_ids["a-1"], job_ids["k-1"]]
    duplicate_group = payload["items"][1]["group"]
    assert duplicate_group["group_id"] == group_id
    assert [member["id"] for member in duplicate_group["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    assert duplicate_group["sources"] == ["arbeitnow", "remotive"]


def test_grouped_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE)

    assert list(payload) == ["items", "total", "offset", "limit"]
    for item in payload["items"]:
        assert list(item) == ["group", "match_score", "score", "explanation"]


def test_grouped_group_and_job_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE)

    for item in payload["items"]:
        assert list(item["group"]) == _GROUP_FIELDS
        assert list(item["group"]["representative"]) == _JOB_FIELDS
        assert "description" not in item["group"]["representative"]
        for member in item["group"]["members"]:
            assert list(member) == _JOB_FIELDS
            assert "description" not in member


def test_grouped_deterministic_ranking_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _rank(client, _MAIN_PROFILE)
        second = _rank(client, _MAIN_PROFILE)

    assert first == second
    assert [item["match_score"] for item in first["items"]] == [53, 53, 8]
    assert [item["group"]["representative"]["id"] for item in first["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["k-1"],
    ]


def test_grouped_deterministic_tie_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
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
                "Munich",
                published_at="2026-08-01T10:00:00Z",
                description="Python services.",
            ),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    ensure_grouped(connection)
    connection.close()
    _set_database(monkeypatch, path)
    profile = {"desired_titles": ["Engineer"]}

    with TestClient(app) as client:
        payload = _rank(client, profile)

    assert payload["total"] == 2
    assert [
        item["group"]["representative"]["id"] for item in payload["items"]
    ] == sorted(job_ids.values())
    assert len({item["match_score"] for item in payload["items"]}) == 1


# --------------------------------------------------------------------------- #
# Filtering, pagination, and minimum score
# --------------------------------------------------------------------------- #


def test_grouped_filters_applied_before_grouping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        by_source = _rank(client, _MAIN_PROFILE, source="remotive")
        by_location = _rank(client, _MAIN_PROFILE, location="Munich")
        by_remote = _rank(client, _MAIN_PROFILE, remote="true")
        by_type = _rank(client, _MAIN_PROFILE, employment_type="Contract")
        by_query = _rank(client, _MAIN_PROFILE, query="Scientist")

    assert [item["group"]["representative"]["id"] for item in by_source["items"]] == [
        job_ids["r-1"]
    ]
    assert by_source["total"] == 1
    assert by_source["items"][0]["group"]["members"] == [
        by_source["items"][0]["group"]["representative"]
    ]

    assert [item["group"]["representative"]["id"] for item in by_location["items"]] == [
        job_ids["k-1"]
    ]
    assert by_location["total"] == 1

    assert [item["group"]["representative"]["id"] for item in by_remote["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
    ]
    assert by_remote["total"] == 2

    assert [item["group"]["representative"]["id"] for item in by_type["items"]] == [
        job_ids["k-1"]
    ]
    assert [item["group"]["representative"]["id"] for item in by_query["items"]] == [
        job_ids["k-1"]
    ]


def test_grouped_ranking_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _rank(client, _MAIN_PROFILE, limit=2)
        second_page = _rank(client, _MAIN_PROFILE, offset=2, limit=2)

    assert [item["group"]["representative"]["id"] for item in first_page["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
    ]
    assert [item["group"]["representative"]["id"] for item in second_page["items"]] == [
        job_ids["k-1"]
    ]
    assert first_page["total"] == 3
    assert second_page["total"] == 3


def test_grouped_total_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE, offset=1, limit=2)

    assert payload["total"] == 3
    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
    ]


def test_grouped_minimum_score_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        high = _rank(client, _MAIN_PROFILE, minimum_score=30)
        trimmed = _rank(client, _MAIN_PROFILE, minimum_score=30, limit=1)
        none_pass = _rank(client, _MAIN_PROFILE, minimum_score=60)

    assert [item["group"]["representative"]["id"] for item in high["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
    ]
    assert high["total"] == 2
    assert [item["group"]["representative"]["id"] for item in trimmed["items"]] == [
        job_ids["j-1"]
    ]
    assert trimmed["total"] == 2
    assert none_pass == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_grouped_empty_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE, query="nonexistent-job-xyz")

    assert payload == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_grouped_empty_profile_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {})

    assert payload["total"] == 3
    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert all(item["match_score"] == 0 for item in payload["items"])
    assert all(item["score"] == 0 for item in payload["items"])


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_grouped_absent_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_grouped_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


def test_grouped_malformed_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert database_path.read_bytes() == before


# --------------------------------------------------------------------------- #
# Request and profile validation
# --------------------------------------------------------------------------- #


def test_grouped_rejects_invalid_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
            response = client.post(
                "/ranked-jobs",
                json={"profile": _MAIN_PROFILE},
                params={"grouped": "true", **params},
            )
            assert response.status_code == 422, params


def test_grouped_rejects_invalid_minimum_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for minimum_score in (-1, 101):
            response = client.post(
                "/ranked-jobs",
                json={"profile": _MAIN_PROFILE},
                params={"grouped": "true", "minimum_score": minimum_score},
            )
            assert response.status_code == 422


def test_grouped_rejects_invalid_remote_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE},
            params={"grouped": "true", "remote": "maybe"},
        )

    assert response.status_code == 422


def test_grouped_rejects_invalid_profile_types(
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
            response = client.post(
                "/ranked-jobs",
                json={"profile": profile},
                params={"grouped": "true"},
            )
            assert response.status_code == 422, profile


def test_grouped_rejects_oversized_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)
    oversized = ["term"] * 101

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={"profile": {"desired_titles": oversized}},
            params={"grouped": "true"},
        )

    assert response.status_code == 422


def test_grouped_rejects_oversized_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        long_term = "x" * 101
        long_preference = "x" * 51
        long_term_response = client.post(
            "/ranked-jobs",
            json={"profile": {"skills": [long_term]}},
            params={"grouped": "true"},
        )
        long_preference_response = client.post(
            "/ranked-jobs",
            json={"profile": {"remote_preference": long_preference}},
            params={"grouped": "true"},
        )

    assert long_term_response.status_code == 422
    assert long_preference_response.status_code == 422


def test_grouped_rejects_whitespace_only_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={"profile": {"desired_titles": ["Engineer", "   "]}},
            params={"grouped": "true"},
        )

    assert response.status_code == 422


def test_grouped_unknown_fields_ignored_including_profile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
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
        response = client.post("/ranked-jobs", json=body, params={"grouped": "true"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert [item["group"]["representative"]["id"] for item in payload["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert "/etc/passwd" not in response.text
    assert "/tmp/evil.sqlite3" not in response.text


# --------------------------------------------------------------------------- #
# Feedback and privacy
# --------------------------------------------------------------------------- #


def test_grouped_feedback_decisions_do_not_affect_scores(
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
        payload = _rank(client, profile)

    assert payload["total"] == 1
    assert payload["items"][0]["match_score"] == 28
    assert payload["items"][0]["score"] == 28
    assert "secret-review-note" not in payload
    assert "shortlist" not in str(payload)
    assert "feedback" not in str(payload)
    assert "decision" not in str(payload)


def test_grouped_privacy_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
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
    # any echo of a profile term in the response would be detectable.
    distinctive_profile = {
        "desired_titles": ["NebulousRole"],
        "skills": ["Frobnicate"],
        "preferred_locations": ["Nowhereland"],
        "excluded_terms": ["mysticterm"],
    }
    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={"profile": distinctive_profile},
            params={"grouped": "true"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    for item in payload["items"]:
        assert list(item) == ["group", "match_score", "score", "explanation"]
        assert list(item["group"]) == _GROUP_FIELDS
        assert list(item["group"]["representative"]) == _JOB_FIELDS
        assert "description" not in item["group"]["representative"]

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
        "applied",
        "follow_up",
        "review_decision",
        "application_status",
        "Python services.",
        "Python analysis.",
        "description",
        "note",
        "decision",
        "message",
        str(path),
        "Traceback",
        "sqlite3",
    ):
        assert leaked not in response.text


# --------------------------------------------------------------------------- #
# Regression: flat behavior and prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_grouped_flat_default_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)[0]
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        omitted = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})
        explicit = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE},
            params={"grouped": "false"},
        )

    for payload in (omitted.json(), explicit.json()):
        assert payload["total"] == 4
        assert [item["job"]["id"] for item in payload["items"]] == [
            job_ids["j-1"],
            job_ids["a-1"],
            job_ids["r-1"],
            job_ids["k-1"],
        ]
        assert [item["match_score"] for item in payload["items"]] == [53, 53, 53, 8]
        for item in payload["items"]:
            assert list(item) == ["job", "match_score", "score", "explanation"]


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(database_path)
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        groups = client.get("/groups")
        review_queue = client.post("/review-queue", json={"profile": _MAIN_PROFILE})
        flat_ranked = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})
        grouped_ranked = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE},
            params={"grouped": "true"},
        )

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }

    assert jobs.status_code == 200
    assert jobs.json()["total"] == 4

    assert groups.status_code == 200
    assert groups.json()["total"] == 3

    assert review_queue.status_code == 200
    assert review_queue.json()["total"] == 3

    assert flat_ranked.status_code == 200
    assert flat_ranked.json()["total"] == 4

    assert grouped_ranked.status_code == 200
    assert grouped_ranked.json()["total"] == 3
    duplicate_members = [
        member["id"] for member in grouped_ranked.json()["items"][1]["group"]["members"]
    ]
    assert duplicate_members == [job_ids["a-1"], job_ids["r-1"]]


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
