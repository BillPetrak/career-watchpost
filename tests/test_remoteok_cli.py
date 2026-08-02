"""CLI tests for the Remote OK collector."""

from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect

_SAMPLE_JOB = CollectedJob(
    source="remoteok",
    source_job_id="1",
    title="Python Engineer",
    company="Acme",
    location="Worldwide",
    description="Build things.",
    url="https://remoteok.com/remote-jobs/1",
    employment_type="Full Time",
    remote_status="remote",
    published_at="2026-08-01T16:00:13+00:00",
    collected_at="2026-08-01T17:00:00+00:00",
)


def _fake_remoteok_fetch(
    *, limit: int = 25, timeout: float = 30.0
) -> list[CollectedJob]:
    return [_SAMPLE_JOB][:limit]


def test_collect_remoteok_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "remoteok", _fake_remoteok_fetch)

    exit_code = cli.main(
        ["collect", "remoteok", "--limit", "1", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Remote OK" in captured.out
    assert "Fetched:  1" in captured.out
    assert "Inserted: 1" in captured.out
    assert "Updated:  0" in captured.out

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, source_job_id, title FROM jobs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["source"] == "remoteok"
    assert rows[0]["source_job_id"] == "1"
    assert rows[0]["title"] == "Python Engineer"


def test_collect_second_run_updates_no_duplicates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "remoteok", _fake_remoteok_fetch)

    args = ["collect", "remoteok", "--limit", "1", "--database", str(database_path)]

    assert cli.main(args) == 0
    capsys.readouterr()
    assert cli.main(args) == 0
    captured = capsys.readouterr()

    assert "Inserted: 0" in captured.out
    assert "Updated:  1" in captured.out

    with connect(database_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    assert count == 1


def test_collect_source_separation_from_arbeitnow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"

    def fake_arbeitnow_fetch(
        *, limit: int = 25, timeout: float = 30.0
    ) -> list[CollectedJob]:
        return [
            CollectedJob(
                source="arbeitnow",
                source_job_id="shared-1",
                title="Python Engineer",
                company="Other Corp",
                location="Athens",
                description="Build software.",
                url="https://arbeitnow.com/job/1",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-07-29T10:00:00Z",
                collected_at="2026-08-01T17:00:00+00:00",
            )
        ][:limit]

    monkeypatch.setitem(cli.COLLECTORS, "arbeitnow", fake_arbeitnow_fetch)
    monkeypatch.setitem(cli.COLLECTORS, "remoteok", _fake_remoteok_fetch)

    assert (
        cli.main(
            ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            ["collect", "remoteok", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()

    with connect(database_path) as connection:
        rows = connection.execute("SELECT source FROM jobs ORDER BY source").fetchall()

    assert [row["source"] for row in rows] == ["arbeitnow", "remoteok"]
