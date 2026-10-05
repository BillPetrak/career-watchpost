"""Tests for the CareerOS profile-less report API (``GET /report``).

Covers the Career Snapshot dashboard endpoint: the all-zero/empty report for a
missing database (without creating a file, parent directory, or lock file), the
seeded report's overview counters, application pipeline, action items,
reminders, collection health, and source statistics, the profile-less
review-queue and recommendations sections (structurally present with
``source="none"``, empty items, zero counts, and the canonical note), ``limit``
semantics (default 10, range 1..100, list caps), absolute privacy (job
descriptions, application/review notes, reminder messages, provider
``last_error`` text, ``notification_summary``, scoring reasons/components,
saved-search ``description``/``profile_path``, and paths are never exposed),
deterministic repeated responses (identical except ``as_of``), strict
read-only behavior (no database writes, no run-history recording, no lock
file, works while the collection lock is held, no schema initialization),
missing/uninitialized/malformed database handling (missing -> 200 empty
report; uninitialized/malformed -> canonical privacy-safe 500), the
schema-version invariant, prior-endpoint regression, the shared no-
``careeros.cli`` import guarantee, and the rule that the real
``data/database/careeros.sqlite3`` is never opened or modified.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.api import app
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
from tests.support import TestClient, prepare_today_ranking

_SRC = Path(__file__).resolve().parents[1] / "src"

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

# Distinctive sentinels proving actual non-leakage of private fields: if any
# string ever appears in response text, the API leaked a private value.
_SENTINEL_DESCRIPTION = "PRIVATE-JOB-DESCRIPTION-9f41c7d2"
_SENTINEL_APPLICATION_NOTE = "private-application-note"
_SENTINEL_REVIEW_NOTE = "private-review-note"
_SENTINEL_REMINDER_MESSAGE = "private-reminder-message"
_SENTINEL_SAVED_SEARCH_DESCRIPTION = "private-saved-search-description"
_SENTINEL_PROFILE_PATH = "/opt/private/secrets/candidate-profile.yaml"
_SENTINEL_COLLECTION_ERROR = "PRIVATE-ERROR: connection reset by peer"
_SENTINEL_DIRECTORY = "private-secret-directory"

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


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
    company: str = "Acme GmbH",
    description: str | None = None,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location="Berlin",
        remote_status="remote",
        description=description or _SENTINEL_DESCRIPTION,
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        published_at="2026-07-29T10:00:00Z",
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


def _seed_due_today_database(path: Path) -> None:
    with connect(path) as connection:
        initialize_schema(connection)
        job_ids = _seed_jobs(connection)
        now = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        add_reminder(
            connection,
            job_id=job_ids["a-1"],
            kind="follow_up",
            due_at=start_of_day.isoformat(),
            message=_SENTINEL_REMINDER_MESSAGE,
        )


def _collect_keys(node: object, needle: str) -> bool:
    """Return True if ``needle`` is any key in the JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == needle or _collect_keys(value, needle):
                return True
    elif isinstance(node, list):
        return any(_collect_keys(item, needle) for item in node)
    return False


# --------------------------------------------------------------------------- #
# A. Missing and empty databases
# --------------------------------------------------------------------------- #


def test_report_missing_database_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / _SENTINEL_DIRECTORY / "careeros.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()
    assert not Path(str(database_path) + ".lock").exists()


def test_report_missing_database_never_creates_file_or_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not Path(str(database_path) + ".lock").exists()


def test_report_empty_initialized_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/report")

    assert response.status_code == 200
    body = response.json()
    assert body["overview"]["total_jobs"] == 0
    assert [entry["source"] for entry in body["overview"]["sources"]] == list(
        _SOURCE_ORDER
    )
    assert body["overview"]["due_sources"] == list(_SOURCE_ORDER)
    assert [entry["source"] for entry in body["overview"]["source_stats"]] == list(
        _SOURCE_ORDER
    )
    assert [entry["source"] for entry in body["collection_health"]["sources"]] == list(
        _SOURCE_ORDER
    )


# --------------------------------------------------------------------------- #
# B. Seeded report sections
# --------------------------------------------------------------------------- #


def test_report_overview_counts_and_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    assert body["overview"]["total_jobs"] == 4
    assert body["overview"]["canonical_groups"] == 1
    assert body["overview"]["grouped_members"] == 2
    assert body["overview"]["distinct_opportunities"] == 3
    assert body["overview"]["tracked_applications"] == 4
    assert body["overview"]["saved_searches"] == 1
    assert body["overview"]["reviewed_groups"] == 2
    assert body["overview"]["unreviewed_groups"] == 1
    assert body["overview"]["shortlisted_groups"] == 1
    assert body["overview"]["dismissed_groups"] == 1
    assert body["overview"]["review_later_groups"] == 0


def test_report_application_pipeline_and_action_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    pipeline = body["application_pipeline"]
    assert list(pipeline["status_counts"]) == list(_APPLICATION_STATUS_ORDER)
    assert pipeline["status_counts"]["interview"] == 1
    assert pipeline["status_counts"]["offer"] == 1
    assert pipeline["status_counts"]["interested"] == 1
    assert pipeline["status_counts"]["applied"] == 1
    assert pipeline["status_counts"]["rejected"] == 0
    assert pipeline["status_counts"]["withdrawn"] == 0
    # Newest first (later tracking `now` values sort first).
    assert [item["job"]["source_job_id"] for item in pipeline["recently_changed"]] == [
        "j-1",
        "w-1",
        "r-1",
        "a-1",
    ]
    for item in pipeline["recently_changed"]:
        assert "description" not in item["job"]
        assert "note" not in item

    actions = body["action_items"]
    assert [item["job"]["source_job_id"] for item in actions["interview"]] == ["a-1"]
    assert [item["job"]["source_job_id"] for item in actions["offer"]] == ["r-1"]
    assert [item["job"]["source_job_id"] for item in actions["interested"]] == ["w-1"]
    assert [item["job"]["source_job_id"] for item in actions["awaiting_progress"]] == [
        "j-1"
    ]
    assert actions["interview"][0]["label"] == "Interview attention"


def test_report_review_queue_and_recommendations_profile_less(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    review = body["review_queue"]
    assert review["grouped"] is False
    assert review["source"] == "none"
    assert review["source_name"] is None
    assert review["items"] == []
    assert review["candidate_count"] == 0
    assert review["note"] == _REVIEW_QUEUE_NOTE

    recommendations = body["recommendations"]
    assert recommendations["grouped"] is False
    assert recommendations["source"] == "none"
    assert recommendations["source_name"] is None
    assert recommendations["ranked_jobs"] == []
    assert recommendations["ranked_groups"] == []
    assert recommendations["candidate_count"] == 0
    assert recommendations["min_score"] == 0
    assert recommendations["note"] == _RECOMMENDATIONS_NOTE


def test_report_reminders_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    reminders = body["reminders"]
    assert reminders["overdue_count"] == 2  # a-1 (now - 2d) and r-1 (due now)
    assert reminders["due_today_utc_count"] == 1  # r-1 due within today
    assert reminders["open_total"] == 3
    assert [item["job"]["source_job_id"] for item in reminders["upcoming"]] == ["w-1"]
    for item in reminders["upcoming"]:
        assert "message" not in item
        assert item["job"]["source_job_id"] == "w-1"


def test_report_due_today_utc_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_due_today_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    assert body["reminders"]["due_today_utc_count"] == 1
    assert body["reminders"]["open_total"] == 1


def test_report_collection_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    sources = body["collection_health"]["sources"]
    assert [entry["source"] for entry in sources] == list(_SOURCE_ORDER)
    by_source = {entry["source"]: entry for entry in sources}

    jobicy = by_source["jobicy"]
    assert jobicy["min_interval_seconds"] == 3600
    assert jobicy["documented"] is True
    assert jobicy["last_succeeded"] is True
    assert jobicy["fetched"] == 4
    assert jobicy["inserted"] == 3
    assert jobicy["updated"] == 1
    assert jobicy["due"] is False
    assert jobicy["last_attempt"] is not None
    assert jobicy["next_due"] is not None
    assert jobicy["incomplete_runs"] == 0

    arbeitnow = by_source["arbeitnow"]
    assert arbeitnow["last_succeeded"] is False
    assert arbeitnow["due"] is True
    assert "last_error" not in arbeitnow

    himalayas = by_source["himalayas"]
    assert himalayas["last_attempt"] is None
    assert himalayas["last_succeeded"] is None
    assert himalayas["due"] is True
    assert himalayas["incomplete_runs"] == 1

    remoteok = by_source["remoteok"]
    assert remoteok["last_attempt"] is None
    assert remoteok["due"] is True


def test_report_malformed_timestamp_treated_as_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', 'not-a-date', 'not-a-date', 1)
            """
        )
        connection.commit()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    by_source = {
        entry["source"]: entry for entry in body["collection_health"]["sources"]
    }
    assert by_source["jobicy"]["last_attempt"] is None
    assert by_source["jobicy"]["due"] is True


def test_report_source_stats_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    stats = body["overview"]["source_stats"]
    assert [entry["source"] for entry in stats] == list(_SOURCE_ORDER)
    arbeitnow = next(entry for entry in stats if entry["source"] == "arbeitnow")
    assert arbeitnow["total_jobs"] == 1
    assert arbeitnow["never_groupable_jobs"] == 0
    assert len(arbeitnow["fields"]) == 8
    assert {entry["field"] for entry in arbeitnow["fields"]} == {
        "source_job_id",
        "company",
        "location",
        "description",
        "url",
        "employment_type",
        "remote_status",
        "published_at",
    }


def test_report_field_order_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        body = client.get("/report").json()

    assert list(body) == [
        "as_of",
        "overview",
        "application_pipeline",
        "action_items",
        "reminders",
        "review_queue",
        "recommendations",
        "collection_health",
    ]
    assert list(body["overview"]) == [
        "total_jobs",
        "canonical_groups",
        "grouped_members",
        "distinct_opportunities",
        "tracked_applications",
        "saved_searches",
        "reviewed_groups",
        "unreviewed_groups",
        "shortlisted_groups",
        "dismissed_groups",
        "review_later_groups",
        "sources",
        "due_sources",
        "source_stats",
    ]
    source = body["overview"]["sources"][0]
    assert list(source) == [
        "source",
        "min_interval_seconds",
        "documented",
        "last_attempt",
        "last_succeeded",
        "fetched",
        "inserted",
        "updated",
        "next_due",
        "due",
        "incomplete_runs",
    ]


# --------------------------------------------------------------------------- #
# C. limit parameter
# --------------------------------------------------------------------------- #


def test_report_limit_default_and_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        default = client.get("/report").json()
        capped = client.get("/report", params={"limit": 1}).json()

    # Default limit is 10 and the seeded lists are shorter, so nothing is cut.
    assert len(default["application_pipeline"]["recently_changed"]) == 4
    assert len(default["action_items"]["interview"]) == 1
    assert len(default["reminders"]["upcoming"]) == 1

    # limit=1 caps every list-oriented section to one entry.
    assert len(capped["application_pipeline"]["recently_changed"]) == 1
    assert len(capped["action_items"]["interview"]) == 1
    assert len(capped["action_items"]["offer"]) == 1
    assert len(capped["action_items"]["interested"]) == 1
    assert len(capped["action_items"]["awaiting_progress"]) == 1
    assert len(capped["reminders"]["upcoming"]) == 1


def test_report_limit_accepts_upper_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/report", params={"limit": 100})

    assert response.status_code == 200


def test_report_limit_rejects_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for invalid in (0, -1, 101, 1.5, "abc", "1.5"):
            response = client.get("/report", params={"limit": invalid})
            assert response.status_code == 422, invalid


# --------------------------------------------------------------------------- #
# D. Privacy
# --------------------------------------------------------------------------- #


def test_report_never_exposes_private_sentinels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/report")

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
    # The only `note` values are the canonical profile-less section notes.
    assert body["review_queue"]["note"] == _REVIEW_QUEUE_NOTE
    assert body["recommendations"]["note"] == _RECOMMENDATIONS_NOTE
    assert not _collect_keys(
        {k: v for k, v in body.items() if k not in ("review_queue", "recommendations")},
        "note",
    )


def test_report_missing_database_does_not_leak_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / _SENTINEL_DIRECTORY / "careeros.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


# --------------------------------------------------------------------------- #
# E. Determinism and read-only guarantees
# --------------------------------------------------------------------------- #


def test_report_deterministic_across_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.get("/report").json()
        second = client.get("/report").json()

    assert first != second  # as_of advances
    assert first["as_of"] != second["as_of"]
    datetime.fromisoformat(first["as_of"])
    datetime.fromisoformat(second["as_of"])
    first["as_of"] = second["as_of"] = "normalized"
    assert first == second


def test_report_as_of_is_utc_iso8601(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        as_of = client.get("/report").json()["as_of"]

    parsed = datetime.fromisoformat(as_of)
    assert parsed.utcoffset() == timedelta(0)


def test_report_does_not_write_to_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    with connect(path) as connection:
        prepare_today_ranking(connection)
    before = path.read_bytes()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/report")

    assert response.status_code == 200
    assert path.read_bytes() == before
    assert not Path(str(path) + ".lock").exists()


def test_report_does_not_record_run_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        client.get("/report")

    with connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert count == 3  # jobicy success + arbeitnow failure + himalayas incomplete


def test_report_works_while_collection_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with collection_lock(path, command="collect due"):
        with TestClient(app) as client:
            response = client.get("/report")

    assert response.status_code == 200
    assert response.json()["overview"]["total_jobs"] == 4


def test_report_does_not_initialize_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    _set_database(monkeypatch, empty)

    before = empty.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert empty.read_bytes() == before
    with connect(empty) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'jobs'"
        ).fetchone()
    assert row is None


def test_report_malformed_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = tmp_path / "malformed.sqlite3"
    malformed.write_bytes(b"this is not a sqlite database file at all")
    _set_database(monkeypatch, malformed)

    before = malformed.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert malformed.read_bytes() == before


def test_report_uninitialized_database_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    sqlite3.connect(path).close()
    _set_database(monkeypatch, path)

    before = path.read_bytes()
    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert path.read_bytes() == before


def test_report_schema_version_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        health = client.get("/health")

    assert health.json()["schema_version"] == SCHEMA_VERSION == 14


# --------------------------------------------------------------------------- #
# F. Prior endpoints and invariants
# --------------------------------------------------------------------------- #


def test_prior_endpoints_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)

    references = {
        "/health": 200,
        "/jobs": 200,
        "/sources/stats": 200,
        "/collection-runs": 200,
        "/applications": 200,
        "/review-decisions": 200,
        "/reminders": 200,
        "/groups": 200,
        "/groups/summary": 200,
        "/saved-searches": 200,
        "/report": 200,
    }
    with TestClient(app) as client:
        for endpoint, expected in references.items():
            response = client.get(endpoint)
            assert response.status_code == expected, endpoint


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
    _seed_initialized_database(path)
    _set_database(monkeypatch, path)
    with TestClient(app) as client:
        client.get("/report")

    assert database_path() == path
    assert not real.is_file() or real.stat().st_mtime_ns == before
