"""Tests for collection status observability (collect status)."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors import COLLECTORS
from careeros.database.connection import connect
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import initialize_schema
from careeros.status import build_statuses

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_ORDER = tuple(COLLECTORS)


def _seed_run(
    database_path: Path,
    source: str,
    started_at: datetime,
    *,
    succeeded: bool = True,
    error: str | None = None,
    fetched: int = 1,
    inserted: int = 1,
    updated: int = 0,
    incomplete: bool = False,
) -> None:
    """Write a run record directly into the database."""
    with connect(database_path) as connection:
        initialize_schema(connection)
        run_id = insert_run(connection, source=source, started_at=started_at)
        if not incomplete:
            complete_run(
                connection,
                run_id=run_id,
                finished_at=started_at + timedelta(seconds=5),
                succeeded=succeeded,
                fetched=fetched,
                inserted=inserted,
                updated=updated,
                error=error,
            )


def _statuses(database_path: Path, *, now: datetime = _EPOCH) -> dict[str, object]:
    with connect(database_path) as connection:
        initialize_schema(connection)
        return {status.source: status for status in build_statuses(connection, now=now)}


def test_never_run_sources_are_due(tmp_path: Path) -> None:
    statuses = _statuses(tmp_path / "careeros.sqlite3")
    assert {source: statuses[source].due for source in _ORDER} == {
        source: True for source in _ORDER
    }
    assert all(statuses[source].last_attempt is None for source in _ORDER)
    assert all(statuses[source].next_due is None for source in _ORDER)


def test_recent_successful_attempt_is_not_due(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(minutes=30))
    statuses = _statuses(database_path)
    assert statuses["jobicy"].due is False
    assert statuses["jobicy"].last_attempt == _EPOCH - timedelta(minutes=30)
    assert statuses["jobicy"].last_succeeded is True
    assert statuses["jobicy"].next_due == _EPOCH + timedelta(minutes=30)
    assert statuses["arbeitnow"].due is True


def test_expired_successful_attempt_is_due(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=2))
    statuses = _statuses(database_path)
    assert statuses["jobicy"].due is True
    assert statuses["jobicy"].next_due == _EPOCH - timedelta(hours=1)


def test_recent_failed_completed_attempt_delays_retry(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH - timedelta(minutes=5),
        succeeded=False,
        error="RuntimeError: boom",
    )
    statuses = _statuses(database_path)
    assert statuses["jobicy"].due is False
    assert statuses["jobicy"].last_succeeded is False
    assert statuses["jobicy"].last_error == "RuntimeError: boom"


def test_expired_failed_attempt_is_due(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH - timedelta(hours=2),
        succeeded=False,
        error="RuntimeError: boom",
    )
    assert _statuses(database_path)["jobicy"].due is True


def test_incomplete_attempt_does_not_replace_last_completed(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1))
    _seed_run(database_path, "jobicy", _EPOCH, incomplete=True)
    statuses = _statuses(database_path)
    assert statuses["jobicy"].last_attempt == _EPOCH - timedelta(hours=1)
    assert statuses["jobicy"].last_succeeded is True
    assert statuses["jobicy"].incomplete_runs == 1


def test_malformed_timestamp_handled_safely_and_source_is_due(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', 'not-a-date', 'not-a-date', 1)
            """
        )
        connection.commit()
    statuses = _statuses(database_path)
    assert statuses["jobicy"].last_attempt is None
    assert statuses["jobicy"].due is True


def test_registry_order_preserved(tmp_path: Path) -> None:
    with connect(tmp_path / "careeros.sqlite3") as connection:
        initialize_schema(connection)
        statuses = build_statuses(connection, now=_EPOCH)
    assert [status.source for status in statuses] == list(_ORDER)


def test_one_fixed_now_used_consistently(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(minutes=30))
    _seed_run(database_path, "arbeitnow", _EPOCH - timedelta(days=2))
    statuses = _statuses(database_path)
    assert statuses["jobicy"].due is False
    assert statuses["jobicy"].next_due == _EPOCH + timedelta(minutes=30)
    assert statuses["arbeitnow"].due is True
    assert statuses["arbeitnow"].next_due == _EPOCH - timedelta(days=2) + timedelta(
        hours=6
    )


def test_cli_status_never_run_shows_due(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("careeros.cli._utc_now", lambda: _EPOCH)
    exit_code = cli.main(
        ["collect", "status", "--database", str(tmp_path / "careeros.sqlite3")]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"As of: {_EPOCH.isoformat()}" in captured.out
    assert "Last attempt:   never" in captured.out
    assert "Last status:    never" in captured.out
    assert "State:          due" in captured.out
    assert "1 hour (documented)" in captured.out
    assert "6 hours (provisional)" in captured.out


def test_cli_status_recent_success_not_due(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(minutes=30))
    monkeypatch.setattr("careeros.cli._utc_now", lambda: _EPOCH)
    exit_code = cli.main(["collect", "status", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Last status:    success" in captured.out
    assert "State:          not due" in captured.out


def test_cli_status_failed_run_shows_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH - timedelta(minutes=5),
        succeeded=False,
        error="URLError: network down",
    )
    monkeypatch.setattr("careeros.cli._utc_now", lambda: _EPOCH)
    exit_code = cli.main(["collect", "status", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Last status:    failed" in captured.out
    assert "Error:          URLError: network down" in captured.out


def test_cli_status_surfaces_incomplete_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1))
    _seed_run(database_path, "jobicy", _EPOCH, incomplete=True)
    monkeypatch.setattr("careeros.cli._utc_now", lambda: _EPOCH)
    assert cli.main(["collect", "status", "--database", str(database_path)]) == 0
    captured = capsys.readouterr()
    assert "Incomplete runs: 1" in captured.out


def test_cli_status_does_not_mutate_database(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    with connect(database_path) as connection:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM collection_runs ORDER BY id"
            ).fetchall()
        ]

    assert cli.main(["collect", "status", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with connect(database_path) as connection:
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM collection_runs ORDER BY id"
            ).fetchall()
        ]
    assert after == before


def test_cli_status_on_empty_database_adds_no_runs(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    assert cli.main(["collect", "status", "--database", str(database_path)]) == 0
    capsys.readouterr()
    with connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert count == 0


def test_cli_status_custom_database(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "custom" / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    exit_code = cli.main(["collect", "status", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Jobicy" in captured.out


def test_cli_status_database_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = cli.main(["collect", "status", "--database", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Status failed:" in captured.err
