"""CLI integration tests for collection locking."""

import json
import os
from pathlib import Path
from urllib.error import URLError

import pytest

from careeros import cli
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock, lock_path_for

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


def _fake_collectors() -> dict[str, _FakeCollector]:
    """Build one recording fake per expected source, in registry order."""
    return {source: _FakeCollector(source) for source in _ORDER}


def _patch_collectors(
    monkeypatch: pytest.MonkeyPatch,
    fakes: dict[str, _FakeCollector],
) -> None:
    """Replace the real collectors in the CLI registry with fakes."""
    for source, fake in fakes.items():
        monkeypatch.setitem(cli.COLLECTORS, source, fake)


def _table_counts(database_path: Path) -> tuple[int, int]:
    """Return (collection_runs, jobs) row counts for the database."""
    with connect(database_path) as connection:
        initialize_schema(connection)
        runs = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
        jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    return int(runs), int(jobs)


@pytest.mark.parametrize(
    "command",
    [
        ["collect", "arbeitnow"],
        ["collect", "all"],
        ["collect", "due"],
    ],
)
def test_collection_commands_conflict_with_held_lock(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    with collection_lock(database_path):
        exit_code = cli.main(
            [*command, "--limit", "1", "--database", str(database_path)]
        )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    assert str(database_path) in captured.err
    assert all(fake.calls == 0 for fake in fakes.values())


def test_lock_contention_makes_no_database_changes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)
    with connect(database_path) as connection:
        initialize_schema(connection)
        connection.execute(
            "INSERT INTO jobs (source, source_job_id, title, collected_at)"
            " VALUES ('arbeitnow', 'seed', 'seed job', '2026-08-03T12:00:00+00:00')"
        )
        connection.commit()

    with collection_lock(database_path):
        exit_code = cli.main(
            ["collect", "due", "--limit", "1", "--database", str(database_path)]
        )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    assert _table_counts(database_path) == (0, 1)


def test_conflict_message_includes_owner_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    with collection_lock(database_path, command="collect due"):
        exit_code = cli.main(
            ["collect", "due", "--limit", "1", "--database", str(database_path)]
        )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Owner:" in captured.err
    assert f"pid={os.getpid()}" in captured.err
    assert "command='collect due'" in captured.err


def test_read_only_commands_remain_available_while_locked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with collection_lock(database_path):
        assert cli.main(["collect", "status", "--database", str(database_path)]) == 0
    capsys.readouterr()
    with collection_lock(database_path):
        assert cli.main(["collect", "history", "--database", str(database_path)]) == 0
    capsys.readouterr()
    with collection_lock(database_path):
        assert cli.main(["search", "engineer", "--database", str(database_path)]) == 0
    capsys.readouterr()


def test_different_database_paths_collect_independently(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first" / "careeros.sqlite3"
    second = tmp_path / "second" / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    with collection_lock(first):
        exit_code = cli.main(
            ["collect", "arbeitnow", "--limit", "1", "--database", str(second)]
        )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert fakes["arbeitnow"].calls == 1
    assert "Collection complete" in captured.out


def test_lock_releases_after_expected_collector_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["arbeitnow"] = _FakeCollector("arbeitnow", error=URLError("network down"))
    _patch_collectors(monkeypatch, fakes)

    exit_code = cli.main(
        ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Collection failed: URLError" in captured.err
    with collection_lock(database_path):
        pass


def test_lock_releases_after_unexpected_exception(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["arbeitnow"] = _FakeCollector("arbeitnow", error=TypeError("bug"))
    _patch_collectors(monkeypatch, fakes)

    with pytest.raises(TypeError):
        cli.main(
            ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
        )

    with collection_lock(database_path):
        pass


def test_lock_releases_after_keyboard_interrupt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    fakes["arbeitnow"] = _FakeCollector("arbeitnow", error=KeyboardInterrupt())
    _patch_collectors(monkeypatch, fakes)

    with pytest.raises(KeyboardInterrupt):
        cli.main(
            ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
        )

    with collection_lock(database_path):
        pass


def test_cli_records_subcommand_only_in_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    assert (
        cli.main(["collect", "all", "--limit", "1", "--database", str(database_path)])
        == 0
    )
    capsys.readouterr()

    payload = json.loads(lock_path_for(database_path).read_text())
    assert payload["command"] == "collect all"
    assert "pytest" not in payload["command"]


def test_lock_path_failure_reported_as_collection_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    fakes = _fake_collectors()
    _patch_collectors(monkeypatch, fakes)

    def fail_open(*args: object, **kwargs: object) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr("careeros.locking.os.open", fail_open)

    exit_code = cli.main(
        ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Collection failed: PermissionError: denied" in captured.err
    assert fakes["arbeitnow"].calls == 0
