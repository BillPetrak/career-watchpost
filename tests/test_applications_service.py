"""Tests for the application-tracking service layer."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros.applications.models import APPLICATION_STATUSES, validate_status
from careeros.applications.service import (
    UnknownJobError,
    application_statuses_for_jobs,
    get_application,
    list_application_events,
    list_applications,
    set_application_status,
)
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Tech Corp",
        location="Berlin",
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed_jobs(connection) -> dict[str, int]:
    jobs = [
        _make_job(source="arbeitnow", source_job_id="a-1", title="Python Developer"),
        _make_job(source="remoteok", source_job_id="r-1", title="Frontend Developer"),
    ]
    save_jobs(connection, jobs)
    ids = {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }
    return ids


def _open(tmp_path: Path) -> sqlite3.Connection:
    database_path = tmp_path / "careeros.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    return connection


def test_status_vocabulary() -> None:
    assert APPLICATION_STATUSES == (
        "interested",
        "applied",
        "interview",
        "offer",
        "rejected",
        "withdrawn",
    )
    assert validate_status("applied") == "applied"


def test_validate_status_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        validate_status("hired")


def test_first_assignment_creates_application_and_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        result = set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="sent CV",
            now=_EPOCH,
        )
        assert result.created is True
        assert result.changed is True

        application = get_application(connection, job_id=job_ids["a-1"])
        assert application is not None
        assert application.current_status == "applied"
        assert application.created_at == _EPOCH.isoformat()
        assert application.updated_at == _EPOCH.isoformat()

        events = list_application_events(connection, application_id=application.id)
        assert len(events) == 1
        assert events[0].old_status is None
        assert events[0].new_status == "applied"
        assert events[0].note == "sent CV"


def test_valid_transition_updates_status_and_appends_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        result = set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="interview",
            now=_EPOCH + timedelta(hours=1),
        )
        assert result.created is False
        assert result.changed is True

        application = get_application(connection, job_id=job_ids["a-1"])
        assert application is not None
        assert application.current_status == "interview"
        assert application.updated_at == (_EPOCH + timedelta(hours=1)).isoformat()

        events = list_application_events(connection, application_id=application.id)
        assert [event.new_status for event in events] == ["interview", "applied"]
        assert events[0].old_status == "applied"
        assert events[0].new_status == "interview"


def test_same_status_without_note_is_idempotent(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        before = connection.execute(
            "SELECT COUNT(*) FROM application_events"
        ).fetchone()[0]

        result = set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH + timedelta(hours=1),
        )
        assert result.created is False
        assert result.changed is False

        application = get_application(connection, job_id=job_ids["a-1"])
        assert application is not None
        assert application.current_status == "applied"
        assert application.updated_at == _EPOCH.isoformat()

        after = connection.execute(
            "SELECT COUNT(*) FROM application_events"
        ).fetchone()[0]
        assert after == before


def test_same_status_with_note_appends_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        result = set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="still waiting",
            now=_EPOCH + timedelta(hours=1),
        )
        assert result.changed is True

        application = get_application(connection, job_id=job_ids["a-1"])
        assert application is not None
        assert application.current_status == "applied"

        events = list_application_events(connection, application_id=application.id)
        assert len(events) == 2
        assert events[0].old_status == "applied"
        assert events[0].new_status == "applied"
        assert events[0].note == "still waiting"
        assert application.updated_at == (_EPOCH + timedelta(hours=1)).isoformat()


def test_rollback_on_event_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_event(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("simulated event failure")

    monkeypatch.setattr("careeros.applications.service.append_event", fail_event)

    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        with pytest.raises(sqlite3.OperationalError):
            set_application_status(
                connection,
                job_id=job_ids["a-1"],
                status="applied",
                now=_EPOCH,
            )
        applications = connection.execute(
            "SELECT COUNT(*) FROM applications"
        ).fetchone()[0]
        events = connection.execute(
            "SELECT COUNT(*) FROM application_events"
        ).fetchone()[0]

    assert applications == 0
    assert events == 0


def test_unknown_job_raises(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        with pytest.raises(UnknownJobError):
            set_application_status(connection, job_id=9999, status="applied")


def test_invalid_status_raises(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        with pytest.raises(ValueError):
            set_application_status(connection, job_id=job_ids["a-1"], status="hired")


def test_list_applications_newest_first(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["r-1"],
            status="interview",
            now=_EPOCH + timedelta(hours=1),
        )
        items = list_applications(connection)

    assert [item.job.id for item in items] == [job_ids["r-1"], job_ids["a-1"]]
    assert [item.application.current_status for item in items] == [
        "interview",
        "applied",
    ]


def test_list_applications_tie_break_by_id_desc(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        set_application_status(
            connection, job_id=job_ids["r-1"], status="interested", now=_EPOCH
        )
        items = list_applications(connection)

    assert [item.job.id for item in items] == [job_ids["r-1"], job_ids["a-1"]]


def test_list_applications_status_filter(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        set_application_status(
            connection, job_id=job_ids["r-1"], status="interview", now=_EPOCH
        )
        items = list_applications(connection, statuses=("applied",))

    assert len(items) == 1
    assert items[0].job.id == job_ids["a-1"]


def test_list_applications_includes_latest_note(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="first note",
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="second note",
            now=_EPOCH + timedelta(hours=1),
        )
        items = list_applications(connection)

    assert items[0].latest_note == "second note"


def test_list_applications_empty(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        items = list_applications(connection)

    assert items == []


def test_events_ordered_newest_first(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="interested", now=_EPOCH
        )
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH + timedelta(hours=1),
        )
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="interview",
            now=_EPOCH + timedelta(hours=2),
        )
        application = get_application(connection, job_id=job_ids["a-1"])
        assert application is not None
        events = list_application_events(connection, application_id=application.id)

    assert [event.new_status for event in events] == [
        "interview",
        "applied",
        "interested",
    ]
    assert events[0].id > events[1].id > events[2].id


def test_events_tie_broken_by_id_desc(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="interested",
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            now=_EPOCH,
        )
        application = get_application(connection, job_id=job_ids["a-1"])
        assert application is not None
        events = list_application_events(connection, application_id=application.id)

    assert [event.new_status for event in events] == ["applied", "interested"]


def test_events_empty(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        application = get_application(connection, job_id=job_ids["a-1"])
        assert application is not None
        events = list_application_events(
            connection, application_id=application.id, limit=1
        )

    assert len(events) == 1


def test_statuses_for_jobs_maps_tracked_subset(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        statuses = application_statuses_for_jobs(
            connection, [job_ids["a-1"], job_ids["r-1"]]
        )

    assert statuses == {job_ids["a-1"]: "applied"}


def test_statuses_for_jobs_empty_input(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        statuses = application_statuses_for_jobs(connection, [])

    assert statuses == {}
