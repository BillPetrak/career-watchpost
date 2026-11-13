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
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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
from careeros.export import writer
from careeros.locking import collection_lock
from careeros.review.service import set_decision

PROFILE = (
    Path(__file__).resolve().parents[1] / "config" / "candidate_profile.example.json"
)


def _private_profile(tmp_path: Path) -> Path:
    """Copy the committed example into a disposable owner-only profile."""
    path = tmp_path / "profile.json"
    path.write_bytes(PROFILE.read_bytes())
    path.chmod(0o600)
    return path


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
                str(_private_profile(tmp_path)),
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
                str(_private_profile(tmp_path)),
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
                str(_private_profile(tmp_path)),
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
                str(_private_profile(tmp_path)),
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
                str(_private_profile(tmp_path)),
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
                str(_private_profile(tmp_path)),
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
                str(_private_profile(tmp_path)),
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
                str(_private_profile(tmp_path)),
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


@pytest.mark.parametrize("mask", [0o000, 0o022])
def test_output_file_is_private_independent_of_umask(tmp_path: Path, mask: int) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / f"export-{mask:o}.json"
    old_mask = os.umask(mask)
    try:
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
    finally:
        os.umask(old_mask)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".csv"])
def test_output_file_formats_preserve_serialization(
    tmp_path: Path, suffix: str
) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / f"export{suffix}"
    fmt = suffix[1:]
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--format",
                fmt,
                "--output",
                str(output),
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    text = output.read_text(encoding="utf-8")
    if fmt == "json":
        assert json.loads(text)["total"] == 4
    elif fmt == "jsonl":
        assert len(text.splitlines()) == 4
    else:
        assert len(list(csv.reader(text.splitlines()))) == 5


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "fifo", "mode644", "mode666"]
)
def test_unsafe_existing_output_is_rejected(tmp_path: Path, kind: str) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / "unsafe"
    referent: Path | None = None
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("secret", encoding="utf-8")
        output.symlink_to(target)
        referent = target
    elif kind == "hardlink":
        output.write_text("secret", encoding="utf-8")
        referent = tmp_path / "second-link"
        os.link(output, referent)
        output.chmod(0o600)
    elif kind == "directory":
        output.mkdir()
    elif kind == "fifo":
        os.mkfifo(output)
    else:
        output.write_text("secret", encoding="utf-8")
        output.chmod(0o644 if kind == "mode644" else 0o666)
    before = referent.read_bytes() if referent is not None else None
    before_stat = output.lstat() if output.exists() or output.is_symlink() else None
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
        == 1
    )
    if referent is not None:
        assert referent.read_bytes() == before
    if before_stat is not None:
        after_stat = output.lstat()
        assert (after_stat.st_dev, after_stat.st_ino) == (
            before_stat.st_dev,
            before_stat.st_ino,
        )
    assert not list(tmp_path.glob(".careeros-export-*.tmp"))


def test_output_rejects_unsafe_owner_and_symlinked_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / "owned.json"
    output.write_text("secret", encoding="utf-8")
    output.chmod(0o600)
    monkeypatch.setattr(writer.os, "geteuid", lambda: output.stat().st_uid + 1)
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
        == 1
    )

    monkeypatch.undo()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    assert (
        cli.main(
            [
                "export",
                "jobs",
                "--output",
                str(link_parent / "x.json"),
                "--database",
                str(database_path),
            ]
        )
        == 1
    )


def test_target_owner_validation_is_independent_of_parent_validation() -> None:
    info = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_uid=os.geteuid() + 1,
        st_nlink=1,
        st_dev=1,
        st_ino=1,
        st_size=1,
        st_mtime_ns=1,
        st_ctime_ns=1,
    )
    with pytest.raises(writer.ExportError, match="owned"):
        writer._validate_target(info)  # type: ignore[arg-type]


def test_prepublication_directory_fsync_failure_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / "export.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o600)
    real_fsync = writer.os.fsync
    calls = 0

    def fail_parent_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected parent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(writer.os, "fsync", fail_parent_fsync)
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
        == 1
    )
    assert output.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".careeros-export-*.tmp"))


def test_failed_export_preserves_target_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / "export.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o600)

    def fail_temp_fsync(fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(writer.os, "fsync", fail_temp_fsync)
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
        == 1
    )
    assert output.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".careeros-export-*.tmp"))


@pytest.mark.parametrize("failure", ["write", "flush"])
def test_partial_write_and_flush_failures_clean_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    output = tmp_path / "export.json"
    real_fdopen = writer.os.fdopen

    class FailingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, text):
            if failure == "write":
                self.stream.write(text[:1])
                raise OSError("injected write failure")
            return self.stream.write(text)

        def flush(self):
            if failure == "flush":
                raise OSError("injected flush failure")
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    monkeypatch.setattr(
        writer.os,
        "fdopen",
        lambda *args, **kwargs: FailingStream(real_fdopen(*args, **kwargs)),
    )
    with pytest.raises(Exception, match="injected"):
        writer.write_file_export(output, "sensitive output", overwrite=False)
    assert not output.exists()
    assert not list(tmp_path.glob(".careeros-export-*.tmp"))


def test_publication_before_effect_failure_preserves_old_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "export.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o600)
    monkeypatch.setattr(
        writer.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("before replace")),
    )
    with pytest.raises(Exception, match="before replace"):
        writer.write_file_export(output, "new", overwrite=True)
    assert output.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".careeros-export-*.tmp"))


def test_postpublication_directory_fsync_failure_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "export.json"
    calls = 0
    real_fsync = writer.os.fsync

    def fail_after_publication(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("directory durability unavailable")
        real_fsync(fd)

    monkeypatch.setattr(writer.os, "fsync", fail_after_publication)
    assert writer.write_file_export(output, "new", overwrite=False) == 0
    assert output.read_text(encoding="utf-8") == "new"


def test_creation_target_appearance_race_is_rejected_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "export.json"
    real_link = writer.os.link

    def competitor_then_link(*args, **kwargs):
        output.write_text("competitor", encoding="utf-8")
        output.chmod(0o600)
        return real_link(*args, **kwargs)

    monkeypatch.setattr(writer.os, "link", competitor_then_link)
    with pytest.raises(writer.ExportError):
        writer.write_file_export(output, "new", overwrite=False)
    assert output.read_text(encoding="utf-8") == "competitor"
    assert not list(tmp_path.glob(".careeros-export-*.tmp"))


def test_replacement_change_race_is_rejected_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "export.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o600)
    real_entry = writer._entry
    calls = 0

    def changed_entry(fd, name):
        nonlocal calls
        calls += 1
        value = real_entry(fd, name)
        if calls == 2:
            output.write_text("changed", encoding="utf-8")
            output.chmod(0o600)
            value = real_entry(fd, name)
        return value

    monkeypatch.setattr(writer, "_entry", changed_entry)
    with pytest.raises(writer.ExportError, match="changed"):
        writer.write_file_export(output, "new", overwrite=True)
    assert output.read_text(encoding="utf-8") == "changed"
    assert not list(tmp_path.glob(".careeros-export-*.tmp"))


def test_publication_then_error_is_reported_successfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = _seed(tmp_path)
    output = tmp_path / "export.json"
    output.write_text("old", encoding="utf-8")
    output.chmod(0o600)
    replace = writer.os.replace

    def replace_then_raise(*args, **kwargs):
        replace(*args, **kwargs)
        raise OSError("injected after publication")

    monkeypatch.setattr(writer.os, "replace", replace_then_raise)
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
    assert json.loads(output.read_text(encoding="utf-8"))["total"] == 4


def test_ranked_and_review_file_exports(tmp_path: Path) -> None:
    database_path = _seed(tmp_path)
    profile = _private_profile(tmp_path)
    ranked = tmp_path / "ranked.json"
    review = tmp_path / "review.json"
    assert (
        cli.main(
            [
                "export",
                "ranked",
                "--query",
                "python",
                "--profile",
                str(profile),
                "--output",
                str(ranked),
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "export",
                "review",
                "--profile",
                str(profile),
                "--output",
                str(review),
                "--database",
                str(database_path),
            ]
        )
        == 0
    )
    assert isinstance(json.loads(ranked.read_text(encoding="utf-8")), dict)
    assert isinstance(json.loads(review.read_text(encoding="utf-8")), dict)


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
