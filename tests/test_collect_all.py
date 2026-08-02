"""Tests for unified multi-source collection (collect all)."""

from pathlib import Path
from urllib.error import URLError

import pytest

from careeros import cli
from careeros.collecting import CollectionResult, collect_all
from careeros.collectors import COLLECTORS
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect

_EXPECTED_ORDER = ("arbeitnow", "remoteok", "weworkremotely", "jobicy")


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
        collected_at="2026-08-02T08:00:00+00:00",
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
        self.limits: list[int] = []

    def __call__(
        self,
        *,
        limit: int = 25,
        timeout: float = 30.0,
    ) -> list[CollectedJob]:
        self.calls += 1
        self.limits.append(limit)
        if self.error is not None:
            raise self.error
        return [
            _make_job(self.source, f"{self.source}-{index}")
            for index in range(self.count)
        ][:limit]


def _fake_collectors(count: int = len(_EXPECTED_ORDER)) -> dict[str, _FakeCollector]:
    """Build one recording fake per expected source, in registry order."""
    return {source: _FakeCollector(source) for source in _EXPECTED_ORDER[:count]}


def _patch_collectors(
    monkeypatch: pytest.MonkeyPatch,
    fakes: dict[str, _FakeCollector],
) -> None:
    """Replace the real collectors in the CLI registry with fakes."""
    for source, fake in fakes.items():
        monkeypatch.setitem(cli.COLLECTORS, source, fake)


def test_registry_defines_stable_execution_order() -> None:
    assert list(COLLECTORS) == ["arbeitnow", "remoteok", "weworkremotely", "jobicy"]


def test_collect_all_executes_sources_in_registry_order(tmp_path: Path) -> None:
    fakes = _fake_collectors()

    results = list(
        collect_all(
            limit=2,
            database_path=tmp_path / "careeros.sqlite3",
            collectors=fakes,
        )
    )

    assert [result.source for result in results] == list(fakes)
    assert all(isinstance(result, CollectionResult) for result in results)
    for source in fakes:
        assert fakes[source].calls == 1


def test_collect_all_passes_same_limit_to_every_collector(tmp_path: Path) -> None:
    fakes = _fake_collectors()

    list(
        collect_all(
            limit=3,
            database_path=tmp_path / "careeros.sqlite3",
            collectors=fakes,
        )
    )

    for source in fakes:
        assert fakes[source].limits == [3]


def test_collect_all_uses_one_database_for_all_sources(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors(count=2)

    list(
        collect_all(
            limit=2,
            database_path=database_path,
            collectors=fakes,
        )
    )

    with connect(database_path) as connection:
        rows = connection.execute("SELECT source FROM jobs ORDER BY source").fetchall()

    assert [row["source"] for row in rows] == sorted(fakes)


def test_collect_all_saves_each_source_jobs(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors(count=2)

    list(
        collect_all(
            limit=5,
            database_path=database_path,
            collectors=fakes,
        )
    )

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, source_job_id FROM jobs ORDER BY source"
        ).fetchall()

    assert [row["source"] for row in rows] == ["arbeitnow", "remoteok"]
    assert rows[0]["source_job_id"] == "arbeitnow-0"
    assert rows[1]["source_job_id"] == "remoteok-0"


def test_collect_all_reports_per_source_inserted_updated(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors(count=2)

    first = list(
        collect_all(
            limit=5,
            database_path=database_path,
            collectors=fakes,
        )
    )
    second = list(
        collect_all(
            limit=5,
            database_path=database_path,
            collectors=fakes,
        )
    )

    assert [(r.fetched, r.inserted, r.updated) for r in first] == [
        (1, 1, 0),
        (1, 1, 0),
    ]
    assert [(r.fetched, r.inserted, r.updated) for r in second] == [
        (1, 0, 1),
        (1, 0, 1),
    ]


def test_aggregate_totals_equal_sum_of_results(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["weworkremotely"] = _FakeCollector("weworkremotely", count=2)
    fakes["jobicy"] = _FakeCollector("jobicy", count=3)

    results = list(
        collect_all(
            limit=10,
            database_path=database_path,
            collectors=fakes,
        )
    )

    assert len(results) == len(_EXPECTED_ORDER)
    assert all(result.succeeded for result in results)
    assert sum(result.fetched for result in results) == 1 + 1 + 2 + 3
    assert sum(result.inserted for result in results) == 1 + 1 + 2 + 3
    assert sum(result.updated for result in results) == 0


def test_failing_first_source_does_not_block_later(tmp_path: Path) -> None:
    fakes = _fake_collectors()
    fakes["arbeitnow"] = _FakeCollector("arbeitnow", error=RuntimeError("boom"))

    results = list(
        collect_all(
            limit=2,
            database_path=tmp_path / "careeros.sqlite3",
            collectors=fakes,
        )
    )

    assert results[0].source == "arbeitnow"
    assert results[0].succeeded is False
    assert results[0].error == "RuntimeError: boom"
    assert [result.succeeded for result in results[1:]] == [True, True, True]
    for source in fakes:
        assert fakes[source].calls == 1


def test_failing_middle_source_does_not_block_later(tmp_path: Path) -> None:
    fakes = _fake_collectors()
    fakes["weworkremotely"] = _FakeCollector(
        "weworkremotely", error=RuntimeError("boom")
    )

    results = list(
        collect_all(
            limit=2,
            database_path=tmp_path / "careeros.sqlite3",
            collectors=fakes,
        )
    )

    assert [result.succeeded for result in results] == [True, True, False, True]
    assert results[2].source == "weworkremotely"
    assert results[2].error == "RuntimeError: boom"


def test_failed_source_does_not_roll_back_previous_work(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["remoteok"] = _FakeCollector("remoteok", error=RuntimeError("boom"))

    list(
        collect_all(
            limit=2,
            database_path=database_path,
            collectors=fakes,
        )
    )

    with connect(database_path) as connection:
        rows = connection.execute("SELECT source FROM jobs ORDER BY source").fetchall()

    assert [row["source"] for row in rows] == ["arbeitnow", "jobicy", "weworkremotely"]


def test_duplicate_source_ids_remain_isolated_by_source(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    def fetch_arbeitnow(
        *, limit: int = 25, timeout: float = 30.0
    ) -> list[CollectedJob]:
        return [_make_job("arbeitnow", "shared-id")][:limit]

    def fetch_remoteok(*, limit: int = 25, timeout: float = 30.0) -> list[CollectedJob]:
        return [_make_job("remoteok", "shared-id")][:limit]

    collectors = {"arbeitnow": fetch_arbeitnow, "remoteok": fetch_remoteok}

    list(
        collect_all(
            limit=5,
            database_path=database_path,
            collectors=collectors,
        )
    )

    with connect(database_path) as connection:
        rows = connection.execute("SELECT source FROM jobs ORDER BY source").fetchall()

    assert [row["source"] for row in rows] == ["arbeitnow", "remoteok"]


def test_empty_collector_result_is_successful_zero_jobs(tmp_path: Path) -> None:
    def fetch_empty(*, limit: int = 25, timeout: float = 30.0) -> list[CollectedJob]:
        return []

    results = list(
        collect_all(
            limit=5,
            database_path=tmp_path / "careeros.sqlite3",
            collectors={"arbeitnow": fetch_empty},
        )
    )

    assert len(results) == 1
    assert results[0].source == "arbeitnow"
    assert results[0].succeeded is True
    assert results[0].fetched == 0
    assert results[0].inserted == 0
    assert results[0].updated == 0


def test_network_error_marks_source_failed(tmp_path: Path) -> None:
    fakes = _fake_collectors(count=1)
    fakes["arbeitnow"] = _FakeCollector("arbeitnow", error=URLError("network down"))

    results = list(
        collect_all(
            limit=2,
            database_path=tmp_path / "careeros.sqlite3",
            collectors=fakes,
        )
    )

    assert results[0].succeeded is False
    assert results[0].error == "URLError: <urlopen error network down>"


def test_unexpected_programming_error_not_swallowed(tmp_path: Path) -> None:
    def broken_fetch(*, limit: int = 25, timeout: float = 30.0) -> list[CollectedJob]:
        raise TypeError("unexpected bug")

    with pytest.raises(TypeError):
        list(
            collect_all(
                limit=5,
                database_path=tmp_path / "careeros.sqlite3",
                collectors={"arbeitnow": broken_fetch},
            )
        )


def test_collect_all_success_returns_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    exit_code = cli.main(
        ["collect", "all", "--limit", "2", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    for source in _EXPECTED_ORDER:
        assert source in captured.out
    assert "Status:   success" in captured.out
    assert "Sources attempted: 4" in captured.out
    assert "Succeeded:         4" in captured.out
    assert "Failed:            0" in captured.out


def test_one_source_failure_returns_1_and_reports_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["jobicy"] = _FakeCollector("jobicy", error=RuntimeError("boom"))
    _patch_collectors(monkeypatch, fakes)

    exit_code = cli.main(
        ["collect", "all", "--limit", "2", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "jobicy" in captured.out
    assert "Error:  RuntimeError: boom" in captured.out
    assert "Succeeded:         3" in captured.out
    assert "Failed:            1" in captured.out
    assert "jobicy collection failed: RuntimeError: boom" in captured.err


def test_collect_all_empty_source_is_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["jobicy"] = _FakeCollector("jobicy", count=0)
    _patch_collectors(monkeypatch, fakes)

    exit_code = cli.main(
        ["collect", "all", "--limit", "2", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Fetched:  0" in captured.out
    assert "Succeeded:         4" in captured.out
    assert "Failed:            0" in captured.out


def test_collect_all_invalid_limit_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            ["collect", "all", "--limit", "0", "--database", str(tmp_path / "x.db")]
        )

    assert exc_info.value.code != 0


def test_collect_all_custom_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "custom" / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    exit_code = cli.main(
        ["collect", "all", "--limit", "2", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Database:          {database_path}" in captured.out

    with connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    assert count == len(_EXPECTED_ORDER)


def test_single_source_collection_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    exit_code = cli.main(
        ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert fakes["arbeitnow"].calls == 1
    assert fakes["remoteok"].calls == 0
    assert fakes["weworkremotely"].calls == 0
    assert fakes["jobicy"].calls == 0
    assert "Collection complete" in captured.out
    assert "Fetched:  1" in captured.out
