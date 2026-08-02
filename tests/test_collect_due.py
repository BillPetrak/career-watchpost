"""Tests for rate-aware scheduled collection (collect due)."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros import cli
from careeros.collecting import collect_due
from careeros.collection_policies import SOURCE_POLICIES
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import initialize_schema

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_ORDER = ("arbeitnow", "remoteok", "weworkremotely", "jobicy")


def _make_job(source: str, source_job_id: str) -> CollectedJob:
    """Build a minimal job owned by the given source."""
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title="Python Engineer",
        company="Acme",
        location="Worldwide",
        description="Build things.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full Time",
        remote_status="remote",
        published_at="2026-08-01T16:00:13+00:00",
        collected_at=_EPOCH.isoformat(),
    )


class _FakeCollector:
    """Recording stand-in for a real collector fetch function."""

    def __init__(
        self,
        source: str,
        *,
        count: int = 1,
        error: Exception | None = None,
    ) -> None:
        self.source = source
        self.count = count
        self.error = error
        self.calls = 0

    def __call__(
        self,
        *,
        limit: int = 25,
        timeout: float = 30.0,
    ) -> list[CollectedJob]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [
            _make_job(self.source, f"{self.source}-{index}")
            for index in range(self.count)
        ][:limit]


def _fake_collectors(count: int = len(_ORDER)) -> dict[str, _FakeCollector]:
    """Build one recording fake per expected source, in registry order."""
    return {source: _FakeCollector(source) for source in _ORDER[:count]}


def _patch_collectors(
    monkeypatch: pytest.MonkeyPatch,
    fakes: dict[str, _FakeCollector],
) -> None:
    """Replace the real collectors in the CLI registry with fakes."""
    for source, fake in fakes.items():
        monkeypatch.setitem(cli.COLLECTORS, source, fake)


def _clock() -> datetime:
    """A fixed deterministic clock for scheduling and run timestamps."""
    return _EPOCH


def _seed_completed_run(
    database_path: Path,
    source: str,
    started_at: datetime,
    *,
    succeeded: bool = True,
) -> None:
    """Write a completed run record directly into the database."""
    with connect(database_path) as connection:
        initialize_schema(connection)
        run_id = insert_run(connection, source=source, started_at=started_at)
        complete_run(
            connection,
            run_id=run_id,
            finished_at=started_at,
            succeeded=succeeded,
            fetched=1,
            inserted=1,
            updated=0,
            error=None if succeeded else "RuntimeError: boom",
        )


def test_collect_due_no_history_collects_all_in_registry_order(tmp_path: Path) -> None:
    fakes = _fake_collectors()

    results = list(
        collect_due(
            limit=2,
            database_path=tmp_path / "careeros.sqlite3",
            collectors=fakes,
            policies=SOURCE_POLICIES,
            clock=_clock,
        )
    )

    assert [result.source for result in results] == list(fakes)
    assert all(result.succeeded for result in results)
    for source in fakes:
        assert fakes[source].calls == 1


def test_collect_due_skips_recent_sources(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    for source in _ORDER:
        if source == "jobicy":
            started_at = _EPOCH - timedelta(minutes=30)
        else:
            started_at = _EPOCH - timedelta(days=2)
        _seed_completed_run(database_path, source, started_at)
    fakes = _fake_collectors()

    results = list(
        collect_due(
            limit=2,
            database_path=database_path,
            collectors=fakes,
            policies=SOURCE_POLICIES,
            clock=_clock,
        )
    )

    assert [result.source for result in results] == [
        "arbeitnow",
        "remoteok",
        "weworkremotely",
    ]
    assert fakes["jobicy"].calls == 0
    assert all(fakes[result.source].calls == 1 for result in results)


def test_collect_due_records_run_history(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors(count=1)

    list(
        collect_due(
            limit=2,
            database_path=database_path,
            collectors=fakes,
            policies=SOURCE_POLICIES,
            clock=_clock,
        )
    )

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, started_at, finished_at, succeeded, fetched,"
            " inserted, updated FROM collection_runs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["source"] == "arbeitnow"
    assert rows[0]["started_at"] == _EPOCH.isoformat()
    assert rows[0]["finished_at"] == _EPOCH.isoformat()
    assert rows[0]["succeeded"] == 1
    assert rows[0]["fetched"] == 1
    assert rows[0]["inserted"] == 1
    assert rows[0]["updated"] == 0


def test_collect_due_failed_completed_run_delays_retry(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    for source in _ORDER:
        _seed_completed_run(database_path, source, _EPOCH, succeeded=False)
    fakes = _fake_collectors()

    results = list(
        collect_due(
            limit=2,
            database_path=database_path,
            collectors=fakes,
            policies=SOURCE_POLICIES,
            clock=_clock,
        )
    )

    assert results == []
    for source in fakes:
        assert fakes[source].calls == 0


def test_expired_failed_run_becomes_due_again(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_completed_run(
        database_path,
        "jobicy",
        _EPOCH - timedelta(hours=2),
        succeeded=False,
    )
    fakes = {"jobicy": _FakeCollector("jobicy")}

    results = list(
        collect_due(
            limit=2,
            database_path=database_path,
            collectors=fakes,
            policies=SOURCE_POLICIES,
            clock=_clock,
        )
    )

    assert [result.source for result in results] == ["jobicy"]
    assert fakes["jobicy"].calls == 1


def test_collect_due_empty_success_advances_schedule(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    def fetch_empty(*, limit: int = 25, timeout: float = 30.0) -> list[CollectedJob]:
        return []

    collectors = {"arbeitnow": fetch_empty}
    first = list(
        collect_due(
            limit=2,
            database_path=database_path,
            collectors=collectors,
            policies=SOURCE_POLICIES,
            clock=_clock,
        )
    )
    second = list(
        collect_due(
            limit=2,
            database_path=database_path,
            collectors=collectors,
            policies=SOURCE_POLICIES,
            clock=_clock,
        )
    )

    assert len(first) == 1
    assert first[0].succeeded is True
    assert first[0].fetched == 0
    assert second == []


def test_collect_due_programming_error_propagates(tmp_path: Path) -> None:
    def broken_fetch(*, limit: int = 25, timeout: float = 30.0) -> list[CollectedJob]:
        raise TypeError("unexpected bug")

    with pytest.raises(TypeError):
        list(
            collect_due(
                limit=2,
                database_path=tmp_path / "careeros.sqlite3",
                collectors={"arbeitnow": broken_fetch},
                policies=SOURCE_POLICIES,
                clock=_clock,
            )
        )


def test_collect_due_cli_all_due_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)
    monkeypatch.setattr("careeros.collecting._utc_now", _clock)

    exit_code = cli.main(
        ["collect", "due", "--limit", "2", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Due-source collection complete" in captured.out
    assert "Sources attempted: 4" in captured.out
    assert "Succeeded:         4" in captured.out
    assert "Status:   success" in captured.out
    for source in _ORDER:
        assert fakes[source].calls == 1


def test_collect_due_cli_no_sources_due_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    for source in _ORDER:
        _seed_completed_run(database_path, source, _EPOCH)
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)
    monkeypatch.setattr("careeros.collecting._utc_now", _clock)

    exit_code = cli.main(
        ["collect", "due", "--limit", "2", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No sources are due." in captured.out
    assert "Sources attempted: 0" in captured.out
    for source in fakes:
        assert fakes[source].calls == 0


def test_collect_due_cli_partial_failure_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["jobicy"] = _FakeCollector("jobicy", error=RuntimeError("boom"))
    _patch_collectors(monkeypatch, fakes)
    monkeypatch.setattr("careeros.collecting._utc_now", _clock)

    exit_code = cli.main(
        ["collect", "due", "--limit", "2", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "jobicy" in captured.out
    assert "Error:  RuntimeError: boom" in captured.out
    assert "Failed:            1" in captured.out
    assert "jobicy collection failed: RuntimeError: boom" in captured.err
    assert fakes["arbeitnow"].calls == 1
    assert fakes["jobicy"].calls == 1


def test_collect_due_cli_setup_failure_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)
    monkeypatch.setattr("careeros.collecting._utc_now", _clock)

    exit_code = cli.main(
        ["collect", "due", "--limit", "2", "--database", str(tmp_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Collection failed:" in captured.err
    for source in fakes:
        assert fakes[source].calls == 0


def test_manual_single_source_collect_records_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = {"jobicy": _FakeCollector("jobicy")}
    _patch_collectors(monkeypatch, fakes)

    assert (
        cli.main(
            ["collect", "jobicy", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, succeeded, fetched FROM collection_runs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["source"] == "jobicy"
    assert rows[0]["succeeded"] == 1
    assert rows[0]["fetched"] == 1


def test_manual_collect_bypasses_scheduling(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    _seed_completed_run(database_path, "jobicy", _EPOCH)
    fakes = {"jobicy": _FakeCollector("jobicy")}
    _patch_collectors(monkeypatch, fakes)
    monkeypatch.setattr("careeros.collecting._utc_now", _clock)

    assert (
        cli.main(
            ["collect", "jobicy", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()

    assert fakes["jobicy"].calls == 1


def test_manual_single_source_records_failed_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = {"jobicy": _FakeCollector("jobicy", error=RuntimeError("boom"))}
    _patch_collectors(monkeypatch, fakes)

    exit_code = cli.main(
        ["collect", "jobicy", "--limit", "1", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Collection failed: RuntimeError: boom" in captured.err

    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT source, succeeded, error FROM collection_runs"
        ).fetchone()

    assert row["succeeded"] == 0
    assert row["error"] == "RuntimeError: boom"


def test_collect_all_records_run_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    assert (
        cli.main(["collect", "all", "--limit", "2", "--database", str(database_path)])
        == 0
    )
    capsys.readouterr()

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, succeeded FROM collection_runs ORDER BY id"
        ).fetchall()

    assert [(row["source"], row["succeeded"]) for row in rows] == [
        (source, 1) for source in _ORDER
    ]
