"""Tests for the CareerOS saved-search execution endpoint (twelfth slice).

Covers ``POST /saved-searches/{name}/execute``: the response envelope and the
exact approved item shape for all four mode/grouping combinations, stored-filter
and stored-``result_limit`` authority, stored rank ``min_score``, ordering that
exactly matches the shared execution pipeline, ``offset``/``limit`` pagination
with ``total`` computed before slicing, deterministic repeated requests, the
``422`` contract (rank without profile, search with profile, invalid
pagination), the canonical ``404`` for an unknown name and for a missing
database, the privacy-safe ``500`` for uninitialized/malformed databases, the
guarantee that the stored ``profile_path`` is never read or dereferenced and
that unknown body fields (including ``profile_path`` probes) are inert, review
feedback always disabled, and the privacy guarantee that reasons, score
components, profile contents, descriptions, notes, decision text, reminder
messages, paths, and exception details never appear in any response.
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
from careeros.ranking.scorer import CandidateProfile
from careeros.reminders.service import add_reminder
from careeros.review.feedback import build_feedback_set
from careeros.review.service import set_decision
from careeros.saved_searches.execution import (
    execute_rank,
    execute_search,
    rank_candidates,
)
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search, get_search
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

_GROUP_FIELDS = ["group_id", "representative", "members", "sources", "urls"]

_RANKED_JOB_FIELDS = ["job", "match_score", "score", "explanation"]

_RANKED_GROUP_FIELDS = ["group", "match_score", "score", "explanation"]

_ENVELOPE_FIELDS = ["mode", "grouped", "items", "total", "offset", "limit"]

# Distinctive sentinels proving actual non-leakage of private state: if any
# string ever appears in response text, the API leaked a private value.
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/sentinel-candidate-profile.yaml"
_SENTINEL_SKILL = "SENTINEL-SKILL-7f3a2b9c"
_SENTINEL_APPLICATION_NOTE = "secret-cv-note"
_SENTINEL_REVIEW_NOTE = "secret-review-note"
_SENTINEL_REMINDER_MESSAGE = "secret-reminder-message"

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
    published_at: str,
    description: str,
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


def _seed_jobs(path: Path) -> tuple[dict[str, int], str]:
    """Seed four jobs with a canonical duplicate pair and recompute groups.

    Returns a mapping of source-job-id to ``jobs.id`` and the canonical group ID
    of the Senior Engineer duplicate pair (representative is the lower id).
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
                description="Python backend development team.",
            ),
            _make_job(
                "remotive",
                "r-1",
                "Senior Engineer",
                "Acme",
                "Berlin, Germany",
                published_at="2026-07-31T10:00:00Z",
                description="Python backend development team.",
            ),
            _make_job(
                "remoteok",
                "k-1",
                "Frontend Developer",
                "Frontend Labs",
                "Berlin",
                published_at="2026-07-30T10:00:00Z",
                description="React frontend team.",
            ),
            _make_job(
                "jobicy",
                "j-1",
                "Data Scientist",
                "Other Co",
                "Munich",
                remote_status="onsite",
                employment_type="Contract",
                published_at="2026-07-29T10:00:00Z",
                description="Python analysis team.",
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


def _create_searches(path: Path, sentinel_profile_path: str) -> None:
    """Create the saved-search definitions the suite exercises.

    Rank-mode definitions carry a sentinel ``profile_path`` that never exists on
    disk and must never be read by the API.
    """
    connection = connect(path)
    initialize_schema(connection)
    definitions = [
        SavedSearchFields(
            name="flat-search",
            query="engineer",
            mode="search",
            grouped=False,
            result_limit=10,
        ),
        SavedSearchFields(
            name="grouped-search",
            query="engineer",
            mode="search",
            grouped=True,
            result_limit=10,
        ),
        SavedSearchFields(
            name="filtered-search",
            query="engineer",
            mode="search",
            grouped=False,
            source="arbeitnow",
            result_limit=10,
        ),
        SavedSearchFields(
            name="capped-search",
            query="engineer",
            mode="search",
            grouped=False,
            result_limit=1,
        ),
        SavedSearchFields(
            name="team-search",
            query="team",
            mode="search",
            grouped=False,
            result_limit=10,
        ),
        SavedSearchFields(
            name="flat-rank",
            query="engineer",
            mode="rank",
            profile_path=sentinel_profile_path,
            grouped=False,
            min_score=None,
            result_limit=10,
        ),
        SavedSearchFields(
            name="grouped-rank",
            query="engineer",
            mode="rank",
            profile_path=sentinel_profile_path,
            grouped=True,
            min_score=None,
            result_limit=10,
        ),
        SavedSearchFields(
            name="min-score-rank",
            query="team",
            mode="rank",
            profile_path=sentinel_profile_path,
            grouped=False,
            min_score=40,
            result_limit=10,
        ),
        SavedSearchFields(
            name="empty-rank",
            query="engineer",
            mode="rank",
            profile_path=sentinel_profile_path,
            grouped=False,
            min_score=95,
            result_limit=10,
        ),
    ]
    for fields in definitions:
        create_search(connection, fields=fields, now=_EPOCH)
    connection.close()


def _profile() -> CandidateProfile:
    """The scorer profile equivalent of ``_MAIN_PROFILE`` via the API mapper."""
    return CandidateProfile(
        desired_titles=("Engineer",),
        skills=("Python",),
        preferred_locations=("Berlin",),
        remote_preference="remote",
        excluded_terms=(),
        required_skills=(),
        hard_excluded_terms=(),
        preferred_employment_types=(),
        preferred_seniority=(),
    )


def _seed(path: Path) -> tuple[dict[str, int], str]:
    """Seed jobs, grouping, and all saved-search definitions."""
    job_ids, group_id = _seed_jobs(path)
    _create_searches(path, _SENTINEL_PROFILE_PATH)
    return job_ids, group_id


def _execute(
    client: TestClient,
    name: str,
    *,
    params: dict[str, int] | None = None,
    body: dict[str, object] | None = None,
) -> object:
    """Run one execution request against the app under test."""
    return client.post(
        f"/saved-searches/{name}/execute",
        params=params,
        json=body,
    )


# --------------------------------------------------------------------------- #
# Search mode
# --------------------------------------------------------------------------- #


def test_search_flat_returns_job_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "flat-search")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload.keys()) == _ENVELOPE_FIELDS
    assert payload["mode"] == "search"
    assert payload["grouped"] is False
    assert payload["total"] == 2
    assert [item["id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    for item in payload["items"]:
        assert list(item.keys()) == _JOB_FIELDS


def test_search_grouped_returns_group_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "grouped-search")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "search"
    assert payload["grouped"] is True
    assert payload["total"] == 1
    (item,) = payload["items"]
    assert list(item.keys()) == _GROUP_FIELDS
    assert item["group_id"] == group_id
    assert item["representative"]["id"] == job_ids["a-1"]
    assert [member["id"] for member in item["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    assert item["sources"] == ["arbeitnow", "remotive"]
    assert item["urls"] == [
        "https://example.com/a-1",
        "https://example.com/r-1",
    ]


def test_search_stored_filters_are_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "filtered-search")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    (item,) = payload["items"]
    assert item["id"] == job_ids["a-1"]
    assert item["source"] == "arbeitnow"


def test_search_stored_result_limit_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "capped-search")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [item["id"] for item in payload["items"]] == [job_ids["a-1"]]


def test_search_mode_requires_no_profile_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        no_body = _execute(client, "flat-search")
        empty_body = _execute(client, "flat-search", body={})

    assert no_body.status_code == 200
    assert empty_body.status_code == 200
    assert no_body.json()["items"] == empty_body.json()["items"]


def test_search_mode_with_profile_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "flat-search", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 422
    assert response.json() == {
        "detail": "profile is only valid for rank-mode saved searches."
    }


# --------------------------------------------------------------------------- #
# Rank mode
# --------------------------------------------------------------------------- #


def test_rank_flat_returns_ranked_job_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "flat-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    payload = response.json()
    assert list(payload.keys()) == _ENVELOPE_FIELDS
    assert payload["mode"] == "rank"
    assert payload["grouped"] is False
    assert payload["total"] == 2
    for item in payload["items"]:
        assert list(item.keys()) == _RANKED_JOB_FIELDS
        assert list(item["job"].keys()) == _JOB_FIELDS
        assert isinstance(item["match_score"], int)
        assert isinstance(item["score"], int)


def test_rank_grouped_returns_ranked_group_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "grouped-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "rank"
    assert payload["grouped"] is True
    assert payload["total"] == 1
    (item,) = payload["items"]
    assert list(item.keys()) == _RANKED_GROUP_FIELDS
    assert list(item["group"].keys()) == _GROUP_FIELDS
    assert item["group"]["group_id"] == group_id
    assert item["group"]["representative"]["id"] == job_ids["a-1"]
    assert isinstance(item["match_score"], int)
    assert isinstance(item["score"], int)


def test_rank_stored_min_score_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    profile = _profile()
    connection = connect(path)
    initialize_schema(connection)
    search = get_search(connection, "min-score-rank")
    expected = execute_rank(connection, search, profile=profile, feedback=None)
    expected_ids = [item.job.id for item in expected.flat_ranked]
    full = rank_candidates(
        connection,
        query="team",
        profile=profile,
        limit=10,
        min_score=0,
        grouped=False,
    )
    connection.close()

    assert 0 < len(expected_ids) < len(full.flat_ranked)

    with TestClient(app) as client:
        response = _execute(client, "min-score-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    payload = response.json()
    assert [item["job"]["id"] for item in payload["items"]] == expected_ids
    assert payload["total"] == len(expected_ids)


def test_rank_empty_result_after_min_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "empty-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "rank"
    assert payload["total"] == 0
    assert payload["items"] == []


def test_rank_without_profile_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        empty_body = _execute(client, "flat-rank", body={})
        null_profile = _execute(client, "flat-rank", body={"profile": None})

    for response in (empty_body, null_profile):
        assert response.status_code == 422
        assert response.json() == {
            "detail": "profile is required to execute a rank-mode saved search."
        }


# --------------------------------------------------------------------------- #
# Ordering matches the shared execution pipeline
# --------------------------------------------------------------------------- #


def test_search_ordering_matches_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    connection = connect(path)
    initialize_schema(connection)
    search = get_search(connection, "flat-search")
    outcome = execute_search(connection, search)
    expected_ids = [job.id for job in outcome.flat]
    connection.close()

    with TestClient(app) as client:
        response = _execute(client, "flat-search")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == expected_ids


def test_search_grouped_ordering_matches_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    connection = connect(path)
    initialize_schema(connection)
    search = get_search(connection, "grouped-search")
    outcome = execute_search(connection, search)
    expected_representatives = [group.representative.id for group in outcome.groups]
    connection.close()

    with TestClient(app) as client:
        response = _execute(client, "grouped-search")

    assert response.status_code == 200
    assert [
        item["representative"]["id"] for item in response.json()["items"]
    ] == expected_representatives


def test_rank_ordering_matches_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    profile = _profile()
    connection = connect(path)
    initialize_schema(connection)
    search = get_search(connection, "flat-rank")
    expected = execute_rank(connection, search, profile=profile, feedback=None)
    expected_rows = [
        (item.job.id, item.match_score, item.score) for item in expected.flat_ranked
    ]
    connection.close()

    with TestClient(app) as client:
        response = _execute(client, "flat-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    rows = [
        (item["job"]["id"], item["match_score"], item["score"])
        for item in response.json()["items"]
    ]
    assert rows == expected_rows


def test_rank_grouped_ordering_matches_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    profile = _profile()
    connection = connect(path)
    initialize_schema(connection)
    search = get_search(connection, "grouped-rank")
    expected = execute_rank(connection, search, profile=profile, feedback=None)
    expected_rows = [
        (item.group.representative.id, item.match_score, item.score)
        for item in expected.grouped_ranked
    ]
    connection.close()

    with TestClient(app) as client:
        response = _execute(client, "grouped-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    rows = [
        (
            item["group"]["representative"]["id"],
            item["match_score"],
            item["score"],
        )
        for item in response.json()["items"]
    ]
    assert rows == expected_rows


# --------------------------------------------------------------------------- #
# Pagination and determinism
# --------------------------------------------------------------------------- #


def test_pagination_and_total_before_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _execute(client, "flat-search", params={"limit": 1})
        second = _execute(client, "flat-search", params={"offset": 1, "limit": 1})
        beyond = _execute(client, "flat-search", params={"offset": 10})

    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert [item["id"] for item in first.json()["items"]] == [job_ids["a-1"]]

    assert second.status_code == 200
    assert second.json()["total"] == 2
    assert [item["id"] for item in second.json()["items"]] == [job_ids["r-1"]]

    assert beyond.status_code == 200
    assert beyond.json()["total"] == 2
    assert beyond.json()["items"] == []


def test_invalid_pagination_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        negative_offset = _execute(client, "flat-search", params={"offset": -1})
        zero_limit = _execute(client, "flat-search", params={"limit": 0})
        over_limit = _execute(client, "flat-search", params={"limit": 101})

    assert negative_offset.status_code == 422
    assert zero_limit.status_code == 422
    assert over_limit.status_code == 422


def test_deterministic_repeated_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_search = _execute(client, "flat-search")
        second_search = _execute(client, "flat-search")
        first_rank = _execute(client, "flat-rank", body={"profile": _MAIN_PROFILE})
        second_rank = _execute(client, "flat-rank", body={"profile": _MAIN_PROFILE})

    assert first_search.json() == second_search.json()
    assert first_rank.json() == second_rank.json()


# --------------------------------------------------------------------------- #
# Name resolution
# --------------------------------------------------------------------------- #


def test_case_insensitive_name_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "FLAT-SEARCH")

    assert response.status_code == 200
    assert response.json()["mode"] == "search"


def test_unknown_name_is_404_with_canonical_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "No saved search named 'does-not-exist'."}


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_refuses_startup_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v10 database"
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_uninitialized_database_refuses_startup_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "empty.sqlite3"
    database_path.write_bytes(b"")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass

    assert str(exc_info.value) == "CareerOS requires a current schema v10 database"
    assert database_path.read_bytes() == before == b""


def test_malformed_database_refuses_startup_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "garbage.sqlite3"
    database_path.write_bytes(b"this is not a sqlite database")
    _set_database(monkeypatch, database_path)

    before = database_path.read_bytes()
    with pytest.raises(ValueError, match="current schema v10") as exc_info:
        with TestClient(app):
            pass

    message = str(exc_info.value)
    assert message == "CareerOS requires a current schema v10 database"
    assert database_path.read_bytes() == before
    assert str(database_path) not in message
    assert "not a database" not in message


# --------------------------------------------------------------------------- #
# Stored profile_path safety
# --------------------------------------------------------------------------- #


def test_stored_profile_path_never_read_or_dereferenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed_jobs(path)
    nonexistent = tmp_path / "no" / "such" / "sentinel-profile.yaml"
    assert not nonexistent.exists()
    _create_searches(path, str(nonexistent))
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "flat-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    assert str(nonexistent) not in response.text
    assert [item["job"]["id"] for item in response.json()["items"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]
    assert not nonexistent.exists()


def test_stored_profile_path_never_echoed_in_rank_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "grouped-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    assert _SENTINEL_PROFILE_PATH not in response.text


def test_unknown_body_field_profile_path_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    probe = {
        "profile": _MAIN_PROFILE,
        "profile_path": "/tmp/evil-profile.yaml",
        "description": "probe-description",
        "unknown": "probe",
    }
    with TestClient(app) as client:
        rank_response = _execute(client, "flat-rank", body=probe)
        search_response = _execute(
            client, "flat-search", body={"profile_path": "/tmp/evil-profile.yaml"}
        )

    assert rank_response.status_code == 200
    assert "/tmp/evil-profile.yaml" not in rank_response.text
    assert "probe-description" not in rank_response.text

    assert search_response.status_code == 200
    assert "/tmp/evil-profile.yaml" not in search_response.text


# --------------------------------------------------------------------------- #
# Privacy exclusions
# --------------------------------------------------------------------------- #


def test_private_state_never_appears_in_any_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed_jobs(path)
    connection = connect(path)
    initialize_schema(connection)
    set_application_status(
        connection,
        job_id=job_ids["a-1"],
        status="applied",
        note=_SENTINEL_APPLICATION_NOTE,
        now=_EPOCH,
    )
    set_decision(
        connection,
        job_id=job_ids["a-1"],
        decision="shortlist",
        note=_SENTINEL_REVIEW_NOTE,
        now=_EPOCH,
    )
    add_reminder(
        connection,
        job_id=job_ids["a-1"],
        kind="follow_up",
        due_at="2026-08-05T09:00:00+00:00",
        message=_SENTINEL_REMINDER_MESSAGE,
        now=_EPOCH,
    )
    connection.close()
    _create_searches(path, _SENTINEL_PROFILE_PATH)
    _set_database(monkeypatch, path)

    bodies = [
        {"profile": _MAIN_PROFILE | {"skills": [_SENTINEL_SKILL]}},
        {"profile": _MAIN_PROFILE | {"skills": [_SENTINEL_SKILL]}},
    ]
    with TestClient(app) as client:
        responses = [
            _execute(client, "flat-search"),
            _execute(client, "grouped-search"),
            _execute(client, "flat-rank", body=bodies[0]),
            _execute(client, "grouped-rank", body=bodies[1]),
        ]

    for response in responses:
        assert response.status_code == 200
        for leaked in (
            _SENTINEL_PROFILE_PATH,
            _SENTINEL_APPLICATION_NOTE,
            _SENTINEL_REVIEW_NOTE,
            _SENTINEL_REMINDER_MESSAGE,
            "reasons",
            "score_components",
            "profile",
            "description",
            "note",
            "message",
        ):
            assert leaked not in response.text


def test_four_combinations_use_exact_approved_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        flat_search = _execute(client, "flat-search")
        grouped_search = _execute(client, "grouped-search")
        flat_rank = _execute(client, "flat-rank", body={"profile": _MAIN_PROFILE})
        grouped_rank = _execute(client, "grouped-rank", body={"profile": _MAIN_PROFILE})

    assert all(list(item.keys()) == _JOB_FIELDS for item in flat_search.json()["items"])
    grouped_items = grouped_search.json()["items"]
    assert all(list(item.keys()) == _GROUP_FIELDS for item in grouped_items)
    assert all(
        list(member.keys()) == _JOB_FIELDS
        for item in grouped_items
        for member in item["members"]
    )
    assert all(
        list(item.keys()) == _RANKED_JOB_FIELDS for item in flat_rank.json()["items"]
    )
    ranked_grouped_items = grouped_rank.json()["items"]
    assert all(
        list(item.keys()) == _RANKED_GROUP_FIELDS for item in ranked_grouped_items
    )
    assert all(
        list(item["group"].keys()) == _GROUP_FIELDS for item in ranked_grouped_items
    )


# --------------------------------------------------------------------------- #
# Review feedback is always disabled
# --------------------------------------------------------------------------- #


def test_review_feedback_never_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids, _group_id = _seed_jobs(path)
    _create_searches(path, _SENTINEL_PROFILE_PATH)
    connection = connect(path)
    initialize_schema(connection)
    set_decision(
        connection,
        job_id=job_ids["a-1"],
        decision="shortlist",
        now=_EPOCH,
    )
    set_decision(
        connection,
        job_id=job_ids["r-1"],
        decision="dismiss",
        now=_EPOCH,
    )
    profile = _profile()
    search = get_search(connection, "flat-rank")
    no_feedback = execute_rank(connection, search, profile=profile, feedback=None)
    with_feedback = execute_rank(
        connection,
        search,
        profile=profile,
        feedback=build_feedback_set(connection),
    )
    no_scores = {item.job.id: item.score for item in no_feedback.flat_ranked}
    fb_scores = {item.job.id: item.score for item in with_feedback.flat_ranked}
    connection.close()

    assert no_scores != fb_scores

    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _execute(client, "flat-rank", body={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    api_scores = {item["job"]["id"]: item["score"] for item in response.json()["items"]}
    assert api_scores == no_scores


# --------------------------------------------------------------------------- #
# Regression: prior endpoints unchanged
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")
        jobs = client.get("/jobs")
        searches = client.get("/saved-searches")
        detail = client.get("/saved-searches/flat-search")
        ranked = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})

    assert health.status_code == 200
    assert health.json()["schema_version"] == SCHEMA_VERSION
    assert jobs.status_code == 200
    assert searches.status_code == 200
    assert searches.json()["total"] == 9
    assert detail.status_code == 200
    assert detail.json()["mode"] == "search"
    assert ranked.status_code == 200


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
