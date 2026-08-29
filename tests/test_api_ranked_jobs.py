"""Tests for the CareerOS ranked-jobs endpoint (seventh slice).

Covers ``POST /ranked-jobs``: the response envelope and item field order, the
canonical nested job projection, deterministic score ordering and tie-breaking,
filters applied before ranking, ranking before pagination, ``total`` before
pagination, minimum-score filtering, empty-profile behavior, database
availability semantics, request/profile validation limits, unknown-field
tolerance (including a ``profile_path`` probe), the guarantee that review
decisions never affect API scores, and the privacy guarantee that reasons,
score components, profile terms, descriptions, notes, decision text, reminder
messages, paths, and exception details never appear in any response.
"""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

import pytest

from careeros.api import app
from careeros.applications.service import set_application_status
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
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
    """Seed four jobs with distinct, deterministic scores.

    Against ``_MAIN_PROFILE`` the raw scores are: Senior Engineer 53, Backend
    Engineer 53, Frontend Developer 25, Data Scientist 8. Returns a mapping of
    source-job-id to ``jobs.id``.
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
    connection.close()
    return job_ids


def _seed_feedback_pair(path: Path) -> dict[str, int]:
    """Seed two identical Acme "Backend Engineer" jobs for the feedback test.

    Both score 28 against a title+skill profile and share a publication time,
    so without feedback the order is deterministic by ascending job ID. With
    review feedback enabled, a ``shortlist`` decision on the lower-ID job would
    lift the higher-ID job to 33 and flip the order — the API must never do
    that.
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


def _rank(client: TestClient, profile: object, **params: object) -> object:
    """POST /ranked-jobs with the given inline profile and query params."""
    response = client.post("/ranked-jobs", json={"profile": profile}, params=params)
    assert response.status_code == 200
    return response.json()


# --------------------------------------------------------------------------- #
# Normal response, field order, and deterministic ordering
# --------------------------------------------------------------------------- #


def test_ranked_returns_sorted_normal_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE)

    assert payload["total"] == 4
    assert [item["job"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
        job_ids["r-1"],
    ]
    assert [item["match_score"] for item in payload["items"]] == [53, 53, 25, 8]
    assert [item["score"] for item in payload["items"]] == [53, 53, 25, 8]


def test_ranked_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE)

    assert list(payload) == ["items", "total", "offset", "limit"]
    for item in payload["items"]:
        assert list(item) == ["job", "match_score", "score", "explanation"]
        assert set(item["explanation"]) == {
            "scope",
            "listing_count",
            "summary",
            "score",
            "matches",
            "gaps",
            "unknowns",
            "other_factors",
        }
        assert "description" not in item["explanation"]
        assert "reasons" not in item["explanation"]
        assert "score_components" not in item["explanation"]


def test_ranked_nested_job_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE)

    for item in payload["items"]:
        assert list(item["job"]) == _JOB_FIELDS
        assert "description" not in item["job"]


def test_ranked_deterministic_score_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _rank(client, _MAIN_PROFILE)
        second = _rank(client, _MAIN_PROFILE)

    assert first == second
    scores = [item["match_score"] for item in first["items"]]
    assert scores == sorted(scores, reverse=True)


def test_ranked_deterministic_tie_break(
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
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {"desired_titles": ["Engineer"], "skills": ["Python"]})

    assert [item["job"]["id"] for item in payload["items"]] == sorted(job_ids.values())
    assert len({item["match_score"] for item in payload["items"]}) == 1


# --------------------------------------------------------------------------- #
# Filtering, pagination, and minimum score
# --------------------------------------------------------------------------- #


def test_ranked_filters_applied_before_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        by_source = _rank(client, _MAIN_PROFILE, source="remotive")
        by_location = _rank(client, _MAIN_PROFILE, location="Berlin")
        by_remote = _rank(client, _MAIN_PROFILE, remote="true")
        by_type = _rank(client, _MAIN_PROFILE, employment_type="Contract")
        by_query = _rank(client, _MAIN_PROFILE, query="Scientist")

    assert [item["job"]["id"] for item in by_source["items"]] == [job_ids["r-1"]]
    assert by_source["total"] == 1

    assert [item["job"]["id"] for item in by_location["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
    ]
    assert by_location["total"] == 3

    assert [item["job"]["id"] for item in by_remote["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["j-1"],
    ]

    assert [item["job"]["id"] for item in by_type["items"]] == [job_ids["r-1"]]
    assert [item["job"]["id"] for item in by_query["items"]] == [job_ids["r-1"]]


def test_ranked_ranking_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _rank(client, _MAIN_PROFILE, limit=2)
        second_page = _rank(client, _MAIN_PROFILE, offset=2, limit=2)

    assert [item["job"]["id"] for item in first_page["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert [item["job"]["id"] for item in second_page["items"]] == [
        job_ids["j-1"],
        job_ids["r-1"],
    ]
    assert first_page["total"] == 4
    assert second_page["total"] == 4


def test_ranked_total_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE, offset=1, limit=2)

    assert payload["total"] == 4
    assert [item["job"]["id"] for item in payload["items"]] == [
        job_ids["k-1"],
        job_ids["j-1"],
    ]


def test_ranked_minimum_score_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        high = _rank(client, _MAIN_PROFILE, minimum_score=30)
        trimmed = _rank(client, _MAIN_PROFILE, minimum_score=30, limit=1)
        none_pass = _rank(client, _MAIN_PROFILE, minimum_score=60)

    assert [item["job"]["id"] for item in high["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert high["total"] == 2
    assert [item["job"]["id"] for item in trimmed["items"]] == [job_ids["a-1"]]
    assert trimmed["total"] == 2
    assert none_pass == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_ranked_empty_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, _MAIN_PROFILE, query="nonexistent-job-xyz")

    assert payload == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_ranked_empty_profile_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _rank(client, {})

    assert payload["total"] == 4
    assert [item["job"]["id"] for item in payload["items"]] == [
        job_ids["j-1"],
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]
    assert all(item["match_score"] == 0 for item in payload["items"])
    assert all(item["score"] == 0 for item in payload["items"])


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_ranked_absent_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v9"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_ranked_uninitialized_database_is_500(
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


def test_ranked_malformed_database_is_500(
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
# Request and profile validation
# --------------------------------------------------------------------------- #


def test_ranked_rejects_invalid_profile_types(
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
            response = client.post("/ranked-jobs", json={"profile": profile})
            assert response.status_code == 422, profile


def test_ranked_rejects_oversized_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)
    oversized = ["term"] * 101

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs", json={"profile": {"desired_titles": oversized}}
        )

    assert response.status_code == 422


def test_ranked_rejects_oversized_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        long_term = "x" * 101
        long_preference = "x" * 51
        long_term_response = client.post(
            "/ranked-jobs", json={"profile": {"skills": [long_term]}}
        )
        long_preference_response = client.post(
            "/ranked-jobs",
            json={"profile": {"remote_preference": long_preference}},
        )

    assert long_term_response.status_code == 422
    assert long_preference_response.status_code == 422


def test_ranked_rejects_whitespace_only_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs",
            json={"profile": {"desired_titles": ["Engineer", "   "]}},
        )

    assert response.status_code == 422


def test_ranked_rejects_invalid_minimum_score(
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
                params={"minimum_score": minimum_score},
            )
            assert response.status_code == 422


def test_ranked_rejects_invalid_pagination(
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
                params=params,
            )
            assert response.status_code == 422, params


def test_ranked_rejects_invalid_remote_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post(
            "/ranked-jobs", json={"profile": _MAIN_PROFILE}, params={"remote": "maybe"}
        )

    assert response.status_code == 422


def test_ranked_unknown_fields_ignored_including_profile_path(
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
        response = client.post("/ranked-jobs", json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    assert [item["job"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
        job_ids["r-1"],
        job_ids["j-1"],
    ]
    assert "/etc/passwd" not in response.text
    assert "/tmp/evil.sqlite3" not in response.text


# --------------------------------------------------------------------------- #
# Feedback and privacy
# --------------------------------------------------------------------------- #


def test_ranked_feedback_decisions_do_not_affect_scores(
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

    assert [item["job"]["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    assert [item["match_score"] for item in payload["items"]] == [28, 28]
    assert "secret-review-note" not in payload
    assert "shortlist" not in str(payload)
    assert "feedback" not in str(payload)
    assert "decision" not in str(payload)


def test_ranked_privacy_exclusions(
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
    # any echo of a profile term in the response would be detectable.
    distinctive_profile = {
        "desired_titles": ["NebulousRole"],
        "skills": ["Frobnicate"],
        "preferred_locations": ["Nowhereland"],
        "excluded_terms": ["mysticterm"],
    }
    with TestClient(app) as client:
        response = client.post("/ranked-jobs", json={"profile": distinctive_profile})

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 4
    for item in payload["items"]:
        assert list(item) == ["job", "match_score", "score", "explanation"]
        assert list(item["job"]) == _JOB_FIELDS
        assert "description" not in item["job"]

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
        "Python backend development.",
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
    assert groups.json()["total"] == 4

    assert summary.status_code == 200
    assert summary.json()["distinct_opportunities"] == 4

    assert ranked.status_code == 200
    assert ranked.json()["total"] == 4


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
