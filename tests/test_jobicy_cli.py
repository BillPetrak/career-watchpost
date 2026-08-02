"""CLI tests for the Jobicy collector."""

from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect

_SAMPLE_JOB = CollectedJob(
    source="jobicy",
    source_job_id="150110",
    title="Customer Service Representative",
    company="NTT DATA",
    location="Canada",
    description="<p>Build things.</p>",
    url="https://jobicy.com/jobs/150110-customer-service-representative-4",
    employment_type="Full-Time",
    remote_status="remote",
    published_at="2026-08-02T12:10:14+00:00",
    collected_at="2026-08-02T13:00:00+00:00",
)


def _fake_jobicy_fetch(*, limit: int = 25, timeout: float = 30.0) -> list[CollectedJob]:
    return [_SAMPLE_JOB][:limit]


def test_collect_jobicy_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "jobicy", _fake_jobicy_fetch)

    exit_code = cli.main(
        ["collect", "jobicy", "--limit", "1", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Jobicy" in captured.out
    assert "Fetched:  1" in captured.out
    assert "Inserted: 1" in captured.out
    assert "Updated:  0" in captured.out

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, source_job_id, title FROM jobs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["source"] == "jobicy"
    assert rows[0]["source_job_id"] == "150110"
    assert rows[0]["title"] == "Customer Service Representative"


def test_collect_second_run_updates_no_duplicates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "jobicy", _fake_jobicy_fetch)

    args = ["collect", "jobicy", "--limit", "1", "--database", str(database_path)]

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
                collected_at="2026-08-02T13:00:00+00:00",
            )
        ][:limit]

    monkeypatch.setitem(cli.COLLECTORS, "arbeitnow", fake_arbeitnow_fetch)
    monkeypatch.setitem(cli.COLLECTORS, "jobicy", _fake_jobicy_fetch)

    assert (
        cli.main(
            ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            ["collect", "jobicy", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()

    with connect(database_path) as connection:
        rows = connection.execute("SELECT source FROM jobs ORDER BY source").fetchall()

    assert [row["source"] for row in rows] == ["arbeitnow", "jobicy"]
