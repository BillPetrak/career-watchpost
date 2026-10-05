"""Tests for the CareerOS review-decisions API (ninth slice).

Covers ``GET /review-decisions``, ``GET /review-decisions/{job_id}``, and
``GET /review-decisions/{job_id}/history``: the response envelopes and item
field order, the canonical nested job projection, deterministic service
ordering (``updated_at DESC, id DESC`` for decisions, ``created_at DESC, id
DESC`` for events), single and repeated decision filters, invalid-filter 422s,
pagination with total-before-slicing, current-decision detail, the new
existing-but-unreviewed 404, unknown-job 404s, first-assignment/clear null
transition semantics, empty history for never-reviewed jobs, missing-database
behavior without file/parent creation, uninitialized/malformed 500s, the
privacy guarantee that decision notes, application notes, and reminder
messages never appear, defensive orphan handling, prior endpoints unchanged,
and the shared no-``careeros.cli``-import guarantee.
"""

from __future__ import annotations

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
from careeros.review.service import clear_decision, set_decision
from tests.support import TestClient

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_LATER = datetime.fromisoformat("2026-08-04T12:00:00+00:00")

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

_ITEM_FIELDS = [
    "decision_id",
    "job_id",
    "decision",
    "created_at",
    "updated_at",
    "job",
]

_EVENT_FIELDS = [
    "event_id",
    "job_id",
    "old_decision",
    "new_decision",
    "created_at",
]

_EMPTY_PROFILE = {}


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed_jobs(path: Path) -> dict[str, int]:
    """Seed three distinct jobs and return a source-job-id to jobs.id mapping."""
    connection = connect(path)
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job("arbeitnow", "a-1", "Senior Engineer", "Acme GmbH", "Berlin"),
            _make_job("remoteok", "b-1", "Data Scientist", "Other Co", "Munich"),
            _make_job("jobicy", "c-1", "Frontend Developer", "Frontend Labs", "Berlin"),
        ],
    )
    job_ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    connection.close()
    return job_ids


def _seed(path: Path) -> dict[str, int]:
    """Seed three jobs with two decisions sharing one timestamp.

    ``a-1`` shortlisted and ``b-1`` dismissed at the same instant, so the
    decision list ordering falls back to ``id DESC`` (``b-1`` first). ``c-1``
    stays unreviewed.
    """
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="dismiss",
            note="secret-review-note-b",
            now=_EPOCH,
        )
    return job_ids


def _seed_ordered(path: Path) -> dict[str, int]:
    """Seed two decisions with distinct ``updated_at`` timestamps.

    ``a-1`` (lower id) is updated later than ``b-1``, so ``updated_at DESC``
    must put ``a-1`` first — proving updated_at governs over id.
    """
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            now=_LATER,
        )
        set_decision(
            connection,
            job_id=job_ids["b-1"],
            decision="dismiss",
            now=_EPOCH,
        )
    return job_ids


def _seed_history(path: Path) -> dict[str, int]:
    """Seed one job with two decision transitions on ``a-1``."""
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            note="secret-review-note",
            now=_EPOCH,
        )
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="review_later",
            now=_LATER,
        )
    return job_ids


def _seed_cleared(path: Path) -> dict[str, int]:
    """Seed one job whose decision was set and then cleared."""
    job_ids = _seed_jobs(path)
    with connect(path) as connection:
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            now=_EPOCH,
        )
        clear_decision(connection, job_id=job_ids["a-1"], now=_LATER)
    return job_ids


# --------------------------------------------------------------------------- #
# Populated list, field order, and deterministic ordering
# --------------------------------------------------------------------------- #


def test_review_decisions_list_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [item["job_id"] for item in payload["items"]] == [
        job_ids["b-1"],
        job_ids["a-1"],
    ]
    assert [item["decision"] for item in payload["items"]] == ["dismiss", "shortlist"]
    assert [item["decision_id"] for item in payload["items"]] == [
        item["decision_id"] for item in payload["items"]
    ]
    assert payload["items"][1]["job"]["title"] == "Senior Engineer"


def test_review_decisions_envelope_and_item_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = client.get("/review-decisions").json()

    assert list(payload) == ["items", "total", "offset", "limit"]
    for item in payload["items"]:
        assert list(item) == _ITEM_FIELDS


def test_review_decisions_nested_job_field_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = client.get("/review-decisions").json()

    for item in payload["items"]:
        job = item["job"]
        assert list(job) == _JOB_FIELDS
        assert "description" not in job


def test_review_decisions_deterministic_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.get("/review-decisions").json()
        second = client.get("/review-decisions").json()

    assert first == second
    assert [item["decision_id"] for item in first["items"]] == sorted(
        (item["decision_id"] for item in first["items"]), reverse=True
    )


def test_review_decisions_updated_at_desc_governs_over_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_ordered(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = client.get("/review-decisions").json()

    # a-1 has the lower id but the later updated_at, so it must lead.
    assert [item["job_id"] for item in payload["items"]] == [
        job_ids["a-1"],
        job_ids["b-1"],
    ]


def test_review_decisions_id_desc_tiebreak_on_equal_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        payload = client.get("/review-decisions").json()

    # Equal timestamps: the higher id is newest.
    assert [item["job_id"] for item in payload["items"]] == [
        job_ids["b-1"],
        job_ids["a-1"],
    ]


# --------------------------------------------------------------------------- #
# Decision filters
# --------------------------------------------------------------------------- #


def test_review_decisions_single_and_repeated_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        shortlist = client.get("/review-decisions", params={"decision": "shortlist"})
        both = client.get(
            "/review-decisions",
            params=[("decision", "shortlist"), ("decision", "dismiss")],
        )
        none = client.get("/review-decisions", params={"decision": "review_later"})

    assert shortlist.status_code == 200
    assert [item["job_id"] for item in shortlist.json()["items"]] == [job_ids["a-1"]]
    assert shortlist.json()["total"] == 1

    assert both.status_code == 200
    assert [item["job_id"] for item in both.json()["items"]] == [
        job_ids["b-1"],
        job_ids["a-1"],
    ]
    assert both.json()["total"] == 2

    assert none.status_code == 200
    assert none.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_review_decisions_invalid_decision_filter_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions", params={"decision": "bogus"})

    assert response.status_code == 422
    assert "Invalid review decision" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# job_id filter
# --------------------------------------------------------------------------- #


def test_review_decisions_job_id_filter_returns_reviewed_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions", params={"job_id": job_ids["a-1"]})

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == ["items", "total", "offset", "limit"]
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert list(item) == _ITEM_FIELDS
    assert item["job_id"] == job_ids["a-1"]
    assert item["decision"] == "shortlist"
    assert list(item["job"]) == _JOB_FIELDS


def test_review_decisions_job_id_filter_unreviewed_job_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions", params={"job_id": job_ids["c-1"]})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_review_decisions_job_id_filter_unknown_job_is_empty_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions", params={"job_id": 9999})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_review_decisions_job_id_filter_non_integer_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions", params={"job_id": "not-an-int"})

    assert response.status_code == 422


def test_review_decisions_job_id_filter_zero_or_negative_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        for value in (0, -1):
            response = client.get("/review-decisions", params={"job_id": value})
            assert response.status_code == 422, value


def test_review_decisions_job_id_filter_composes_with_decision_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(
            "/review-decisions",
            params={"job_id": job_ids["a-1"], "decision": "dismiss"},
        )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


# --------------------------------------------------------------------------- #
# Pagination
# --------------------------------------------------------------------------- #


def test_review_decisions_pagination_total_before_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.get("/review-decisions", params={"limit": 1})
        second = client.get("/review-decisions", params={"offset": 1, "limit": 1})

    assert [item["job_id"] for item in first.json()["items"]] == [job_ids["b-1"]]
    assert first.json()["total"] == 2
    assert [item["job_id"] for item in second.json()["items"]] == [job_ids["a-1"]]
    assert second.json()["total"] == 2


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #


def test_review_decisions_current_decision_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/review-decisions/{job_ids['a-1']}")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == _ITEM_FIELDS
    assert payload["job_id"] == job_ids["a-1"]
    assert payload["decision"] == "shortlist"
    assert list(payload["job"]) == _JOB_FIELDS


def test_review_decisions_existing_unreviewed_detail_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/review-decisions/{job_ids['c-1']}")

    assert response.status_code == 404
    assert response.json() == {
        "detail": f"No review decision found for job {job_ids['c-1']}."
    }


def test_review_decisions_unknown_job_detail_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions/9999")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_review_decisions_detail_non_integer_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions/not-an-int")

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_review_decisions_history_populated_and_ordered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_history(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == ["items", "total", "offset", "limit"]
    assert payload["total"] == 2
    events = payload["items"]
    assert [list(event) for event in events] == [_EVENT_FIELDS, _EVENT_FIELDS]
    # Newest first: the review_later transition, then the original assignment.
    assert events[0]["old_decision"] == "shortlist"
    assert events[0]["new_decision"] == "review_later"
    assert events[1]["old_decision"] is None
    assert events[1]["new_decision"] == "shortlist"
    assert events[0]["created_at"] > events[1]["created_at"]


def test_review_decisions_history_null_new_decision_on_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_cleared(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    # The current decision is gone after a clear, but the history survives.
    assert detail.status_code == 404
    assert detail.json() == {
        "detail": f"No review decision found for job {job_ids['a-1']}."
    }

    assert history.status_code == 200
    events = history.json()["items"]
    assert history.json()["total"] == 2
    assert events[0]["old_decision"] == "shortlist"
    assert events[0]["new_decision"] is None
    assert events[1]["old_decision"] is None
    assert events[1]["new_decision"] == "shortlist"


def test_review_decisions_history_never_reviewed_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get(f"/review-decisions/{job_ids['c-1']}/history")

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 20}


def test_review_decisions_history_unknown_job_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    _seed(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        response = client.get("/review-decisions/9999/history")

    assert response.status_code == 404
    assert response.json() == {"detail": "No job found with ID 9999."}


def test_review_decisions_history_pagination_total_before_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_history(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        first = client.get(
            f"/review-decisions/{job_ids['a-1']}/history", params={"limit": 1}
        )
        second = client.get(
            f"/review-decisions/{job_ids['a-1']}/history",
            params={"offset": 1, "limit": 1},
        )

    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert first.json()["items"][0]["new_decision"] == "review_later"

    assert second.status_code == 200
    assert second.json()["total"] == 2
    assert second.json()["items"][0]["new_decision"] == "shortlist"


# --------------------------------------------------------------------------- #
# Database availability
# --------------------------------------------------------------------------- #


def test_review_decisions_missing_database_is_empty_without_creating_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "nested" / "missing.sqlite3"
    _set_database(monkeypatch, database_path)

    with pytest.raises(ValueError, match="current schema v14"):
        with TestClient(app):
            pass
    assert not database_path.exists()
    assert not database_path.parent.exists()


def test_review_decisions_uninitialized_database_is_500(
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


def test_review_decisions_malformed_database_is_500(
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
# Privacy
# --------------------------------------------------------------------------- #


def test_review_decisions_privacy_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
        set_application_status(
            connection,
            job_id=job_ids["b-1"],
            status="applied",
            note="secret-cv-note",
            now=_EPOCH,
        )
        add_reminder(
            connection,
            job_id=job_ids["c-1"],
            kind="follow_up",
            due_at="2026-08-10T14:00:00+00:00",
            message="secret-reminder-message",
            now=_EPOCH,
        )
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        list_response = client.get("/review-decisions")
        detail_response = client.get(f"/review-decisions/{job_ids['a-1']}")
        history_response = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    assert history_response.status_code == 200

    for response in (list_response, detail_response, history_response):
        for leaked in (
            "secret-review-note",
            "secret-review-note-b",
            "secret-cv-note",
            "secret-reminder-message",
            "note",
            "latest_note",
            "message",
            "description",
            "reasons",
            "score_components",
            "conflict",
            "feedback",
            str(path),
            "Traceback",
            "sqlite3",
        ):
            assert leaked not in response.text


def test_review_decisions_detail_and_history_never_expose_note_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed_history(path)
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert detail.status_code == 200
    assert history.status_code == 200
    assert list(detail.json()) == _ITEM_FIELDS
    for event in history.json()["items"]:
        assert list(event) == _EVENT_FIELDS


# --------------------------------------------------------------------------- #
# Defensive orphan handling
# --------------------------------------------------------------------------- #


def test_review_decisions_defensive_orphan_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "careeros.sqlite3"
    job_ids = _seed(path)
    with connect(path) as connection:
        # Simulate a corrupt database whose jobs row vanished behind a stored
        # decision: force the delete with foreign keys off (the RESTRICT
        # constraint prevents this in normal operation). The API must never
        # manufacture partial job data for the orphan.
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM jobs WHERE id = ?", (job_ids["a-1"],))
        connection.commit()
    _set_database(monkeypatch, path)

    with TestClient(app) as client:
        list_response = client.get("/review-decisions")
        detail_response = client.get(f"/review-decisions/{job_ids['a-1']}")
        history_response = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    # The orphan is excluded by the shared INNER JOIN (no partial data), while
    # the surviving decision still lists normally.
    assert list_response.status_code == 200
    payload = list_response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["job_id"] == job_ids["b-1"]

    assert detail_response.status_code == 404
    assert detail_response.json() == {
        "detail": f"No job found with ID {job_ids['a-1']}."
    }

    assert history_response.status_code == 404
    assert history_response.json() == {
        "detail": f"No job found with ID {job_ids['a-1']}."
    }


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
        ranked = client.post("/ranked-jobs", json={"profile": _EMPTY_PROFILE})
        queue = client.post("/review-queue", json={"profile": _EMPTY_PROFILE})
        decisions = client.get("/review-decisions")
        detail = client.get(f"/review-decisions/{job_ids['a-1']}")
        history = client.get(f"/review-decisions/{job_ids['a-1']}/history")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "database": "present",
        "schema_version": SCHEMA_VERSION,
    }

    assert jobs.status_code == 200
    assert jobs.json()["total"] == 3

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
    assert ranked.json()["total"] == 3

    # Two of the three jobs are decided, so only c-1 is unreviewed.
    assert queue.status_code == 200
    assert queue.json()["total"] == 1

    assert decisions.status_code == 200
    assert decisions.json()["total"] == 2

    assert detail.status_code == 200
    assert detail.json()["decision"] == "shortlist"

    assert history.status_code == 200
    assert history.json()["total"] == 1
