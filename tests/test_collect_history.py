"""Tests for collection run history (collect history)."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros import cli
from careeros.database.connection import connect
from careeros.database.runs import complete_run, insert_run, list_runs
from careeros.database.schema import initialize_schema

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


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


def _open(database_path: Path) -> sqlite3.Connection:
    connection = connect(database_path)
    initialize_schema(connection)
    return connection


def test_list_runs_newest_completed_first(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=2))
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1))
    _seed_run(database_path, "jobicy", _EPOCH)
    with _open(database_path) as connection:
        runs = list_runs(connection, limit=10)
    assert [run.run_id for run in runs] == [3, 2, 1]
    assert [run.started_at for run in runs] == [
        _EPOCH.isoformat(),
        (_EPOCH - timedelta(hours=1)).isoformat(),
        (_EPOCH - timedelta(hours=2)).isoformat(),
    ]


def test_list_runs_tie_broken_by_id_desc(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(database_path, "remoteok", _EPOCH)
    with _open(database_path) as connection:
        runs = list_runs(connection, limit=10)
    assert [run.run_id for run in runs] == [2, 1]


def test_list_runs_source_filter(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(database_path, "arbeitnow", _EPOCH - timedelta(hours=1))
    with _open(database_path) as connection:
        runs = list_runs(connection, source="jobicy", limit=10)
    assert [run.source for run in runs] == ["jobicy"]


def test_list_runs_failed_only(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH,
        succeeded=False,
        error="RuntimeError: boom",
    )
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1))
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=2), incomplete=True)
    with _open(database_path) as connection:
        runs = list_runs(connection, failed_only=True, limit=10)
    assert len(runs) == 1
    assert runs[0].succeeded == 0
    assert runs[0].error == "RuntimeError: boom"


def test_list_runs_limit(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    for hours in range(5):
        _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=hours))
    with _open(database_path) as connection:
        runs = list_runs(connection, limit=2)
    assert len(runs) == 2
    assert runs[0].started_at == _EPOCH.isoformat()
    assert runs[1].started_at == (_EPOCH - timedelta(hours=1)).isoformat()


def test_list_runs_includes_incomplete_by_default(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1), incomplete=True)
    with _open(database_path) as connection:
        runs = list_runs(connection, limit=10)
    assert [run.finished_at for run in runs] == [
        (_EPOCH + timedelta(seconds=5)).isoformat(),
        None,
    ]
    assert runs[1].succeeded is None


def test_cli_history_newest_first(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=2))
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=1))
    _seed_run(database_path, "jobicy", _EPOCH)
    assert cli.main(["collect", "history", "--database", str(database_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out.index("Run 3") < captured.out.index("Run 2")
    assert captured.out.index("Run 2") < captured.out.index("Run 1")


def test_cli_history_source_filter(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(database_path, "arbeitnow", _EPOCH - timedelta(hours=1))
    assert (
        cli.main(
            [
                "collect",
                "history",
                "--source",
                "jobicy",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Filter: source=jobicy" in captured.out
    assert "(jobicy)" in captured.out
    assert "(arbeitnow)" not in captured.out


def test_cli_history_invalid_source_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "collect",
                "history",
                "--source",
                "bogus",
                "--database",
                str(tmp_path / "x.db"),
            ]
        )
    assert exc_info.value.code == 2


@pytest.mark.parametrize("bad_limit", ["0", "-1"])
def test_cli_history_invalid_limit_rejected(tmp_path: Path, bad_limit: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "collect",
                "history",
                "--limit",
                bad_limit,
                "--database",
                str(tmp_path / "x.db"),
            ]
        )
    assert exc_info.value.code == 2


def test_cli_history_custom_limit(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    for hours in range(4, -1, -1):
        _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=hours))
    assert (
        cli.main(
            ["collect", "history", "--limit", "2", "--database", str(database_path)]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Run 5" in captured.out
    assert "Run 4" in captured.out
    assert "Run 3" not in captured.out


def test_cli_history_success_formatting(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    assert cli.main(["collect", "history", "--database", str(database_path)]) == 0
    captured = capsys.readouterr()
    assert "Status:   success" in captured.out
    assert f"Started:  {_EPOCH.isoformat()}" in captured.out
    assert f"Finished: {(_EPOCH + timedelta(seconds=5)).isoformat()}" in captured.out
    assert "Elapsed:  5.0s" in captured.out
    assert "Fetched:  1" in captured.out
    assert "Inserted: 1" in captured.out
    assert "Updated:  0" in captured.out


def test_cli_history_failed_formatting_with_error(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH,
        succeeded=False,
        error="URLError: network down",
    )
    assert cli.main(["collect", "history", "--database", str(database_path)]) == 0
    captured = capsys.readouterr()
    assert "Status:   failed" in captured.out
    assert "Error:    URLError: network down" in captured.out


def test_cli_history_incomplete_run_label(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH, incomplete=True)
    assert cli.main(["collect", "history", "--database", str(database_path)]) == 0
    captured = capsys.readouterr()
    assert "Status:   incomplete" in captured.out
    assert "Finished: -" in captured.out
    assert "Elapsed:  -" in captured.out


def test_cli_history_failed_filter_excludes_incomplete(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    _seed_run(
        database_path,
        "jobicy",
        _EPOCH - timedelta(hours=1),
        succeeded=False,
        error="URLError: down",
    )
    _seed_run(database_path, "jobicy", _EPOCH - timedelta(hours=2), incomplete=True)
    assert (
        cli.main(["collect", "history", "--failed", "--database", str(database_path)])
        == 0
    )
    captured = capsys.readouterr()
    assert "Filter: failed only" in captured.out
    assert "Status:   failed" in captured.out
    assert "URLError: down" in captured.out
    assert "Status:   incomplete" not in captured.out
    assert "Status:   success" not in captured.out


def test_cli_history_malformed_timestamps_do_not_crash(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', 'not-a-date', 'also-not-a-date', 1)
            """
        )
        connection.commit()
    assert cli.main(["collect", "history", "--database", str(database_path)]) == 0
    captured = capsys.readouterr()
    assert "Status:   success" in captured.out
    assert "not-a-date" in captured.out
    assert "Elapsed:  -" in captured.out


def test_cli_history_does_not_mutate_database(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_run(database_path, "jobicy", _EPOCH)
    with connect(database_path) as connection:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM collection_runs ORDER BY id"
            ).fetchall()
        ]

    assert cli.main(["collect", "history", "--database", str(database_path)]) == 0
    capsys.readouterr()

    with connect(database_path) as connection:
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM collection_runs ORDER BY id"
            ).fetchall()
        ]
    assert after == before


def test_cli_history_empty(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
    exit_code = cli.main(["collect", "history", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No collection runs found." in captured.out
    with connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
    assert count == 0


def test_cli_history_missing_database_fails_without_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = tmp_path / "missing.sqlite3"
    assert cli.main(["collect", "history", "--database", str(database_path)]) == 1
    captured = capsys.readouterr()
    assert "History failed: FileNotFoundError:" in captured.err
    assert not database_path.exists()


def test_cli_history_uninitialized_database_fails_without_schema_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = tmp_path / "uninitialized.sqlite3"
    database_path.touch()
    assert cli.main(["collect", "history", "--database", str(database_path)]) == 1
    captured = capsys.readouterr()
    assert "History failed: ValueError: database schema is not current" in captured.err
    assert database_path.read_bytes() == b""


def test_cli_history_database_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    exit_code = cli.main(["collect", "history", "--database", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "History failed:" in captured.err
