"""Tests for the review-decision service layer."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.review.models import DECISION_VALUES, ValidationError, validate_decision
from careeros.review.service import (
    UnknownJobError,
    clear_decision,
    decisions_for_jobs,
    get_decision,
    list_decision_events,
    list_decisions,
    set_decision,
)

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str = "Python Engineer",
    location: str = "Berlin",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Tech Corp",
        location=location,
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed_jobs(connection, *, count: int = 2) -> dict[str, int]:
    jobs = [
        _make_job(source="arbeitnow", source_job_id="a-1"),
        _make_job(source="remoteok", source_job_id="r-1", title="Frontend Developer"),
    ][:count]
    save_jobs(connection, jobs)
    return {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }


def _open(tmp_path: Path) -> sqlite3.Connection:
    database_path = tmp_path / "careeros.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    return connection


def test_decision_vocabulary() -> None:
    assert DECISION_VALUES == ("shortlist", "dismiss", "review_later")
    assert validate_decision("shortlist") == "shortlist"


def test_validate_decision_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        validate_decision("apply")


def test_first_assignment_creates_decision_and_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        result = set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            note="top pick",
            now=_EPOCH,
        )
        assert result.created is True
        assert result.changed is True

        stored = get_decision(connection, job_id=job_ids["a-1"])
        assert stored is not None
        assert stored.decision == "shortlist"
        assert stored.created_at == _EPOCH.isoformat()
        assert stored.updated_at == _EPOCH.isoformat()

        events = list_decision_events(connection, job_id=job_ids["a-1"])
        assert len(events) == 1
        assert events[0].old_decision is None
        assert events[0].new_decision == "shortlist"
        assert events[0].note == "top pick"


def test_valid_transition_updates_decision_and_appends_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        result = set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="dismiss",
            now=_EPOCH + timedelta(hours=1),
        )
        assert result.created is False
        assert result.changed is True

        stored = get_decision(connection, job_id=job_ids["a-1"])
        assert stored is not None
        assert stored.decision == "dismiss"
        assert stored.id == 1

        events = list_decision_events(connection, job_id=job_ids["a-1"])
        assert [event.new_decision for event in events] == ["dismiss", "shortlist"]
        assert events[0].old_decision == "shortlist"
        assert events[0].new_decision == "dismiss"


def test_same_decision_without_note_is_idempotent(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        before = connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]

        result = set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            now=_EPOCH + timedelta(hours=1),
        )
        assert result.created is False
        assert result.changed is False

        stored = get_decision(connection, job_id=job_ids["a-1"])
        assert stored is not None
        assert stored.decision == "shortlist"
        assert stored.updated_at == _EPOCH.isoformat()

        after = connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]
        assert after == before


def test_same_decision_with_note_appends_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        result = set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="shortlist",
            note="changed my mind",
            now=_EPOCH + timedelta(hours=1),
        )
        assert result.changed is True

        stored = get_decision(connection, job_id=job_ids["a-1"])
        assert stored is not None
        assert stored.decision == "shortlist"

        events = list_decision_events(connection, job_id=job_ids["a-1"])
        assert len(events) == 2
        assert events[0].old_decision == "shortlist"
        assert events[0].new_decision == "shortlist"
        assert events[0].note == "changed my mind"
        assert stored.updated_at == (_EPOCH + timedelta(hours=1)).isoformat()


def test_clear_removes_decision_and_records_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        result = clear_decision(
            connection,
            job_id=job_ids["a-1"],
            now=_EPOCH + timedelta(hours=1),
        )
        assert result.changed is True

        assert get_decision(connection, job_id=job_ids["a-1"]) is None
        events = list_decision_events(connection, job_id=job_ids["a-1"])
        assert events[0].old_decision == "shortlist"
        assert events[0].new_decision is None
        assert events[0].note is None


def test_clear_unreviewed_job_is_idempotent_noop(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        result = clear_decision(connection, job_id=job_ids["a-1"], now=_EPOCH)
        assert result.changed is False
        assert (
            connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 0
        )


def test_unknown_job_raises_on_set(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        with pytest.raises(UnknownJobError):
            set_decision(connection, job_id=9999, decision="shortlist")


def test_unknown_job_raises_on_clear(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        with pytest.raises(UnknownJobError):
            clear_decision(connection, job_id=9999)


def test_invalid_decision_raises(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        with pytest.raises(ValidationError):
            set_decision(connection, job_id=job_ids["a-1"], decision="bogus")


def test_rollback_on_event_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_event(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated event failure")

    monkeypatch.setattr("careeros.review.service.append_event", fail_event)

    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        with pytest.raises(sqlite3.OperationalError):
            set_decision(
                connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
            )
        decisions = connection.execute(
            "SELECT COUNT(*) FROM review_decisions"
        ).fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]

    assert decisions == 0
    assert events == 0


def test_rollback_on_transition_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_event(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated event failure")

    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        monkeypatch.setattr("careeros.review.service.append_event", fail_event)
        with pytest.raises(sqlite3.OperationalError):
            set_decision(
                connection,
                job_id=job_ids["a-1"],
                decision="dismiss",
                now=_EPOCH + timedelta(hours=1),
            )
        stored = get_decision(connection, job_id=job_ids["a-1"])
        events = list_decision_events(connection, job_id=job_ids["a-1"])

    assert stored is not None
    assert stored.decision == "shortlist"
    assert len(events) == 1
    assert events[0].new_decision == "shortlist"


def test_list_decisions_returns_decided_jobs_newest_first(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        set_decision(
            connection,
            job_id=job_ids["r-1"],
            decision="dismiss",
            now=_EPOCH + timedelta(hours=1),
        )
        items = list_decisions(connection)

    assert [item.job.id for item in items] == [job_ids["r-1"], job_ids["a-1"]]
    assert [item.decision.decision for item in items] == ["dismiss", "shortlist"]


def test_list_decisions_tie_break_by_id_desc(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        set_decision(
            connection, job_id=job_ids["r-1"], decision="review_later", now=_EPOCH
        )
        items = list_decisions(connection)

    assert [item.job.id for item in items] == [job_ids["r-1"], job_ids["a-1"]]


def test_list_decisions_filters_by_decision(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        set_decision(connection, job_id=job_ids["r-1"], decision="dismiss", now=_EPOCH)
        items = list_decisions(connection, decisions=("shortlist",))

    assert len(items) == 1
    assert items[0].job.id == job_ids["a-1"]


def test_list_decisions_invalid_filter_raises(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        with pytest.raises(ValidationError):
            list_decisions(connection, decisions=("bogus",))


def test_list_decisions_empty(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        items = list_decisions(connection)

    assert items == []


def test_events_ordered_newest_first(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="dismiss",
            now=_EPOCH + timedelta(hours=1),
        )
        events = list_decision_events(connection, job_id=job_ids["a-1"])

    assert [event.new_decision for event in events] == ["dismiss", "shortlist"]
    assert events[0].id > events[1].id


def test_events_limit(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        set_decision(
            connection,
            job_id=job_ids["a-1"],
            decision="dismiss",
            now=_EPOCH + timedelta(hours=1),
        )
        events = list_decision_events(connection, job_id=job_ids["a-1"], limit=1)

    assert len(events) == 1
    assert events[0].new_decision == "dismiss"


def test_events_for_unreviewed_job_is_empty(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        events = list_decision_events(connection, job_id=job_ids["a-1"])

    assert events == []


def test_decisions_for_jobs_maps_reviewed_subset(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        decisions = decisions_for_jobs(connection, [job_ids["a-1"], job_ids["r-1"]])

    assert decisions == {job_ids["a-1"]: "shortlist"}


def test_decisions_for_jobs_empty_input(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        assert decisions_for_jobs(connection, []) == {}
