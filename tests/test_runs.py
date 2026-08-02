"""Tests for run-history persistence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from careeros.database.connection import connect
from careeros.database.runs import complete_run, insert_run, last_completed_attempt
from careeros.database.schema import initialize_schema

_START = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_FINISH = datetime.fromisoformat("2026-08-03T12:00:05+00:00")


def _open(database_path: Path) -> None:
    connection = connect(database_path)
    initialize_schema(connection)
    return connection


def test_insert_run_creates_incomplete_row(tmp_path: Path) -> None:
    with _open(tmp_path / "careeros.sqlite3") as connection:
        run_id = insert_run(connection, source="jobicy", started_at=_START)
        row = connection.execute(
            "SELECT * FROM collection_runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert row["source"] == "jobicy"
    assert row["started_at"] == _START.isoformat()
    assert row["finished_at"] is None
    assert row["succeeded"] is None
    assert row["fetched"] == 0


def test_complete_run_success_updates_row(tmp_path: Path) -> None:
    with _open(tmp_path / "careeros.sqlite3") as connection:
        run_id = insert_run(connection, source="jobicy", started_at=_START)
        complete_run(
            connection,
            run_id=run_id,
            finished_at=_FINISH,
            succeeded=True,
            fetched=2,
            inserted=1,
            updated=1,
            error=None,
        )
        row = connection.execute(
            "SELECT * FROM collection_runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert row["finished_at"] == _FINISH.isoformat()
    assert row["succeeded"] == 1
    assert row["fetched"] == 2
    assert row["inserted"] == 1
    assert row["updated"] == 1
    assert row["error"] is None


def test_complete_run_failure_stores_error(tmp_path: Path) -> None:
    with _open(tmp_path / "careeros.sqlite3") as connection:
        run_id = insert_run(connection, source="jobicy", started_at=_START)
        complete_run(
            connection,
            run_id=run_id,
            finished_at=_FINISH,
            succeeded=False,
            fetched=0,
            inserted=0,
            updated=0,
            error="URLError: network down",
        )
        row = connection.execute(
            "SELECT * FROM collection_runs WHERE id = ?", (run_id,)
        ).fetchone()
    assert row["succeeded"] == 0
    assert row["error"] == "URLError: network down"


def test_last_completed_attempt_returns_latest(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with _open(database_path) as connection:
        first = insert_run(connection, source="jobicy", started_at=_START)
        complete_run(
            connection,
            run_id=first,
            finished_at=_FINISH,
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
        later = _START + timedelta(minutes=1)
        second = insert_run(connection, source="jobicy", started_at=later)
        complete_run(
            connection,
            run_id=second,
            finished_at=later,
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
        latest = last_completed_attempt(connection, "jobicy")
    assert latest == later


def test_last_completed_attempt_ignores_incomplete_run(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with _open(database_path) as connection:
        completed = insert_run(connection, source="jobicy", started_at=_START)
        complete_run(
            connection,
            run_id=completed,
            finished_at=_FINISH,
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
        insert_run(connection, source="jobicy", started_at=_FINISH)
        latest = last_completed_attempt(connection, "jobicy")
    assert latest == _START


def test_last_completed_attempt_none_when_no_runs(tmp_path: Path) -> None:
    with _open(tmp_path / "careeros.sqlite3") as connection:
        assert last_completed_attempt(connection, "jobicy") is None


def test_last_completed_attempt_malformed_timestamp_is_none(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with _open(database_path) as connection:
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', 'not-a-date', 'not-a-date', 1)
            """
        )
        connection.commit()
        assert last_completed_attempt(connection, "jobicy") is None


def test_naive_timestamp_assumed_utc(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with _open(database_path) as connection:
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', '2026-08-03T12:00:00', '2026-08-03T12:00:00', 1)
            """
        )
        connection.commit()
        latest = last_completed_attempt(connection, "jobicy")
    assert latest is not None
    assert latest.tzinfo is not None
    assert latest == datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def test_last_completed_attempt_is_per_source(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with _open(database_path) as connection:
        jobicy_run = insert_run(connection, source="jobicy", started_at=_START)
        complete_run(
            connection,
            run_id=jobicy_run,
            finished_at=_FINISH,
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
        arbeitnow_start = _START - timedelta(days=1)
        arbeitnow_run = insert_run(
            connection, source="arbeitnow", started_at=arbeitnow_start
        )
        complete_run(
            connection,
            run_id=arbeitnow_run,
            finished_at=arbeitnow_start,
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
        assert last_completed_attempt(connection, "jobicy") == _START
        assert last_completed_attempt(connection, "arbeitnow") == arbeitnow_start
