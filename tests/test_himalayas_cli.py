"""CLI tests for the Himalayas collector."""

from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect

_SAMPLE_JOB = CollectedJob(
    source="himalayas",
    source_job_id="https://himalayas.app/companies/micro1/jobs/talent-operations-manager",
    title="Talent Operations Manager",
    company="micro1",
    location=None,
    description="<p><strong>Job Title:</strong> Talent Operations Manager</p>",
    url="https://himalayas.app/companies/micro1/jobs/talent-operations-manager",
    employment_type="Full Time",
    remote_status="remote",
    published_at="2026-08-05T00:09:18+00:00",
    collected_at="2026-08-05T01:00:00+00:00",
)


def _fake_himalayas_fetch(
    *, limit: int = 25, timeout: float = 30.0
) -> list[CollectedJob]:
    return [_SAMPLE_JOB][:limit]


def test_collect_himalayas_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "himalayas", _fake_himalayas_fetch)

    exit_code = cli.main(
        ["collect", "himalayas", "--limit", "1", "--database", str(database_path)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Himalayas" in captured.out
    assert "Fetched:  1" in captured.out
    assert "Inserted: 1" in captured.out
    assert "Updated:  0" in captured.out

    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT source, source_job_id, title FROM jobs"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["source"] == "himalayas"
    assert (
        rows[0]["source_job_id"]
        == "https://himalayas.app/companies/micro1/jobs/talent-operations-manager"
    )
    assert rows[0]["title"] == "Talent Operations Manager"


def test_collect_second_run_updates_no_duplicates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setitem(cli.COLLECTORS, "himalayas", _fake_himalayas_fetch)

    args = ["collect", "himalayas", "--limit", "1", "--database", str(database_path)]

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
                title="Talent Operations Manager",
                company="Other Corp",
                location="Athens",
                description="Run operations.",
                url="https://arbeitnow.com/job/1",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-08-01T10:00:00Z",
                collected_at="2026-08-05T01:00:00+00:00",
            )
        ][:limit]

    monkeypatch.setitem(cli.COLLECTORS, "arbeitnow", fake_arbeitnow_fetch)
    monkeypatch.setitem(cli.COLLECTORS, "himalayas", _fake_himalayas_fetch)

    assert (
        cli.main(
            ["collect", "arbeitnow", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(
            ["collect", "himalayas", "--limit", "1", "--database", str(database_path)]
        )
        == 0
    )
    capsys.readouterr()

    with connect(database_path) as connection:
        rows = connection.execute("SELECT source FROM jobs ORDER BY source").fetchall()

    assert [row["source"] for row in rows] == ["arbeitnow", "himalayas"]
