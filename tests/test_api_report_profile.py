"""Tests for the CareerOS inline-profile report API (``POST /report``).

Covers the personalized Career Snapshot milestone: ``POST /report`` accepts an
optional inline candidate profile and returns the existing ``ReportResponse``.
It reuses the shared ``reporting.service.build_report`` domain service with the
same read-only guarantees and the same strict privacy allow-list as
``GET /report``.

Coverage: byte-identical profile-less behavior for the three omitted-body
forms (no body, ``{}``, ``{"profile": null}``) versus ``GET /report`` with
equivalent settings; a personalized review queue and recommendations with
correct ``source="profile"`` / null ``source_name`` / null ``note`` metadata
and populated ``candidate_count``; tracked-applications exclusion; flat and
grouped modes (cross-source duplicates collapse into one scored entry per
canonical group, with the ``grouped`` flag echoed in both sections); empty
profiles and the shared ``CandidateProfileRequest`` validation limits;
unknown-body-field tolerance and an inert ``profile_path`` probe; ``limit``
semantics (default 10, boundaries, out-of-range 422s, and section caps);
deterministic repeated calls with the expected ordering of the review queue and
recommendations; missing/uninitialized/malformed database behavior (missing ->
200 empty report with no file/dir/lock creation, uninitialized/malformed ->
canonical privacy-safe 500) and malformed stored timestamps; strict read-only
behavior (database bytes unchanged, no run-history recording, works while the
collection lock is held); a privacy leak scan over notes, messages,
descriptions, reasons, score components, feedback, notification summaries,
last-error text, ``profile_path``, and the submitted profile values
themselves; the ``ReportResponse`` field/order contract; the schema-version
invariant; the shared no-``careeros.cli`` import guarantee; the rule that the
real default ``data/database/careeros.sqlite3`` is never touched; and
regressions of ``POST /ranked-jobs``, ``POST /review-queue``, and the
saved-search HTTP endpoints.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
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
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import SCHEMA_VERSION, initialize_schema
from careeros.locking import collection_lock
from careeros.reminders.service import add_reminder
from careeros.review.service import set_decision
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search

_SRC = Path(__file__).resolve().parents[1] / "src"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

# Distinctive sentinels proving actual non-leakage of private fields: if any
# string ever appears in a response, the API leaked a private value.
_SENTINEL_DESCRIPTION = "PRIVATE-JOB-DESCRIPTION-9f41c7d2"
_SENTINEL_APPLICATION_NOTE = "private-application-note"
_SENTINEL_REVIEW_NOTE = "private-review-note"
_SENTINEL_REMINDER_MESSAGE = "private-reminder-message"
_SENTINEL_SAVED_SEARCH_DESCRIPTION = "private-saved-search-description"
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/candidate-profile.yaml"
_SENTINEL_COLLECTION_ERROR = "PRIVATE-ERROR: connection reset by peer"
_SENTINEL_DIRECTORY = "private-secret-directory"
_SENTINEL_PROFILE_TITLE = "PRIVATE-PROFILE-TITLE-sentinel"
_SENTINEL_PROFILE_SKILL = "PRIVATE-PROFILE-SKILL-sentinel"

_REVIEW_QUEUE_NOTE = "No profile or saved search provided; no review queue computed."
_RECOMMENDATIONS_NOTE = (
    "No profile or saved search provided; no recommendations computed."
)

_ALL_SENTINELS = (
    _SENTINEL_DESCRIPTION,
    _SENTINEL_APPLICATION_NOTE,
    _SENTINEL_REVIEW_NOTE,
    _SENTINEL_REMINDER_MESSAGE,
    _SENTINEL_SAVED_SEARCH_DESCRIPTION,
    _SENTINEL_PROFILE_PATH,
    _SENTINEL_COLLECTION_ERROR,
    _SENTINEL_DIRECTORY,
)

_APPLICATION_STATUS_ORDER = (
    "interested",
    "applied",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
)

_SOURCE_ORDER = (
    "arbeitnow",
    "remoteok",
    "weworkremotely",
    "jobicy",
    "remotive",
    "workingnomads",
    "himalayas",
)

# A profile matching the seeded jobs so scores and ordering are deterministic.
_MAIN_PROFILE = {
    "desired_titles": ["Engineer"],
    "skills": ["Python"],
    "preferred_locations": ["Berlin"],
    "remote_preference": "remote",
}

# A deliberately distinctive profile whose terms must never echo in a response.
_PRIVATE_PROFILE = {
    "desired_titles": [_SENTINEL_PROFILE_TITLE],
    "skills": [_SENTINEL_PROFILE_SKILL],
}


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
    company: str = "Acme GmbH",
    location: str = "Berlin",
    remote_status: str = "remote",
    description: str | None = None,
    employment_type: str = "Full-time",
    published_at: str = "2026-07-29T10:00:00Z",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        remote_status=remote_status,
        description=description or _SENTINEL_DESCRIPTION,
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type=employment_type,
        published_at=published_at,
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _seed_jobs(connection: sqlite3.Connection) -> dict[str, int]:
    jobs = [
        _make_job(source="arbeitnow", source_job_id="a-1", title="Python Engineer"),
        _make_job(source="remoteok", source_job_id="r-1", title="Rust Engineer"),
        _make_job(
            source="weworkremotely",
            source_job_id="w-1",
            title="Python Engineer",
            company="Acme Inc",
        ),
        _make_job(source="jobicy", source_job_id="j-1", title="Frontend Engineer"),
    ]
    save_jobs(connection, jobs)
    return {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }


def _track(
    connection: sqlite3.Connection,
    job_ids: dict[str, int],
    state: dict[str, str],
    *,
    now: datetime = _EPOCH,
) -> None:
    for index, (key, status) in enumerate(state.items()):
        set_application_status(
            connection,
            job_id=job_ids[key],
            status=status,
            now=now + timedelta(minutes=index),
        )


def _seed_decision(
    connection: sqlite3.Connection,
    job_id: int,
    decision: str,
    *,
    note: str | None = None,
) -> None:
    set_decision(
        connection,
        job_id=job_id,
        decision=decision,
        note=note,
        now=_EPOCH,
    )


def _seed_reminders(
    connection: sqlite3.Connection,
    job_ids: dict[str, int],
) -> None:
    now = datetime.now(UTC)
    add_reminder(
        connection,
        job_id=job_ids["a-1"],
        kind="follow_up",
        due_at=(now - timedelta(days=2)).isoformat(),
        message=_SENTINEL_REMINDER_MESSAGE,
    )
    add_reminder(
        connection,
        job_id=job_ids["r-1"],
        kind="follow_up",
        due_at=now.isoformat(),
        message=_SENTINEL_REMINDER_MESSAGE,
    )
    add_reminder(
        connection,
        job_id=job_ids["w-1"],
        kind="custom",
        due_at=(now + timedelta(days=2)).isoformat(),
        message=_SENTINEL_REMINDER_MESSAGE,
    )


def _seed_search(connection: sqlite3.Connection) -> None:
    create_search(
        connection,
        fields=SavedSearchFields(
            name="python-engineer",
            query="python",
            mode="rank",
            description=_SENTINEL_SAVED_SEARCH_DESCRIPTION,
            profile_path=_SENTINEL_PROFILE_PATH,
            grouped=False,
            source=None,
            remote="remote",
            location="Berlin",
            untracked=False,
            min_score=None,
            result_limit=10,
        ),
    )


def _seed_run(
    connection: sqlite3.Connection,
    source: str,
    started_at: datetime,
    *,
    succeeded: bool = True,
    error: str | None = None,
) -> None:
    run_id = insert_run(connection, source=source, started_at=started_at)
    complete_run(
        connection,
        run_id=run_id,
        finished_at=started_at + timedelta(seconds=5),
        succeeded=succeeded,
        fetched=4,
        inserted=3,
        updated=1,
        error=error,
    )


def _seed_initialized_database(path: Path) -> None:
    with connect(path) as connection:
        initialize_schema(connection)
        job_ids = _seed_jobs(connection)
        _track(
            connection,
            job_ids,
            {
                "a-1": "interview",
                "r-1": "offer",
                "w-1": "interested",
                "j-1": "applied",
            },
        )
        _seed_decision(
            connection,
            job_ids["a-1"],
            "shortlist",
            note=_SENTINEL_REVIEW_NOTE,
        )
        _seed_decision(connection, job_ids["r-1"], "dismiss")
        _seed_reminders(connection, job_ids)
        _seed_search(connection)
        _seed_run(
            connection,
            "jobicy",
            datetime.now(UTC) - timedelta(minutes=30),
        )
        _seed_run(
            connection,
            "arbeitnow",
            _EPOCH - timedelta(days=2),
            succeeded=False,
            error=_SENTINEL_COLLECTION_ERROR,
        )
        insert_run(
            connection,
            source="himalayas",
            started_at=datetime.now(UTC) - timedelta(hours=2),
        )
        ensure_grouped(connection)


def _seed_profile_database(path: Path) -> None:
    """Seed six singletons with deterministic scores against ``_MAIN_PROFILE``.

    Against ``_MAIN_PROFILE`` the raw scores are: ``a-1`` (Senior Engineer) 53,
    ``k-1`` (Backend Engineer) 53, ``u-1`` (Backend Engineer, reviewed-dismiss)
    53, ``t-1`` (Backend Engineer, tracked-applied) 53, ``j-1`` (Frontend
    Developer) 25, ``r-1`` (Data Scientist) 8. ``t-1`` is tracked and ``u-1`` is
    reviewed; all other jobs are untracked and unreviewed.
    """
    with connect(path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-1",
                    title="Senior Engineer",
                    company="Acme Alpha",
                    published_at="2026-08-01T10:00:00Z",
                    description="Python backend development.",
                ),
                _make_job(
                    source="remoteok",
                    source_job_id="k-1",
                    title="Backend Engineer",
                    company="Acme Beta",
                    published_at="2026-07-01T10:00:00Z",
                    description="Python and SQL services.",
                ),
                _make_job(
                    source="jobicy",
                    source_job_id="j-1",
                    title="Frontend Developer",
                    company="Acme Gamma",
                    published_at="2026-08-02T10:00:00Z",
                    description="Build user interfaces.",
                ),
                _make_job(
                    source="remotive",
                    source_job_id="r-1",
                    title="Data Scientist",
                    company="Acme Delta",
                    location="Munich",
                    remote_status="onsite",
                    employment_type="Contract",
                    published_at="2026-07-30T10:00:00Z",
                    description="Python analysis.",
                ),
                _make_job(
                    source="weworkremotely",
                    source_job_id="u-1",
                    title="Backend Engineer",
                    company="Acme Epsilon",
                    published_at="2026-07-15T10:00:00Z",
                    description="Python services.",
                ),
                _make_job(
                    source="workingnomads",
                    source_job_id="t-1",
                    title="Backend Engineer",
                    company="Acme Zeta",
                    published_at="2026-06-01T10:00:00Z",
                    description="Python services.",
                ),
            ],
        )
        job_ids = {
            row["source_job_id"]: row["id"]
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        _track(connection, job_ids, {"t-1": "applied"})
        _seed_decision(
            connection,
            job_ids["u-1"],
            "dismiss",
            note=_SENTINEL_REVIEW_NOTE,
        )
        ensure_grouped(connection)


def _seed_duplicate_database(path: Path) -> None:
    """Seed one genuine cross-source duplicate pair plus one singleton.

    ``d-1`` (arbeitnow, "Acme GmbH") and ``d-2`` (remoteok, "Acme Inc")
    normalize to the same company and title/location/remote, so they collapse
    into one canonical group; ``s-1`` stays a singleton. All three score 53
    against ``_MAIN_PROFILE`` and are untracked and unreviewed.
    """
    with connect(path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(
                    source="arbeitnow",
                    source_job_id="d-1",
                    title="Backend Engineer",
                    company="Acme GmbH",
                    published_at="2026-08-01T10:00:00Z",
                    description="Python services.",
                ),
                _make_job(
                    source="remoteok",
                    source_job_id="d-2",
                    title="Backend Engineer",
                    company="Acme Inc",
                    published_at="2026-07-20T10:00:00Z",
                    description="Python & SQL services.",
                ),
                _make_job(
                    source="jobicy",
                    source_job_id="s-1",
                    title="Data Engineer",
                    company="Other Co",
                    published_at="2026-08-02T10:00:00Z",
                    description="Python pipelines.",
                ),
            ],
        )
        ensure_grouped(connection)


def _seed_malformed_timestamp_database(path: Path) -> None:
    with connect(path) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', 'not-a-date', 'not-a-date', 1)
            """
        )
        connection.commit()


def _collect_keys(node: object, needle: str) -> bool:
    """Return True if ``needle`` is any key in the JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == needle or _collect_keys(value, needle):
                return True
    elif isinstance(node, list):
        return any(_collect_keys(item, needle) for item in node)
    return False


def _normalize_as_of(body: dict[str, object]) -> dict[str, object]:
    body = dict(body)
    body["as_of"] = "same"
    return body


def _post_report_json(
    client: TestClient,
    body: object,
    **params: object,
) -> dict[str, object]:
    response = client.post("/report", json=body, params=params)
    assert response.status_code == 200
    return response.json()


def _post_report_nobody(
    client: TestClient,
    **params: object,
) -> dict[str, object]:
    response = client.post("/report", params=params)
    assert response.status_code == 200
    return response.json()


# --------------------------------------------------------------------------- #
# A. Profile-less POST is byte-identical to GET /report
# --------------------------------------------------------------------------- #


def test_post_report_without_body_matches_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        get_body = client.get("/report").json()
        post_body = _post_report_nobody(client)

    assert _normalize_as_of(post_body) == _normalize_as_of(get_body)
    assert post_body["review_queue"]["source"] == "none"
    assert post_body["recommendations"]["source"] == "none"


def test_post_report_empty_object_matches_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        get_body = client.get("/report").json()
        post_body = _post_report_json(client, {})

    assert _normalize_as_of(post_body) == _normalize_as_of(get_body)


def test_post_report_null_profile_matches_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        get_body = client.get("/report").json()
        post_body = _post_report_json(client, {"profile": None})

    assert _normalize_as_of(post_body) == _normalize_as_of(get_body)


def test_get_report_unchanged_alongside_post_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    assert body["overview"]["total_jobs"] == 4
    assert body["overview"]["canonical_groups"] == 1
    assert body["overview"]["distinct_opportunities"] == 3
    assert body["review_queue"]["source"] == "none"
    assert body["review_queue"]["items"] == []
    assert body["review_queue"]["note"] == _REVIEW_QUEUE_NOTE
    assert body["recommendations"]["source"] == "none"
    assert body["recommendations"]["ranked_jobs"] == []
    assert body["recommendations"]["note"] == _RECOMMENDATIONS_NOTE


# --------------------------------------------------------------------------- #
# B. Personalized review queue and recommendations with a profile
# --------------------------------------------------------------------------- #


def test_post_report_profile_populates_review_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    queue = body["review_queue"]
    assert queue["source"] == "profile"
    assert queue["candidate_count"] == 4
    assert [
        item["group"]["representative"]["source_job_id"] for item in queue["items"]
    ] == [
        "a-1",
        "k-1",
        "j-1",
        "r-1",
    ]
    assert [item["match_score"] for item in queue["items"]] == [53, 53, 25, 8]


def test_post_report_profile_populates_recommendations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    recommendations = body["recommendations"]
    assert recommendations["source"] == "profile"
    assert recommendations["candidate_count"] == 5
    assert [
        item["job"]["source_job_id"] for item in recommendations["ranked_jobs"]
    ] == [
        "a-1",
        "u-1",
        "k-1",
        "j-1",
        "r-1",
    ]
    assert [item["match_score"] for item in recommendations["ranked_jobs"]] == [
        53,
        53,
        53,
        25,
        8,
    ]


def test_post_report_profile_source_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    assert body["review_queue"]["source"] == "profile"
    assert body["review_queue"]["source_name"] is None
    assert body["review_queue"]["note"] is None
    assert body["recommendations"]["source"] == "profile"
    assert body["recommendations"]["source_name"] is None
    assert body["recommendations"]["min_score"] == 0
    assert body["recommendations"]["note"] is None


def test_post_report_profile_candidate_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    # Review queue excludes the reviewed (u-1) and tracked (t-1) jobs.
    assert body["review_queue"]["candidate_count"] == 4
    # Recommendations exclude only the tracked (t-1) job; reviewed jobs remain.
    assert body["recommendations"]["candidate_count"] == 5


def test_post_report_tracked_jobs_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    queue_ids = {
        item["group"]["representative"]["source_job_id"]
        for item in body["review_queue"]["items"]
    }
    assert queue_ids == {"a-1", "k-1", "j-1", "r-1"}
    recommended_ids = {
        item["job"]["source_job_id"] for item in body["recommendations"]["ranked_jobs"]
    }
    assert recommended_ids == {"a-1", "u-1", "k-1", "j-1", "r-1"}
    assert "t-1" not in queue_ids
    assert "t-1" not in recommended_ids


def test_post_report_all_tracked_jobs_empty_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)  # every stored job is tracked
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    assert body["review_queue"]["items"] == []
    assert body["review_queue"]["candidate_count"] == 0
    assert body["recommendations"]["ranked_jobs"] == []
    assert body["recommendations"]["candidate_count"] == 0
    assert body["review_queue"]["source"] == "profile"
    assert body["recommendations"]["source"] == "profile"


# --------------------------------------------------------------------------- #
# C. grouped parameter and duplicate collapse
# --------------------------------------------------------------------------- #


def test_post_report_flat_mode_keeps_duplicates_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_duplicate_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    assert body["review_queue"]["grouped"] is False
    assert body["recommendations"]["grouped"] is False
    assert [
        item["group"]["representative"]["source_job_id"]
        for item in body["review_queue"]["items"]
    ] == [
        "s-1",
        "d-1",
        "d-2",
    ]
    assert [
        item["job"]["source_job_id"] for item in body["recommendations"]["ranked_jobs"]
    ] == [
        "s-1",
        "d-1",
        "d-2",
    ]
    assert body["recommendations"]["ranked_groups"] == []
    assert body["recommendations"]["candidate_count"] == 3


def test_post_report_grouped_mode_collapses_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_duplicate_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE}, grouped="true")

    assert body["review_queue"]["grouped"] is True
    assert body["recommendations"]["grouped"] is True
    assert body["recommendations"]["ranked_jobs"] == []
    ranked_groups = body["recommendations"]["ranked_groups"]
    assert len(ranked_groups) == 2
    assert [
        group["group"]["representative"]["source_job_id"] for group in ranked_groups
    ] == ["s-1", "d-1"]
    duplicate_group = ranked_groups[1]["group"]
    assert [member["source_job_id"] for member in duplicate_group["members"]] == [
        "d-1",
        "d-2",
    ]
    assert duplicate_group["sources"] == ["arbeitnow", "remoteok"]
    # The duplicate pair contributes exactly once to the candidate count.
    assert body["recommendations"]["candidate_count"] == 2
    assert [
        item["group"]["representative"]["source_job_id"]
        for item in body["review_queue"]["items"]
    ] == ["s-1", "d-1"]
    assert body["review_queue"]["candidate_count"] == 2


@pytest.mark.parametrize("grouped", [False, True])
def test_post_report_grouped_flag_echoed(
    grouped: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(
            client, {"profile": _MAIN_PROFILE}, grouped=str(grouped).lower()
        )

    assert body["review_queue"]["grouped"] is grouped
    assert body["recommendations"]["grouped"] is grouped


# --------------------------------------------------------------------------- #
# D. Profile validation and unknown-field tolerance
# --------------------------------------------------------------------------- #


def test_post_report_empty_profile_valid_and_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": {}})

    # An empty profile is valid: every untracked candidate scores neutral 0.
    assert body["review_queue"]["source"] == "profile"
    assert body["review_queue"]["candidate_count"] == 4
    assert len(body["review_queue"]["items"]) == 4
    assert all(item["match_score"] == 0 for item in body["review_queue"]["items"])
    assert body["recommendations"]["source"] == "profile"
    assert body["recommendations"]["candidate_count"] == 5
    assert len(body["recommendations"]["ranked_jobs"]) == 5
    assert all(
        item["match_score"] == 0 for item in body["recommendations"]["ranked_jobs"]
    )
    assert body["overview"]["total_jobs"] == 6


def test_post_report_profile_validation_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    oversized_entries = [f"entry-{index}" for index in range(101)]
    invalid_bodies = [
        {"profile": {"skills": ["   "]}},  # empty after strip
        {"profile": {"desired_titles": ["x" * 101]}},  # entry too long
        {"profile": {"remote_preference": "r" * 51}},  # remote preference too long
        {"profile": {"skills": oversized_entries}},  # too many entries
        {"profile": {"skills": "not-a-list"}},  # wrong type
    ]
    with TestClient(app) as client:
        for body in invalid_bodies:
            response = client.post("/report", json=body)
            assert response.status_code == 422, body


def test_post_report_unknown_body_fields_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        clean = _post_report_json(client, {"profile": _MAIN_PROFILE})
        noisy = _post_report_json(
            client,
            {
                "profile": _MAIN_PROFILE,
                "profile_path": _SENTINEL_PROFILE_PATH,
                "description": _SENTINEL_SAVED_SEARCH_DESCRIPTION,
                "unknown_future_field": 42,
            },
        )

    assert _normalize_as_of(noisy) == _normalize_as_of(clean)


def test_post_report_profile_path_probe_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        get_body = client.get("/report").json()
        response = client.post("/report", json={"profile_path": _SENTINEL_PROFILE_PATH})

    assert response.status_code == 200
    assert _normalize_as_of(response.json()) == _normalize_as_of(get_body)
    assert _SENTINEL_PROFILE_PATH not in response.text


# --------------------------------------------------------------------------- #
# E. limit parameter
# --------------------------------------------------------------------------- #


def test_post_report_limit_defaults_to_ten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        default = _post_report_json(client, {"profile": _MAIN_PROFILE})
        explicit = _post_report_json(client, {"profile": _MAIN_PROFILE}, limit=10)

    assert _normalize_as_of(explicit) == _normalize_as_of(default)


def test_post_report_limit_caps_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE}, limit=1)

    assert len(body["application_pipeline"]["recently_changed"]) == 1
    assert len(body["review_queue"]["items"]) == 1
    assert body["review_queue"]["candidate_count"] == 4  # limit only trims
    assert len(body["recommendations"]["ranked_jobs"]) == 1
    assert len(body["reminders"]["upcoming"]) <= 1
    assert all(
        len(body["action_items"][category]) <= 1
        for category in ("interview", "offer", "interested", "awaiting_progress")
    )


def test_post_report_limit_boundaries_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for limit in (1, 100):
            response = client.post(
                "/report", json={"profile": _MAIN_PROFILE}, params={"limit": limit}
            )
            assert response.status_code == 200, limit


def test_post_report_limit_rejects_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for invalid in (0, -1, 101, 1.5, "abc"):
            response = client.post(
                "/report",
                json={"profile": _MAIN_PROFILE},
                params={"limit": invalid},
            )
            assert response.status_code == 422, invalid


# --------------------------------------------------------------------------- #
# F. Determinism and expected ordering
# --------------------------------------------------------------------------- #


def test_post_report_deterministic_across_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = _post_report_json(client, {"profile": _MAIN_PROFILE})
        second = _post_report_json(client, {"profile": _MAIN_PROFILE})

    assert first != second  # as_of advances
    datetime.fromisoformat(first["as_of"])
    datetime.fromisoformat(second["as_of"])
    assert _normalize_as_of(first) == _normalize_as_of(second)


def test_post_report_review_queue_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    # match_score DESC, then publication time DESC, then job ID ASC.
    assert [
        item["group"]["representative"]["source_job_id"]
        for item in body["review_queue"]["items"]
    ] == [
        "a-1",
        "k-1",
        "j-1",
        "r-1",
    ]


def test_post_report_recommendations_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = _post_report_json(client, {"profile": _MAIN_PROFILE})

    # match_score DESC, then publication time DESC, then job ID ASC.
    assert [
        item["job"]["source_job_id"] for item in body["recommendations"]["ranked_jobs"]
    ] == [
        "a-1",
        "u-1",
        "k-1",
        "j-1",
        "r-1",
    ]


# --------------------------------------------------------------------------- #
# G. Missing, uninitialized, and malformed databases
# --------------------------------------------------------------------------- #


def test_post_report_missing_database_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / _SENTINEL_DIRECTORY / "careeros.sqlite3"
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        response = client.post("/report", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    body = response.json()
    assert body["overview"]["total_jobs"] == 0
    assert body["overview"]["canonical_groups"] == 0
    assert body["overview"]["grouped_members"] == 0
    assert body["overview"]["distinct_opportunities"] == 0
    assert body["application_pipeline"]["status_counts"] == dict(
        (status, 0) for status in _APPLICATION_STATUS_ORDER
    )
    assert body["review_queue"] == {
        "grouped": False,
        "source": "none",
        "source_name": None,
        "candidate_count": 0,
        "note": _REVIEW_QUEUE_NOTE,
        "items": [],
    }
    assert body["recommendations"] == {
        "grouped": False,
        "source": "none",
        "source_name": None,
        "min_score": 0,
        "candidate_count": 0,
        "note": _RECOMMENDATIONS_NOTE,
        "ranked_jobs": [],
        "ranked_groups": [],
    }
    assert body["collection_health"]["sources"] == []
    assert not database_path.exists()
    assert not database_path.parent.exists()
    assert not Path(str(database_path) + ".lock").exists()


def test_post_report_missing_database_profile_matches_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with TestClient(app) as client:
        get_body = client.get("/report").json()
        post_body = _post_report_json(
            client, {"profile": _MAIN_PROFILE}, grouped="true"
        )

    assert _normalize_as_of(post_body) == _normalize_as_of(get_body)
    assert not database_path.exists()
    assert not Path(str(database_path) + ".lock").exists()


def test_post_report_uninitialized_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    sqlite3.connect(path).close()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/report", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 500
    assert response.json()["detail"] == DATABASE_UNAVAILABLE_MESSAGE


def test_post_report_malformed_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database file at all")
    _set_database(monkeypatch, malformed)

    with TestClient(app) as client:
        response = client.post("/report", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 500
    assert response.json()["detail"] == DATABASE_UNAVAILABLE_MESSAGE


def test_post_report_malformed_timestamp_treated_as_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_malformed_timestamp_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/report", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    by_source = {
        entry["source"]: entry
        for entry in response.json()["collection_health"]["sources"]
    }
    assert by_source["jobicy"]["last_attempt"] is None
    assert by_source["jobicy"]["due"] is True


# --------------------------------------------------------------------------- #
# H. Read-only and locking guarantees
# --------------------------------------------------------------------------- #


def test_post_report_does_not_write_to_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/report", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not Path(str(path) + ".lock").exists()


def test_post_report_does_not_record_run_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)  # exactly 3 seeded runs
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        client.post("/report", json={"profile": _MAIN_PROFILE})

    with connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert count == 3


def test_post_report_works_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="collect due"):
        with TestClient(app) as client:
            response = client.post("/report", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    body = response.json()
    assert body["overview"]["total_jobs"] == 6
    assert len(body["review_queue"]["items"]) == 4


# --------------------------------------------------------------------------- #
# I. Privacy
# --------------------------------------------------------------------------- #


def test_post_report_never_exposes_private_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/report", json={"profile": _MAIN_PROFILE})

    text = response.text
    for sentinel in _ALL_SENTINELS:
        assert sentinel not in text, sentinel

    body = response.json()
    assert not _collect_keys(body, "notification_summary")
    assert not _collect_keys(body, "notifications")
    assert not _collect_keys(body, "last_error")
    assert not _collect_keys(body, "description")
    assert not _collect_keys(body, "profile_path")
    assert not _collect_keys(body, "message")
    assert not _collect_keys(body, "reasons")
    assert not _collect_keys(body, "score_components")
    assert not _collect_keys(body, "feedback")
    assert "Traceback" not in text
    assert "traceback" not in text
    assert _SENTINEL_DIRECTORY not in text


def test_post_report_never_echoes_profile_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/report", json={"profile": _PRIVATE_PROFILE})

    assert response.status_code == 200
    assert _SENTINEL_PROFILE_TITLE not in response.text
    assert _SENTINEL_PROFILE_SKILL not in response.text
    body = response.json()
    # User-filled profile scoring leaves no private text behind; the note
    # fields are null (they only carry text in the profile-less sections).
    assert body["review_queue"]["note"] is None
    assert body["recommendations"]["note"] is None


# --------------------------------------------------------------------------- #
# J. Response field/order contract
# --------------------------------------------------------------------------- #


def test_post_report_field_order_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        flat = _post_report_json(client, {"profile": _MAIN_PROFILE})
        grouped = _post_report_json(client, {"profile": _MAIN_PROFILE}, grouped="true")

    assert list(flat) == [
        "as_of",
        "overview",
        "application_pipeline",
        "action_items",
        "reminders",
        "review_queue",
        "recommendations",
        "collection_health",
    ]
    assert list(flat["review_queue"]) == [
        "grouped",
        "source",
        "source_name",
        "candidate_count",
        "note",
        "items",
    ]
    assert list(flat["recommendations"]) == [
        "grouped",
        "source",
        "source_name",
        "min_score",
        "candidate_count",
        "note",
        "ranked_jobs",
        "ranked_groups",
    ]
    assert list(flat["review_queue"]["items"][0]) == ["group", "match_score"]
    assert list(flat["recommendations"]["ranked_jobs"][0]) == [
        "job",
        "match_score",
        "score",
    ]
    assert list(grouped["recommendations"]["ranked_groups"][0]) == [
        "group",
        "match_score",
        "score",
    ]


# --------------------------------------------------------------------------- #
# K. Invariants and prior-endpoint regressions
# --------------------------------------------------------------------------- #


def test_post_report_schema_version_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert SCHEMA_VERSION == 8
    assert health["schema_version"] == 8


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


def test_real_default_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.settings import DEFAULT_DATABASE_PATH, database_path

    real = DEFAULT_DATABASE_PATH
    before = real.stat().st_mtime_ns if real.is_file() else None

    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        client.post("/report", json={"profile": _MAIN_PROFILE})

    assert database_path() == path
    assert not real.is_file() or real.stat().st_mtime_ns == before


def test_post_ranked_jobs_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/ranked-jobs", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 6
    assert [item["job"]["source_job_id"] for item in body["items"]] == [
        "a-1",
        "u-1",
        "k-1",
        "t-1",
        "j-1",
        "r-1",
    ]


def test_post_review_queue_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_profile_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.post("/review-queue", json={"profile": _MAIN_PROFILE})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 4
    assert [
        item["group"]["representative"]["source_job_id"] for item in body["items"]
    ] == [
        "a-1",
        "k-1",
        "j-1",
        "r-1",
    ]


def test_saved_search_endpoints_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)  # seeds the rank-mode "python-engineer" search
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        listing = client.get("/saved-searches")
        execute = client.post(
            "/saved-searches/python-engineer/execute",
            json={"profile": _MAIN_PROFILE},
        )

    assert listing.status_code == 200
    assert "python-engineer" in [item["name"] for item in listing.json()["items"]]
    assert execute.status_code == 200
    payload = execute.json()
    assert list(payload) == ["mode", "grouped", "items", "total", "offset", "limit"]
    assert payload["mode"] == "rank"
    assert payload["grouped"] is False
    assert payload["total"] >= 1
    assert isinstance(payload["items"][0]["match_score"], int)
    assert isinstance(payload["items"][0]["score"], int)
