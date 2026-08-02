"""CLI tests for the We Work Remotely collector."""

from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect

_SAMPLE_JOB = CollectedJob(
    source="weworkremotely",
    source_job_id="https://weworkremotely.com/remote-jobs/python-engineer",
    title="Python Engineer",
    company="Acme",
    location="Anywhere in the World",
    description="Build things.",
    url="https://weworkremotely.com/remote-jobs/python-engineer",
    employment_type="full time",
    remote_status="remote",
    published_at="2026-08-02T07:31:05+00:00",
    collected_at="2026-08-02T08:00:00+00:00",
)


def _fake_weworkremotely_fetch(
    *, limit: int = 25, timeout: float = 30.0
) -> list[CollectedJob]:
    return [_SAMPLE_JOB][:limit]


def test_collect_weworkremotely_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "weworkremotely", _fake_weworkremotely_fetch)

    exit_code = cli.main(
        [
            "collect",
            "weworkremotely",
            "--limit",
            "1",
            "--database",
            str(database_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "We Work Remotely" in captured.out
    assert "Fetched:  1" in captured.out
    assert "Inserted: 1" in captured.out
    assert "Updated:  0" in captured.out

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, source_job_id, title FROM jobs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["source"] == "weworkremotely"
    assert (
        rows[0]["source_job_id"]
        == "https://weworkremotely.com/remote-jobs/python-engineer"
    )
    assert rows[0]["title"] == "Python Engineer"


def test_collect_second_run_updates_no_duplicates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "weworkremotely", _fake_weworkremotely_fetch)

    args = [
        "collect",
        "weworkremotely",
        "--limit",
        "1",
        "--database",
        str(database_path),
    ]

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
                collected_at="2026-08-02T08:00:00+00:00",
            )
        ][:limit]

    monkeypatch.setitem(cli.COLLECTORS, "arbeitnow", fake_arbeitnow_fetch)
    monkeypatch.setitem(cli.COLLECTORS, "weworkremotely", _fake_weworkremotely_fetch)

    assert (
        cli.main(
            [
                "collect",
                "arbeitnow",
                "--limit",
                "1",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            [
                "collect",
                "weworkremotely",
                "--limit",
                "1",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    capsys.readouterr()

    with connect(database_path) as connection:
        rows = connection.execute("SELECT source FROM jobs ORDER BY source").fetchall()

    assert [row["source"] for row in rows] == ["arbeitnow", "weworkremotely"]
