"""Tests for optional keyword search on ``POST /review-queue``.

Covers the additive ``query`` parameter: case-insensitive substring matching
across title/company/description through the shared ``search_grouped`` pipeline
applied to candidate member rows BEFORE grouping (so a matching member pulls in
its canonical duplicate group exactly as ``GET /groups?query=...`` does),
whitespace-only queries following the canonical search path, filter composition
(source/remote/location/employment_type/state/include_tracked/minimum_score with
AND semantics), empty/default compatibility (omitted or ``query=""`` stays
byte-identical to the pre-milestone queue), group members as the surviving
keyword-matching rows, flat (``grouped=false``) expansion consistent with the
queue's existing semantics, pagination and ``total`` before slicing,
deterministic ``score_groups`` ordering and repeated-request identity, absolute
privacy (descriptions may drive matching but stay structurally absent, no
private workflow text leaks), read-only/no-lock/no-side-effect behavior,
missing/uninitialized/malformed database handling, and regression/invariants
(neighboring keyword endpoints still work, ``SCHEMA_VERSION`` still 8, no
``careeros.cli`` import under ``src/careeros/api``, and the real
``data/database/careeros.sqlite3`` is never touched).

Against ``_MAIN_PROFILE`` the raw scores are: Senior Engineer group 53,
Frontend Developer 25, Data Scientist 8 (the same seed and profile as
``test_api_review_queue.py``).
"""

from __future__ import annotations

import ast
import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careeros.api import app
from careeros.api.deps import DATABASE_UNAVAILABLE_MESSAGE
from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import collection_lock, lock_path_for
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search

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

# Distinctive sentinels proving actual non-leakage of private fields: if any
# string ever appears in response text, the API leaked a private value.
_SENTINEL_DESCRIPTION = "PRIVATE-JOB-DESCRIPTION-9f41c7d2"
_SENTINEL_APPLICATION_NOTE = "private-application-note"
_SENTINEL_REVIEW_NOTE = "private-review-note"
_SENTINEL_REMINDER_MESSAGE = "private-reminder-message"
_SENTINEL_SAVED_SEARCH_DESCRIPTION = "private-saved-search-description"
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/candidate-profile.yaml"

_ALL_SENTINELS = (
    _SENTINEL_DESCRIPTION,
    _SENTINEL_APPLICATION_NOTE,
    _SENTINEL_REVIEW_NOTE,
    _SENTINEL_REMINDER_MESSAGE,
    _SENTINEL_SAVED_SEARCH_DESCRIPTION,
    _SENTINEL_PROFILE_PATH,
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
    """Seed a two-member duplicate group plus two singletons.

    - a-1 (arbeitnow)   "Senior Engineer", Acme GmbH, Berlin,     2026-08-01T10
    - r-1 (remotive)    "Senior Engineer", Acme, Berlin, Germany, 2026-08-01T09
      (a-1 + r-1 collapse into one canonical group; a-1 is representative)
    - k-1 (remoteok)    "Data Scientist", Other Co, Munich,       2026-07-30
      (onsite, Contract; description "Python analysis.")
    - j-1 (jobicy)      "Frontend Developer", Frontend Labs, Berlin, 2026-08-02

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


def _seed_private_state(path: Path) -> dict[str, int]:
    """Add private workflow rows (notes/messages/description/profile_path)."""
    job_ids = _seed(path)
    connection = connect(path)
    connection.execute(
        "UPDATE jobs SET description = ? WHERE id = ?",
        (f"Python analysis. {_SENTINEL_DESCRIPTION}", job_ids["k-1"]),
    )
    connection.commit()
    set_application_status(
        connection,
        job_id=job_ids["j-1"],
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
        due_at="2026-08-10T12:00:00+00:00",
        message=_SENTINEL_REMINDER_MESSAGE,
        now=_EPOCH,
    )
    create_search(
        connection,
        fields=SavedSearchFields(
            name="secret-search",
            query="python",
            mode="rank",
            profile_path=_SENTINEL_PROFILE_PATH,
        ),
    )
    connection.close()
    return job_ids


def _assert_no_private_leak(response_text: str) -> None:
    for sentinel in _ALL_SENTINELS:
        assert sentinel not in response_text, f"leaked {sentinel!r}"


def _queue_raw(client: TestClient, profile: object, **params: object) -> object:
    """POST /review-queue with the given inline profile and query params."""
    return client.post("/review-queue", json={"profile": profile}, params=params)


def _queue(client: TestClient, profile: object, **params: object) -> object:
    response = _queue_raw(client, profile, **params)
    assert response.status_code == 200
    return response.json()


def _ids(payload: object) -> list[int]:
    """Representative job ids in response order."""
    return [item["group"]["representative"]["id"] for item in payload["items"]]


# --------------------------------------------------------------------------- #
# A. Omitted/empty query compatibility
# --------------------------------------------------------------------------- #


def test_omitted_query_preserves_pre_milestone_queue_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE)

    # The exact shape/order the pre-milestone POST /review-queue returned:
    # total 3 with scores 53/25/8 (existing test_api_review_queue.py contract).
    assert list(payload) == ["items", "total", "offset", "limit"]
    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["a-1"], job_ids["j-1"], job_ids["k-1"]]
    assert [item["match_score"] for item in payload["items"]] == [53, 25, 8]


def test_empty_query_equals_omitted_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        omitted = client.post("/review-queue", json={"profile": _MAIN_PROFILE})
        empty = client.post(
            "/review-queue",
            json={"profile": _MAIN_PROFILE},
            params={"query": ""},
        )

    assert omitted.status_code == 200
    assert empty.status_code == 200
    assert empty.content == omitted.content


def test_empty_query_returns_full_unfiltered_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="")

    assert payload["total"] == 3
    assert _ids(payload) == [job_ids["a-1"], job_ids["j-1"], job_ids["k-1"]]


# --------------------------------------------------------------------------- #
# B. Whitespace-only query
# --------------------------------------------------------------------------- #


def test_whitespace_query_follows_canonical_search_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _queue_raw(client, _MAIN_PROFILE, query="   ")

    assert response.status_code == 200
    # A whitespace-only non-empty query is truthy and routes through the
    # canonical search_grouped path (pattern "%%  %%" matches no title/company/
    # description), exactly like GET /jobs and GET /groups.
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# C. Core keyword matching
# --------------------------------------------------------------------------- #


def test_query_matches_title_substring_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        lower = _queue(client, _MAIN_PROFILE, query="engineer")
        upper = _queue(client, _MAIN_PROFILE, query="ENGINEER")

    assert lower["total"] == 1
    assert _ids(lower) == [job_ids["a-1"]]
    assert upper == lower


def test_query_matches_company_substring_case_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="acme")

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["a-1"]]
    assert [m["id"] for m in payload["items"][0]["group"]["members"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
    ]


def test_query_matches_description_without_exposing_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _queue_raw(client, _MAIN_PROFILE, query="Python analysis")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]
    assert "description" not in payload["items"][0]
    assert "description" not in json.dumps(payload)
    assert "Python analysis." not in response.text
    _assert_no_private_leak(response.text)


def test_query_no_matches_returns_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _queue_raw(
            client,
            _MAIN_PROFILE,
            query="zzz-nonexistent-keyword",
            offset=3,
            limit=5,
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 3, "limit": 5}


# --------------------------------------------------------------------------- #
# D. Duplicate-group / flat behavior
# --------------------------------------------------------------------------- #


def test_query_matching_both_duplicate_members_collapses_to_one_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="Senior Engineer")

    assert payload["total"] == 1
    group = payload["items"][0]["group"]
    assert group["group_id"] is not None
    assert group["representative"]["id"] == job_ids["a-1"]
    assert [m["id"] for m in group["members"]] == [job_ids["a-1"], job_ids["r-1"]]
    assert group["sources"] == ["arbeitnow", "remotive"]


def test_query_matching_one_member_collapses_group_to_surviving_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="GmbH")

    # Only a-1 ("Acme GmbH") matches the keyword; keyword filtering applies to
    # candidate member rows BEFORE grouping, so the canonical group collapses to
    # its surviving matching member exactly as GET /groups?query=GmbH does.
    assert payload["total"] == 1
    group = payload["items"][0]["group"]
    assert group["group_id"] is not None
    assert [m["id"] for m in group["members"]] == [job_ids["a-1"]]


def test_query_grouped_and_flat_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        grouped = _queue(client, _MAIN_PROFILE, query="python")
        flat = _queue(client, {}, query="python", grouped="false")

    # grouped: Senior group (both members match via description) + Data Scientist.
    assert grouped["total"] == 2
    assert _ids(grouped) == [job_ids["a-1"], job_ids["k-1"]]
    assert [item["match_score"] for item in grouped["items"]] == [53, 8]

    # flat: sorted by score_groups (all scores 0 under the empty profile), so
    # fallback to newest publication time then representative id.
    assert flat["total"] == 3
    for item in flat["items"]:
        assert item["group"]["group_id"] is None
        assert len(item["group"]["members"]) == 1
    assert _ids(flat) == [job_ids["a-1"], job_ids["r-1"], job_ids["k-1"]]
    assert all(item["match_score"] == 0 for item in flat["items"])


# --------------------------------------------------------------------------- #
# E. Composition with base filters
# --------------------------------------------------------------------------- #


def test_query_composes_with_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="engineer", source="arbeitnow")

    assert payload["total"] == 1
    group = payload["items"][0]["group"]
    assert group["representative"]["id"] == job_ids["a-1"]
    assert len(group["members"]) == 1


def test_query_composes_with_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="engineer", location="Berlin")

    assert payload["total"] == 1
    group = payload["items"][0]["group"]
    assert [m["id"] for m in group["members"]] == [job_ids["a-1"], job_ids["r-1"]]


def test_query_composes_with_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="engineer", remote="true")

    assert payload["total"] == 1
    group = payload["items"][0]["group"]
    assert [m["id"] for m in group["members"]] == [job_ids["a-1"], job_ids["r-1"]]
    assert all(m["remote_status"] == "remote" for m in group["members"])


def test_query_composes_with_employment_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(
            client, _MAIN_PROFILE, query="python", employment_type="Contract"
        )

    assert payload["total"] == 1
    assert _ids(payload) == [job_ids["k-1"]]


def test_query_and_filters_use_and_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        query_only = _queue(client, _MAIN_PROFILE, query="python")
        narrowed = _queue(client, _MAIN_PROFILE, query="python", source="jobicy")

    assert query_only["total"] == 2
    assert _ids(query_only) == [job_ids["a-1"], job_ids["k-1"]]
    assert narrowed == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# F. Composition with review state
# --------------------------------------------------------------------------- #


def test_query_composes_with_state(
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
        shortlisted = _queue(client, _MAIN_PROFILE, query="engineer", state="shortlist")
        unreviewed = _queue(client, _MAIN_PROFILE, query="engineer", state="unreviewed")
        any_state = _queue(client, _MAIN_PROFILE, query="python", state="any")
        dismissed = _queue(client, _MAIN_PROFILE, query="python", state="dismiss")
        later = _queue(client, _MAIN_PROFILE, query="python", state="review_later")

    assert shortlisted["total"] == 1
    assert _ids(shortlisted) == [job_ids["a-1"]]
    assert shortlisted["items"][0]["review_decision"] == "shortlist"

    assert unreviewed == {"items": [], "total": 0, "offset": 0, "limit": 20}

    assert any_state["total"] == 2
    assert _ids(any_state) == [job_ids["a-1"], job_ids["k-1"]]

    assert dismissed["total"] == 1
    assert dismissed["items"][0]["review_decision"] == "dismiss"

    assert later == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# G. Composition with application tracking
# --------------------------------------------------------------------------- #


def test_query_composes_with_include_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["j-1"],
            status="applied",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        excluded = _queue(client, _MAIN_PROFILE, query="frontend")
        included = _queue(
            client,
            _MAIN_PROFILE,
            query="frontend",
            include_tracked="true",
        )

    assert excluded == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert included["total"] == 1
    assert _ids(included) == [job_ids["j-1"]]
    assert included["items"][0]["application_status"] == "applied"


# --------------------------------------------------------------------------- #
# H. Composition with minimum score
# --------------------------------------------------------------------------- #


def test_query_composes_with_minimum_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        high = _queue(client, _MAIN_PROFILE, query="python", minimum_score=30)
        none_pass = _queue(client, _MAIN_PROFILE, query="python", minimum_score=60)

    assert high["total"] == 1
    assert _ids(high) == [job_ids["a-1"]]
    assert none_pass == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# I. Pagination and total
# --------------------------------------------------------------------------- #


def test_total_counts_full_post_filter_queue_before_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        second_page = _queue(client, _MAIN_PROFILE, query="python", offset=1, limit=1)

    assert second_page["total"] == 2
    assert _ids(second_page) == [job_ids["k-1"]]


def test_offset_limit_slice_keyword_filtered_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first_page = _queue(client, _MAIN_PROFILE, query="python", offset=0, limit=1)
        second_page = _queue(client, _MAIN_PROFILE, query="python", offset=1, limit=1)

    assert _ids(first_page) == [job_ids["a-1"]]
    assert first_page["total"] == 2
    assert _ids(second_page) == [job_ids["k-1"]]
    assert second_page["total"] == 2


# --------------------------------------------------------------------------- #
# J. Deterministic ordering and response shape
# --------------------------------------------------------------------------- #


def test_query_deterministic_order_by_score_then_publish_then_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _queue(client, _MAIN_PROFILE, query="python")
        second = _queue(client, _MAIN_PROFILE, query="python")

    assert first == second
    scores = [item["match_score"] for item in first["items"]]
    assert scores == sorted(scores, reverse=True)
    assert [item["score"] for item in first["items"]] == scores


def test_repeated_identical_queries_are_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        query = {"profile": _MAIN_PROFILE}
        first = client.post("/review-queue", json=query, params={"query": "acme"})
        second = client.post("/review-queue", json=query, params={"query": "acme"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.content == first.content


def test_envelope_item_and_group_field_order_with_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = _queue(client, _MAIN_PROFILE, query="python")

    assert list(payload) == ["items", "total", "offset", "limit"]
    for item in payload["items"]:
        assert list(item) == _ITEM_FIELDS
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
        assert "reasons" not in item["explanation"]
        assert "score_components" not in item["explanation"]
        group = item["group"]
        assert list(group) == _GROUP_FIELDS
        for job in (group["representative"], *group["members"]):
            assert list(job) == _JOB_FIELDS
            assert "description" not in job


# --------------------------------------------------------------------------- #
# K. Database availability
# --------------------------------------------------------------------------- #


def test_missing_database_empty_200_creates_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "sub" / "missing.sqlite3"
    _set_database(monkeypatch, missing)

    with TestClient(app) as client:
        response = _queue_raw(client, _MAIN_PROFILE, query="python")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}
    assert not (tmp_path / "sub").exists()


def test_empty_initialized_database_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    connection = connect(path)
    initialize_schema(connection)
    connection.close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = _queue_raw(client, _MAIN_PROFILE, query="python")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_uninitialized_database_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        with_query = _queue_raw(client, _MAIN_PROFILE, query="python")
        without_query = _queue_raw(client, _MAIN_PROFILE)

    assert with_query.status_code == 500
    assert with_query.json()["detail"] == DATABASE_UNAVAILABLE_MESSAGE
    assert without_query.status_code == 500
    assert without_query.json()["detail"] == DATABASE_UNAVAILABLE_MESSAGE


def test_malformed_database_is_privacy_safe_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database file at all")
    _set_database(monkeypatch, malformed)

    with TestClient(app) as client:
        response = _queue_raw(client, _MAIN_PROFILE, query="python")

    assert response.status_code == 500
    assert response.json() == {"detail": DATABASE_UNAVAILABLE_MESSAGE}


# --------------------------------------------------------------------------- #
# L. Read-only, no side effects, lock compatibility
# --------------------------------------------------------------------------- #


def test_endpoint_performs_no_writes_and_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with connect(path) as connection:
        jobs_before = connection.execute(
            "SELECT id, title, company FROM jobs ORDER BY id"
        ).fetchall()

    with TestClient(app) as client:
        _queue_raw(client, _MAIN_PROFILE, query="python")
        _queue_raw(client, _MAIN_PROFILE, query="")
        _queue_raw(client, _MAIN_PROFILE, query="GmbH")

    with connect(path) as connection:
        jobs_after = connection.execute(
            "SELECT id, title, company FROM jobs ORDER BY id"
        ).fetchall()
        runs = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]

    assert [tuple(row) for row in jobs_after] == [tuple(row) for row in jobs_before]
    assert runs == 0
    assert not lock_path_for(path).exists()


def test_read_only_works_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="collect due"):
        with TestClient(app) as client:
            response = _queue_raw(client, _MAIN_PROFILE, query="python")

    assert response.status_code == 200
    assert _ids(response.json()) == [job_ids["a-1"], job_ids["k-1"]]


# --------------------------------------------------------------------------- #
# M. Privacy
# --------------------------------------------------------------------------- #


def test_privacy_sentinel_scan_across_query_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_private_state(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        responses = [
            _queue_raw(client, _MAIN_PROFILE, query=_SENTINEL_DESCRIPTION),
            _queue_raw(client, _MAIN_PROFILE, query="python"),
            _queue_raw(client, _MAIN_PROFILE, query="Acme"),
            _queue_raw(client, _MAIN_PROFILE, query=""),
            _queue_raw(client, _MAIN_PROFILE),
        ]

    # The description sentinel only lives in k-1's description; matching it must
    # return k-1 with the description structurally absent.
    sentinel_response = responses[0]
    assert sentinel_response.status_code == 200
    sentinel = sentinel_response.json()
    assert sentinel["total"] == 1
    assert _ids(sentinel) == [job_ids["k-1"]]
    assert "description" not in json.dumps(sentinel)

    for response in responses:
        assert response.status_code == 200
        _assert_no_private_leak(response.text)

    # Coarse leak-token scan over the strictest controls (no tracked/reviewed
    # values appear because the default state excludes decided/tracked jobs).
    for response in responses:
        for leaked in (
            "description",
            "note",
            "message",
            "shortlist",
            "follow_up",
            # "applied" cannot leak either: the only tracked job (j-1) matches no
            # keyword in any response here.
            "applied",
            "Traceback",
            "sqlite3",
            str(path),
        ):
            assert leaked not in response.text, f"leaked {leaked!r}"


# --------------------------------------------------------------------------- #
# N. Regression and architecture invariants
# --------------------------------------------------------------------------- #


def test_neighboring_keyword_endpoints_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        jobs = client.get("/jobs", params={"query": "python"})
        groups = client.get("/groups", params={"query": "python"})
        ranked = client.post(
            "/ranked-jobs",
            json={"profile": _MAIN_PROFILE},
            params={"query": "python"},
        )
        queue = client.post("/review-queue", json={"profile": _MAIN_PROFILE})
        detail = client.get(f"/jobs/{job_ids['j-1']}")
        job_group = client.get(f"/jobs/{job_ids['a-1']}/group")

    assert jobs.status_code == 200
    assert jobs.json()["total"] == 3
    assert groups.status_code == 200
    assert [item["representative"]["id"] for item in groups.json()["items"]] == [
        job_ids["a-1"],
        job_ids["k-1"],
    ]
    assert ranked.status_code == 200
    assert [item["job"]["id"] for item in ranked.json()["items"]] == [
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["k-1"],
    ]
    assert queue.status_code == 200
    assert queue.json()["total"] == 3
    assert detail.status_code == 200
    assert detail.json()["title"] == "Frontend Developer"
    assert job_group.status_code == 200
    members = [m["id"] for m in job_group.json()["members"]]
    assert members == [job_ids["a-1"], job_ids["r-1"]]


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH, database_path

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        _queue_raw(client, _MAIN_PROFILE, query="python")
        _queue_raw(client, _MAIN_PROFILE)

    assert database_path() == path
    assert not real.is_file() or real.stat().st_mtime_ns == before


def test_schema_version_remains_8() -> None:
    assert SCHEMA_VERSION == 8


def test_api_does_not_import_careeros_cli() -> None:
    api_root = _SRC / "careeros" / "api"
    violations: list[str] = []
    for file in sorted(api_root.rglob("*.py")):
        if file.name == "__init__.py":
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "careeros.cli" or name.startswith("careeros.cli."):
                    violations.append(f"{file.relative_to(_SRC)}: {name}")
    assert violations == []
