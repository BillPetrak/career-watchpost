"""CLI tests for the read-only source statistics command (careeros source stats)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock


def _make_job(
    *,
    source: str,
    source_job_id: str,
    location: str | None = "Berlin",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title="Python Engineer",
        company="Acme GmbH",
        location=location,
        description="Build things with Python.",
        url="https://example.com/job",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _seed(tmp_path: Path) -> Path:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(source="arbeitnow", source_job_id="a-1", location=None),
                _make_job(source="arbeitnow", source_job_id="a-2", location="Munich"),
                _make_job(source="arbeitnow", source_job_id="a-3"),
                _make_job(source="arbeitnow", source_job_id="a-4"),
                _make_job(source="jobicy", source_job_id="j-1"),
            ],
        )
    return database_path


def test_source_command_is_registered(
    capsys: pytest.CaptureFixture,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["source", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "stats" in captured.out


def test_source_command_requires_subcommand(
    capsys: pytest.CaptureFixture,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["source"])
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_source_stats_missing_database_returns_empty_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = tmp_path / "missing.sqlite3"
    exit_code = cli.main(["source", "stats", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.startswith("Source statistics")
    for source in COLLECTORS:
        assert f"{source}: 0 jobs, never groupable: 0" in captured.out
    assert not database_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_cli_source_stats_missing_database_returns_empty_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = tmp_path / "missing.sqlite3"
    exit_code = cli.main(
        ["source", "stats", "--database", str(database_path), "--format", "json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    sources = json.loads(captured.out)["sources"]
    assert [entry["source"] for entry in sources] == list(COLLECTORS)
    for entry in sources:
        assert entry["total_jobs"] == 0
        assert entry["never_groupable_jobs"] == 0
        assert len(entry["fields"]) == 8
    assert not database_path.exists()


def test_cli_source_stats_text_counts_and_coverage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    exit_code = cli.main(["source", "stats", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "arbeitnow: 4 jobs, never groupable: 0" in captured.out
    assert "jobicy: 1 jobs, never groupable: 0" in captured.out
    assert captured.out.index("arbeitnow:") < captured.out.index("jobicy:")
    assert "location 1" in captured.out
    assert "location 75%" in captured.out
    assert "company 100%" in captured.out
    assert "description 100%" in captured.out


def test_cli_source_stats_json_structure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    exit_code = cli.main(
        ["source", "stats", "--database", str(database_path), "--format", "json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    sources = json.loads(captured.out)["sources"]
    assert [entry["source"] for entry in sources] == list(COLLECTORS)
    by_source = {entry["source"]: entry for entry in sources}
    arbeitnow = by_source["arbeitnow"]
    assert arbeitnow["total_jobs"] == 4
    assert arbeitnow["never_groupable_jobs"] == 0
    fields = {field["field"]: field for field in arbeitnow["fields"]}
    assert fields["location"]["missing_count"] == 1
    assert fields["location"]["provided_count"] == 3
    assert fields["location"]["coverage_pct"] == 75
    assert fields["company"]["coverage_pct"] == 100
    assert by_source["himalayas"]["total_jobs"] == 0


def test_cli_source_stats_filters_by_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    exit_code = cli.main(
        ["source", "stats", "--database", str(database_path), "--source", "arbeitnow"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "arbeitnow: 4 jobs, never groupable: 0" in captured.out
    assert "jobicy:" not in captured.out


def test_cli_source_stats_rejects_unknown_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "source",
                "stats",
                "--database",
                str(database_path),
                "--source",
                "not-a-source",
            ]
        )
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_source_stats_rejects_unknown_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            ["source", "stats", "--database", str(database_path), "--format", "xml"]
        )
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_source_stats_is_deterministic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    assert cli.main(["source", "stats", "--database", str(database_path)]) == 0
    first_text = capsys.readouterr().out
    assert cli.main(["source", "stats", "--database", str(database_path)]) == 0
    second_text = capsys.readouterr().out
    assert first_text == second_text

    assert (
        cli.main(
            ["source", "stats", "--database", str(database_path), "--format", "json"]
        )
        == 0
    )
    first_json = capsys.readouterr().out
    assert (
        cli.main(
            ["source", "stats", "--database", str(database_path), "--format", "json"]
        )
        == 0
    )
    second_json = capsys.readouterr().out
    assert first_json == second_json


def test_cli_source_stats_does_not_mutate_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    with connect(database_path) as connection:

        def snapshot(table: str) -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]

        tables = [
            "jobs",
            "collection_runs",
            "applications",
            "application_events",
        ]
        before = {table: snapshot(table) for table in tables}

    exit_code = cli.main(
        ["source", "stats", "--database", str(database_path), "--format", "json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["sources"]

    with connect(database_path) as connection:
        for table in tables:
            assert snapshot(table) == before[table], table


def test_cli_source_stats_read_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = tmp_path / "corrupt.sqlite3"
    database_path.write_text("this is not a sqlite database", encoding="utf-8")
    exit_code = cli.main(["source", "stats", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Source stats failed" in captured.err


def test_cli_source_stats_works_while_collection_lock_held(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    with collection_lock(database_path, command="collect due"):
        exit_code = cli.main(["source", "stats", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "arbeitnow: 4 jobs, never groupable: 0" in captured.out
