"""Tests for application-aware search/rank filtering and annotations."""

import json
from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str = "Berlin",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build things.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _seed(tmp_path: Path, *, grouped: bool = False) -> Path:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-1",
                    title="Python Engineer",
                    company="Acme GmbH",
                ),
                _make_job(
                    source="remoteok",
                    source_job_id="r-1",
                    title="Python Engineer",
                    company="Acme Inc",
                ),
                _make_job(
                    source="weworkremotely",
                    source_job_id="w-1",
                    title="Rust Engineer",
                    company="Systems Co",
                ),
            ],
        )
        if grouped:
            ensure_grouped(connection)
    return db_path


def _job_id(db_path: Path, source_job_id: str) -> int:
    with connect(db_path) as connection:
        return connection.execute(
            "SELECT id FROM jobs WHERE source_job_id = ?", (source_job_id,)
        ).fetchone()["id"]


def _track(db_path: Path, job_id: int, status: str) -> None:
    assert (
        cli.main(
            [
                "application",
                "set",
                str(job_id),
                status,
                "--database",
                str(db_path),
            ]
        )
        == 0
    )


def _write_profile(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "desired_titles": ["Python Engineer"],
                "skills": ["Python"],
                "preferred_locations": ["Berlin"],
                "remote_preference": "remote",
                "excluded_terms": [],
            }
        ),
        encoding="utf-8",
    )


def test_default_search_has_no_application_line(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert cli.main(["search", "Engineer", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert "Application:" not in captured.out
    assert "Python Engineer" in captured.out
    assert "Rust Engineer" in captured.out


def test_flat_untracked_excludes_tracked(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert (
        cli.main(["search", "Engineer", "--untracked", "--database", str(db_path)]) == 0
    )
    captured = capsys.readouterr()
    assert "Python Engineer" in captured.out
    assert "Rust Engineer" in captured.out
    assert "Application: Untracked" in captured.out


def test_flat_status_shows_only_selected(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert (
        cli.main(
            ["search", "Engineer", "--status", "applied", "--database", str(db_path)]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application: Applied" in captured.out
    assert "Rust Engineer" not in captured.out


def test_flat_exclude_status_keeps_untracked(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert (
        cli.main(
            [
                "search",
                "Engineer",
                "--exclude-status",
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application: Untracked" in captured.out
    assert "Rust Engineer" in captured.out


def test_default_grouped_search_has_no_application_line(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert (
        cli.main(["search", "Engineer", "--grouped", "--database", str(db_path)]) == 0
    )
    captured = capsys.readouterr()
    assert "Application:" not in captured.out
    assert "2 listings grouped" in captured.out


def test_grouped_tracked_on_representative(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert (
        cli.main(
            [
                "search",
                "Engineer",
                "--grouped",
                "--status",
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application: Applied" in captured.out
    assert "Rust Engineer" not in captured.out


def test_grouped_tracked_via_other_member(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    r1 = _job_id(db_path, "r-1")
    _track(db_path, r1, "interview")

    assert (
        cli.main(
            [
                "search",
                "Engineer",
                "--grouped",
                "--status",
                "interview",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application: Interview (via job" in captured.out
    assert str(r1) in captured.out
    assert "remoteok" in captured.out


def test_grouped_untracked_drops_tracked_group(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert (
        cli.main(
            [
                "search",
                "Engineer",
                "--grouped",
                "--untracked",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Rust Engineer" in captured.out
    assert "2 listings grouped" not in captured.out


def test_grouped_exclude_status_drops_tracked_group(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")

    assert (
        cli.main(
            [
                "search",
                "Engineer",
                "--grouped",
                "--exclude-status",
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Rust Engineer" in captured.out
    assert "2 listings grouped" not in captured.out


def test_grouped_source_filter_combined_with_tracking(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    r1 = _job_id(db_path, "r-1")
    _track(db_path, r1, "applied")

    assert (
        cli.main(
            [
                "search",
                "Engineer",
                "--grouped",
                "--source",
                "remoteok",
                "--status",
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application: Applied" in captured.out

    assert (
        cli.main(
            [
                "search",
                "Engineer",
                "--grouped",
                "--source",
                "arbeitnow",
                "--status",
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Found 0 matching groups" in captured.out


def test_rank_flat_status_annotation(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)

    assert (
        cli.main(
            [
                "rank",
                "Engineer",
                "--profile",
                str(profile_path),
                "--status",
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application: Applied" in captured.out
    assert "[Score " in captured.out
    assert "Rust Engineer" not in captured.out


def test_rank_grouped_status_annotation(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)

    assert (
        cli.main(
            [
                "rank",
                "Engineer",
                "--profile",
                str(profile_path),
                "--grouped",
                "--status",
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application: Applied" in captured.out
    assert "2 listings grouped" in captured.out
    assert "Rust Engineer" not in captured.out


def test_rank_default_output_unchanged(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path, grouped=True)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)

    assert (
        cli.main(
            [
                "rank",
                "Engineer",
                "--profile",
                str(profile_path),
                "--grouped",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "Application:" not in captured.out
    assert "Rust Engineer" in captured.out
    assert "[Score " in captured.out


def test_scores_unchanged_when_filter_active(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    a1 = _job_id(db_path, "a-1")
    _track(db_path, a1, "applied")
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)

    cli.main(
        ["rank", "Engineer", "--profile", str(profile_path), "--database", str(db_path)]
    )
    unfiltered = capsys.readouterr().out

    cli.main(
        [
            "rank",
            "Engineer",
            "--profile",
            str(profile_path),
            "--exclude-status",
            "withdrawn",
            "--database",
            str(db_path),
        ]
    )
    filtered = capsys.readouterr().out

    def scores(output: str) -> list[str]:
        return [line for line in output.splitlines() if "[Score " in line]

    assert scores(unfiltered) == scores(filtered)


@pytest.mark.parametrize(
    "flags",
    [
        ["--untracked", "--status", "applied"],
        ["--untracked", "--exclude-status", "applied"],
        ["--status", "applied", "--exclude-status", "rejected"],
    ],
)
def test_search_rejects_conflicting_flags(tmp_path: Path, flags: list[str]) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["search", "Engineer", *flags, "--database", str(db_path)])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "flags",
    [
        ["--untracked", "--status", "applied"],
        ["--status", "applied", "--exclude-status", "rejected"],
    ],
)
def test_rank_rejects_conflicting_flags(tmp_path: Path, flags: list[str]) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "rank",
                "Engineer",
                "--profile",
                str(profile_path),
                *flags,
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2
