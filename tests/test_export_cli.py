"""CLI tests for the deterministic export commands (careeros export).

These tests cover the record contracts (JSON envelope, JSONL, CSV), filters
(query, remote, location, source, employment-type), grouping collapse,
ranking/review scoring output, source statistics, collection history,
truncation metadata, output-file semantics, broken-pipe handling, the
read-only guarantee, and the missing-database behavior.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from careeros import cli
from careeros.applications.service import set_application_status
from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.review.service import set_decision

PROFILE = (
    Path(__file__).resolve().parents[1] / "config" / "candidate_profile.example.json"
)

JOB_COLUMNS = [
    "id",
    "source",
    "source_job_id",
    "title",
    "company",
    "location",
    "description",
    "url",
    "employment_type",
    "remote_status",
    "published_at",
    "collected_at",
    "created_at",
    "updated_at",
    "group_id",
]

GROUP_COLUMNS = [
    "group_id",
    "representative_id",
    "source",
    "title",
    "company",
    "location",
    "description",
    "url",
    "employment_type",
    "remote_status",
    "published_at",
    "collected_at",
    "created_at",
    "updated_at",
    "member_count",
    "members",
    "sources",
    "urls",
]


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str = "Python Engineer",
    location: str | None = "Berlin",
    employment_type: str = "Full-time",
    remote_status: str = "remote",
    description: str = "Build things with Python and Docker.",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Acme GmbH",
        location=location,
        description=description,
        url=f"https://example.com/{source_job_id}",
        employment_type=employment_type,
        remote_status=remote_status,
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
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-2",
                    title="Senior Python Engineer",
                    location="Munich",
                ),
                _make_job(source="jobicy", source_job_id="j-1"),
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-3",
                    title="Frontend Developer",
                    employment_type="Contract",
                    remote_status="onsite_or_unspecified",
                ),
            ],
        )
    return database_path


def _seed_grouped(tmp_path: Path) -> Path:
    database_path = _seed(tmp_path)
    with connect(database_path) as connection:
        ensure_grouped(connection)
    return database_path


def _seed_runs(tmp_path: Path) -> Path:
    database_path = _seed(tmp_path)
    with connect(database_path) as connection:
        for index, (source, succeeded) in enumerate(
            [("arbeitnow", True), ("jobicy", False), ("arbeitnow", True)]
        ):
            started = datetime(2026, 8, index + 1, 9, 0, tzinfo=UTC)
            run_id = insert_run(connection, source=source, started_at=started)
            if succeeded:
                complete_run(
                    connection,
                    run_id=run_id,
                    finished_at=started.replace(minute=5),
                    succeeded=True,
                    fetched=10,
                    inserted=9,
                    updated=1,
                    error=None,
                )
            else:
                complete_run(
                    connection,
                    run_id=run_id,
                    finished_at=started.replace(minute=5),
                    succeeded=False,
                    fetched=10,
                    inserted=0,
                    updated=0,
                    error="boom",
                )
    return database_path


def _load_json(capsys: pytest.CaptureFixture) -> dict:
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- #
# Command registration
# --------------------------------------------------------------------------- #


def test_export_command_is_registered(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["export", "--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    for name in ("jobs", "ranked", "review", "source-stats", "collection-history"):
        assert name in captured.out


def test_export_requires_subcommand(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["export"])
    assert exc_info.value.code == 2


def test_export_negative_limit_rejected(tmp_path: Path) -> None:
    database_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["export", "jobs", "--limit", "-1", "--database", str(database_path)])
    assert exc_info.value.code == 2


def test_export_ranked_requires_query_and_profile(tmp_path: Path) -> None:
    database_path = _seed(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["export", "ranked", "--database", str(database_path)])
    with pytest.raises(SystemExit):
        cli.main(
            [
                "export",
                "ranked",
                "--query",
                "python",
                "--database",
                str(database_path),
            ]
        )


# --------------------------------------------------------------------------- #
# Jobs export
# --------------------------------------------------------------------------- #


def test_jobs_json_envelope_and_column_order(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert cli.main(["export", "jobs", "--database", str(database_path)]) == 0
    data = _load_json(capsys)
    assert list(data) == ["records", "total", "truncated"]
    assert data["total"] == 4
    assert data["truncated"] is False
    assert len(data["records"]) == 4
    first = data["records"][0]
    assert list(first.keys()) == JOB_COLUMNS
    assert first["title"] == "Python Engineer"


def test_jobs_grouped_json_collapses_duplicates(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert (
        cli.main(["export", "jobs", "--grouped", "--database", str(database_path)]) == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 3
    first = data["records"][0]
    assert list(first.keys()) == GROUP_COLUMNS
    assert first["member_count"] == 2
    assert first["members"] == [1, 3]
    assert first["sources"] == ["arbeitnow", "jobicy"]
    assert first["group_id"] is not None
    for record in data["records"]:
        assert record["member_count"] == len(record["members"])
        assert len(record["members"]) == len(record["sources"])


def test_jobs_grouped_jsonl(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--grouped",
                "--format",
                "jsonl",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    lines = capsys.readouterr().out.rstrip("\n").split("\n")
    assert len(lines) == 3
    for line in lines:
        record = json.loads(line)
        assert list(record.keys()) == GROUP_COLUMNS


def test_jobs_csv(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--grouped",
                "--format",
                "csv",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == GROUP_COLUMNS
    assert len(rows) == 4  # header + 3 groups
    members_cell = rows[1][rows[0].index("members")]
    assert json.loads(members_cell) == [1, 3]
    assert rows[1][rows[0].index("group_id")] != ""


def test_jobs_limit_truncation_metadata(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(["export", "jobs", "--limit", "2", "--database", str(database_path)])
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 2
    assert data["total"] == 4
    assert data["truncated"] is True


def test_jobs_limit_zero_means_no_cap(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(["export", "jobs", "--limit", "0", "--database", str(database_path)])
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 4
    assert data["truncated"] is False


def test_jobs_employment_type_filter(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--employment-type",
                "Contract",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 1
    assert data["records"][0]["title"] == "Frontend Developer"


def test_jobs_query_and_source_filters(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--query",
                "python",
                "--source",
                "arbeitnow",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 3
    assert all(record["source"] == "arbeitnow" for record in data["records"])


def test_jobs_grouped_filters_apply_before_grouping(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--grouped",
                "--source",
                "arbeitnow",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    first = data["records"][0]
    assert first["title"] == "Python Engineer"
    assert first["member_count"] == 1
    assert first["members"] == [1]


def test_jobs_remote_and_location_filters(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            ["export", "jobs", "--location", "Munich", "--database", str(database_path)]
        )
        == 0
    )
    data = _load_json(capsys)
    assert [record["title"] for record in data["records"]] == ["Senior Python Engineer"]


def test_jobs_clean_descriptions(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            ["export", "jobs", "--clean-descriptions", "--database", str(database_path)]
        )
        == 0
    )
    data = _load_json(capsys)
    for record in data["records"]:
        assert "\n" not in record["description"]


def test_jobs_flat_group_id_populated(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert cli.main(["export", "jobs", "--database", str(database_path)]) == 0
    data = _load_json(capsys)
    by_id = {record["id"]: record["group_id"] for record in data["records"]}
    assert by_id[1] == by_id[3]
    assert by_id[1] is not None
    assert by_id[2] is None


def test_jobs_deterministic_repeat(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert (
        cli.main(["export", "jobs", "--grouped", "--database", str(database_path)]) == 0
    )
    first = capsys.readouterr().out
    assert (
        cli.main(["export", "jobs", "--grouped", "--database", str(database_path)]) == 0
    )
    second = capsys.readouterr().out
    assert first == second


# --------------------------------------------------------------------------- #
# Ranked export
# --------------------------------------------------------------------------- #


def test_ranked_flat_export(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "ranked",
                "--query",
                "engineer",
                "--profile",
                str(PROFILE),
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 3
    assert [record["rank"] for record in data["records"]] == [1, 2, 3]
    first = data["records"][0]
    assert isinstance(first["score"], int)
    assert isinstance(first["match_score"], int)
    assert 0 <= first["match_score"] <= 100
    assert isinstance(first["reasons"], list)
    assert first["reasons"]
    components = first["score_components"]
    assert components
    assert all(
        set(component) == {"kind", "label", "points", "is_veto"}
        for component in components
    )


def test_ranked_min_score_filter(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "ranked",
                "--query",
                "engineer",
                "--profile",
                str(PROFILE),
                "--min-score",
                "30",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert data["records"]
    assert all(record["match_score"] >= 30 for record in data["records"])


def test_ranked_grouped_export(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "ranked",
                "--query",
                "engineer",
                "--profile",
                str(PROFILE),
                "--grouped",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 2
    assert [record["rank"] for record in data["records"]] == [1, 2]
    assert "members" in data["records"][0]
    assert "score_components" in data["records"][0]


# --------------------------------------------------------------------------- #
# Review export
# --------------------------------------------------------------------------- #


def test_review_export_default_unreviewed(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "review",
                "--profile",
                str(PROFILE),
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert data["records"]
    assert all(record["review_decision"] is None for record in data["records"])
    assert "review_decision" in data["records"][0]


def test_review_export_state_any_includes_decisions(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    with connect(database_path) as connection:
        set_decision(connection, job_id=1, decision="shortlist")
    assert (
        cli.main(
            [
                "export",
                "review",
                "--profile",
                str(PROFILE),
                "--state",
                "any",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    by_id = {
        record["representative_id"]: record["review_decision"]
        for record in data["records"]
    }
    assert by_id[1] == "shortlist"
    assert by_id[2] is None


def test_review_export_excludes_tracked_by_default(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    with connect(database_path) as connection:
        set_application_status(connection, job_id=1, status="applied")
    assert (
        cli.main(
            [
                "export",
                "review",
                "--profile",
                str(PROFILE),
                "--state",
                "any",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    ids = {record["representative_id"] for record in data["records"]}
    assert 1 not in ids
    assert 2 in ids

    assert (
        cli.main(
            [
                "export",
                "review",
                "--profile",
                str(PROFILE),
                "--state",
                "any",
                "--include-tracked",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    ids = {record["representative_id"] for record in data["records"]}
    assert 1 in ids


def test_review_export_no_grouped(tmp_path: Path, capsys) -> None:
    database_path = _seed_grouped(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "review",
                "--profile",
                str(PROFILE),
                "--no-grouped",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 4
    assert "id" in data["records"][0]
    assert "members" not in data["records"][0]


# --------------------------------------------------------------------------- #
# Source statistics export
# --------------------------------------------------------------------------- #


def test_source_stats_export_registry_order(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert cli.main(["export", "source-stats", "--database", str(database_path)]) == 0
    data = _load_json(capsys)
    assert [record["source"] for record in data["records"]] == list(COLLECTORS)
    arbeitnow = next(
        record for record in data["records"] if record["source"] == "arbeitnow"
    )
    assert arbeitnow["total_jobs"] == 3
    assert arbeitnow["source_job_id_missing"] == 0
    assert arbeitnow["source_job_id_coverage_pct"] == 100
    assert "location_missing" in arbeitnow
    assert "remote_status_coverage_pct" in arbeitnow


def test_source_stats_export_source_filter(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "source-stats",
                "--source",
                "jobicy",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert [record["source"] for record in data["records"]] == ["jobicy"]


# --------------------------------------------------------------------------- #
# Collection history export
# --------------------------------------------------------------------------- #


def test_history_export_newest_first(tmp_path: Path, capsys) -> None:
    database_path = _seed_runs(tmp_path)
    assert (
        cli.main(["export", "collection-history", "--database", str(database_path)])
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 3
    assert data["records"][0]["run_id"] == 3
    assert data["records"][2]["run_id"] == 1
    failed = next(record for record in data["records"] if record["succeeded"] == 0)
    assert failed["error"] == "boom"
    assert data["records"][0]["fetched"] == 10


def test_history_export_failed_only(tmp_path: Path, capsys) -> None:
    database_path = _seed_runs(tmp_path)
    assert (
        cli.main(
            [
                "export",
                "collection-history",
                "--failed",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    data = _load_json(capsys)
    assert len(data["records"]) == 1
    assert data["records"][0]["succeeded"] == 0


# --------------------------------------------------------------------------- #
# Output handling
# --------------------------------------------------------------------------- #


def test_missing_database_is_empty_and_never_created(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "missing.sqlite3"
    assert cli.main(["export", "jobs", "--database", str(database_path)]) == 0
    data = _load_json(capsys)
    assert data["records"] == []
    assert data["total"] == 0
    assert not database_path.exists()


def test_missing_database_source_stats_zero_counts(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "missing.sqlite3"
    assert cli.main(["export", "source-stats", "--database", str(database_path)]) == 0
    data = _load_json(capsys)
    assert len(data["records"]) == len(COLLECTORS)
    assert all(record["total_jobs"] == 0 for record in data["records"])
    assert not database_path.exists()


def test_output_file_and_overwrite_semantics(tmp_path: Path, capsys) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / "out.json"
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--output",
                str(output),
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["total"] == 4

    exit_code = cli.main(
        ["export", "jobs", "--output", str(output), "--database", str(database_path)]
    )
    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err

    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--output",
                str(output),
                "--overwrite",
                "--database",
                str(database_path),
            ]
        )
        == 0
    )


def test_output_file_missing_parent_dir(tmp_path: Path) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / "nope" / "out.json"
    exit_code = cli.main(
        ["export", "jobs", "--output", str(output), "--database", str(database_path)]
    )
    assert exit_code == 1


def test_broken_pipe_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _seed(tmp_path)

    class BrokenPipeStream:
        def write(self, text: str) -> int:
            raise BrokenPipeError()

        def flush(self) -> None:
            raise BrokenPipeError()

    monkeypatch.setattr(sys, "stdout", BrokenPipeStream())
    exit_code = cli.main(["export", "jobs", "--database", str(database_path)])
    assert exit_code == 0


# --------------------------------------------------------------------------- #
# Read-only guarantee
# --------------------------------------------------------------------------- #


def test_export_is_read_only(tmp_path: Path) -> None:
    database_path = _seed(tmp_path)
    with connect(database_path) as connection:
        before_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        before_runs = connection.execute(
            "SELECT COUNT(*) FROM collection_runs"
        ).fetchone()[0]
        before_groups = connection.execute(
            "SELECT COUNT(*) FROM job_groups"
        ).fetchone()[0]

    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--grouped",
                "--output",
                str(tmp_path / "export.json"),
                "--database",
                str(database_path),
            ]
        )
        == 0
    )

    with connect(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
            == before_runs
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM job_groups").fetchone()[0]
            == before_groups
        )


def test_export_works_while_collection_lock_held(tmp_path: Path) -> None:
    database_path = _seed(tmp_path)
    with collection_lock(database_path, command="collect due"):
        exit_code = cli.main(["export", "jobs", "--database", str(database_path)])
    assert exit_code == 0
