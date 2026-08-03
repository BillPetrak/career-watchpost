"""Tests for the group CLI command and grouped search/rank output."""

import json
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema


def _seed(connection) -> None:
    jobs = [
        CollectedJob(
            source="arbeitnow",
            source_job_id="a-1",
            title="Python Engineer",
            company="Acme GmbH",
            location="Berlin",
            description="Build Python backends.",
            url="https://example.com/a",
            employment_type="Full-time",
            remote_status="remote",
            published_at="2026-07-29T10:00:00Z",
            collected_at="2026-08-01T08:00:00+00:00",
        ),
        CollectedJob(
            source="remoteok",
            source_job_id="r-1",
            title="Python Engineer",
            company="Acme Inc",
            location="Berlin, Germany",
            description="Build Python backends.",
            url="https://example.com/r",
            employment_type="Full-time",
            remote_status="remote",
            published_at="2026-07-28T10:00:00Z",
            collected_at="2026-08-01T08:00:00+00:00",
        ),
        CollectedJob(
            source="weworkremotely",
            source_job_id="w-1",
            title="Rust Engineer",
            company="Systems Co",
            location="Athens",
            description="Build systems software.",
            url="https://example.com/w",
            employment_type="Contract",
            remote_status="onsite_or_unspecified",
            published_at="2026-07-27T10:00:00Z",
            collected_at="2026-08-01T08:00:00+00:00",
        ),
    ]
    save_jobs(connection, jobs)


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


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed(conn)
    return db_path


def test_group_regroup_reports_summary(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)

    from careeros.cli import run_group_regroup

    exit_code = run_group_regroup(db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Total jobs:" in captured.out
    assert "3" in captured.out
    assert "Groups:" in captured.out


def test_group_explain_shows_groups(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    with connect(db_path) as conn:
        ensure_grouped(conn)

    from careeros.cli import run_group_explain

    exit_code = run_group_explain(db_path, group_id=None, limit=20)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Canonical groups" in captured.out
    assert "company+title match" in captured.out
    assert "arbeitnow" in captured.out
    assert "remoteok" in captured.out


def test_group_explain_filters_by_group_id(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    with connect(db_path) as conn:
        ensure_grouped(conn)
        from careeros.database.groups import list_group_infos

        group_id = list_group_infos(conn)[0].group_id

    from careeros.cli import run_group_explain

    exit_code = run_group_explain(db_path, group_id=group_id, limit=20)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Group {group_id}" in captured.out


def test_group_explain_no_groups(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)

    from careeros.cli import run_group_explain

    exit_code = run_group_explain(db_path, group_id=None, limit=20)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No groups found." in captured.out


def test_search_grouped_collapses_duplicates(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    with connect(db_path) as conn:
        ensure_grouped(conn)

    from careeros.cli import run_search

    exit_code = run_search("Python", db_path, grouped=True)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "matching group" in captured.out
    assert "2 listings grouped" in captured.out
    assert "arbeitnow, remoteok" in captured.out


def test_search_without_grouped_shows_all(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    with connect(db_path) as conn:
        ensure_grouped(conn)

    from careeros.cli import run_search

    exit_code = run_search("Python", db_path, grouped=False)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "matching job" in captured.out
    assert "2 listings grouped" not in captured.out


def test_rank_grouped_collapses_duplicates(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    with connect(db_path) as conn:
        ensure_grouped(conn)

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path, grouped=True)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "group" in captured.out
    assert "[Score " in captured.out
    assert "2 listings grouped" in captured.out


def test_group_regroup_via_main(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)

    from careeros.cli import main

    exit_code = main(["group", "regroup", "--database", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Regrouping complete" in captured.out


def test_group_explain_via_main(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    with connect(db_path) as conn:
        ensure_grouped(conn)

    from careeros.cli import main

    exit_code = main(["group", "explain", "--database", str(db_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Canonical groups" in captured.out


def test_group_explain_invalid_limit(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)

    from careeros.cli import main

    with pytest.raises(SystemExit):
        main(["group", "explain", "--limit", "0", "--database", str(db_path)])


def test_group_command_requires_subcommand(tmp_path: Path) -> None:
    from careeros.cli import main

    with pytest.raises(SystemExit):
        main(["group"])
