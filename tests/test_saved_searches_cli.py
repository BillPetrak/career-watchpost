"""CLI integration tests for the saved-search subcommands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock


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
        description="Build things with Python.",
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
                    title="Rust Engineer",
                    company="Systems Co",
                ),
            ],
        )
        if grouped:
            ensure_grouped(connection)
    return db_path


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
    path.chmod(0o600)


def _rows(db_path: Path, table: str) -> list[dict]:
    with connect(db_path) as connection:
        return [
            dict(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
        ]


def _create_search(
    tmp_path: Path,
    *,
    name: str = "engineer",
    query: str = "Engineer",
    mode: str = "search",
    profile: bool = False,
    extra: list[str] | None = None,
) -> tuple[Path, int]:
    db_path = tmp_path / "careeros.sqlite3"
    profile_path = tmp_path / "profile.json"
    args = ["saved-search", "create", name, query, "--mode", mode]
    if profile:
        _write_profile(profile_path)
        args.append("--profile")
        args.append(str(profile_path))
    if extra:
        args.extend(extra)
    args.extend(["--database", str(db_path)])
    exit_code = cli.main(args)
    return db_path, exit_code


def test_create_search_mode(tmp_path: Path, capsys) -> None:
    db_path, exit_code = _create_search(tmp_path)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Created saved search 'engineer' (mode=search)." in captured.out
    assert _rows(db_path, "saved_searches")[0]["query"] == "Engineer"


def test_create_rank_mode_requires_profile(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    exit_code = cli.main(
        [
            "saved-search",
            "create",
            "top",
            "Engineer",
            "--mode",
            "rank",
            "--profile",
            str(profile_path),
            "--min-score",
            "80",
            "--database",
            str(db_path),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Created saved search 'top' (mode=rank)." in captured.out
    row = _rows(db_path, "saved_searches")[0]
    assert row["profile_path"] == str(profile_path)
    assert row["min_score"] == 80


def test_missing_mode_is_parse_error(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "saved-search",
                "create",
                "engineer",
                "Engineer",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_search_mode_rejects_profile(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "saved-search",
                "create",
                "engineer",
                "Engineer",
                "--mode",
                "search",
                "--profile",
                "/x/p.json",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_rank_mode_without_profile_is_parse_error(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "saved-search",
                "create",
                "engineer",
                "Engineer",
                "--mode",
                "rank",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_search_mode_rejects_min_score(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "saved-search",
                "create",
                "engineer",
                "Engineer",
                "--mode",
                "search",
                "--min-score",
                "80",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_create_conflicting_status_flags(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "saved-search",
                "create",
                "engineer",
                "Engineer",
                "--mode",
                "search",
                "--status",
                "applied",
                "--exclude-status",
                "rejected",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_create_empty_name_is_validation_error(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    exit_code = cli.main(
        [
            "saved-search",
            "create",
            "   ",
            "Engineer",
            "--mode",
            "search",
            "--database",
            str(db_path),
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Failed to create saved search" in captured.err


def test_create_duplicate_is_error(tmp_path: Path, capsys) -> None:
    db_path, _ = _create_search(tmp_path)
    exit_code = cli.main(
        [
            "saved-search",
            "create",
            "ENGINEER",
            "Engineer",
            "--mode",
            "search",
            "--database",
            str(db_path),
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "already exists" in captured.err


def test_list_empty(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    assert cli.main(["saved-search", "list", "--database", str(db_path)]) == 0
    assert "No saved searches." in capsys.readouterr().out


def test_list_populated_and_ordered(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    cli.main(
        [
            "saved-search",
            "create",
            "zzz",
            "Engineer",
            "--mode",
            "search",
            "--database",
            str(db_path),
        ]
    )
    cli.main(
        [
            "saved-search",
            "create",
            "aaa",
            "Rust",
            "--mode",
            "search",
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()
    assert cli.main(["saved-search", "list", "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    output = captured.out
    assert output.index("aaa") < output.index("zzz")
    assert "mode=search" in output


def test_show_found(tmp_path: Path, capsys) -> None:
    _create_search(
        tmp_path,
        query="Engineer",
        mode="rank",
        profile=True,
        extra=["--location", "Berlin"],
    )
    db_path = _seed(tmp_path)
    exit_code = cli.main(
        ["saved-search", "show", "engineer", "--database", str(db_path)]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Saved search: engineer" in captured.out
    assert "Mode:         rank" in captured.out
    assert "Location:     Berlin" in captured.out
    assert "Min score:    none" in captured.out


def test_show_missing(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    assert cli.main(["saved-search", "show", "nope", "--database", str(db_path)]) == 1
    assert "No saved search named 'nope'" in capsys.readouterr().err


def test_run_search_is_byte_identical_to_search_command(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    cli.main(
        [
            "saved-search",
            "create",
            "engineer",
            "Engineer",
            "--mode",
            "search",
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()

    cli.main(["search", "Engineer", "--database", str(db_path)])
    explicit = capsys.readouterr().out

    cli.main(["saved-search", "run", "engineer", "--database", str(db_path)])
    run_out = capsys.readouterr().out

    assert run_out == explicit


def test_run_rank_is_byte_identical_to_rank_command(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    cli.main(
        [
            "saved-search",
            "create",
            "ranker",
            "Engineer",
            "--mode",
            "rank",
            "--profile",
            str(profile_path),
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()

    cli.main(
        ["rank", "Engineer", "--profile", str(profile_path), "--database", str(db_path)]
    )
    explicit = capsys.readouterr().out

    cli.main(["saved-search", "run", "ranker", "--database", str(db_path)])
    run_out = capsys.readouterr().out

    assert run_out == explicit


def test_run_missing(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    assert cli.main(["saved-search", "run", "nope", "--database", str(db_path)]) == 1
    assert "No saved search named 'nope'" in capsys.readouterr().err


def test_update_partial_type(tmp_path: Path, capsys) -> None:
    db_path, _ = _create_search(tmp_path)
    exit_code = cli.main(
        [
            "saved-search",
            "update",
            "engineer",
            "--location",
            "Hamburg",
            "--no-grouped",
            "--database",
            str(db_path),
        ]
    )
    assert exit_code == 0
    capsys.readouterr()
    row = _rows(db_path, "saved_searches")[0]
    assert row["location"] == "Hamburg"


def test_update_noop(tmp_path: Path, capsys) -> None:
    db_path, _ = _create_search(tmp_path)
    exit_code = cli.main(
        ["saved-search", "update", "engineer", "--database", str(db_path)]
    )
    assert exit_code == 0
    assert "No changes to saved search 'engineer'." in capsys.readouterr().out


def test_update_rejects_mode_change(tmp_path: Path) -> None:
    db_path, _ = _create_search(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "saved-search",
                "update",
                "engineer",
                "--mode",
                "rank",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_update_conflicting_flags(tmp_path: Path) -> None:
    db_path, _ = _create_search(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "saved-search",
                "update",
                "engineer",
                "--grouped",
                "--no-grouped",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_update_clear_status_filters(tmp_path: Path, capsys) -> None:
    db_path, _ = _create_search(tmp_path, extra=["--status", "applied"])
    capsys.readouterr()
    assert (
        cli.main(
            [
                "saved-search",
                "update",
                "engineer",
                "--clear-status-filters",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    row = _rows(db_path, "saved_searches")[0]
    assert row["include_statuses"] is None


def test_update_cannot_clear_rank_profile(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    cli.main(
        [
            "saved-search",
            "create",
            "ranker",
            "Engineer",
            "--mode",
            "rank",
            "--profile",
            str(profile_path),
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()
    exit_code = cli.main(
        [
            "saved-search",
            "update",
            "ranker",
            "--clear-profile",
            "--database",
            str(db_path),
        ]
    )
    assert exit_code == 1
    assert "Failed to update saved search" in capsys.readouterr().err


def test_update_missing(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    assert (
        cli.main(
            [
                "saved-search",
                "update",
                "nope",
                "--query",
                "x",
                "--database",
                str(db_path),
            ]
        )
        == 1
    )
    assert "No saved search named 'nope'" in capsys.readouterr().err


def test_rename_and_delete(tmp_path: Path, capsys) -> None:
    db_path, _ = _create_search(tmp_path)
    assert (
        cli.main(
            ["saved-search", "rename", "engineer", "dev", "--database", str(db_path)]
        )
        == 0
    )
    assert "Renamed saved search 'engineer' to 'dev'." in capsys.readouterr().out

    assert cli.main(["saved-search", "delete", "dev", "--database", str(db_path)]) == 0
    assert "Deleted saved search 'dev'." in capsys.readouterr().out
    assert _rows(db_path, "saved_searches") == []


def test_rename_to_duplicate(tmp_path: Path, capsys) -> None:
    _create_search(tmp_path, name="alpha")
    _create_search(tmp_path, name="beta")
    db_path, _ = _create_search(tmp_path)
    exit_code = cli.main(
        ["saved-search", "rename", "alpha", "BETA", "--database", str(db_path)]
    )
    assert exit_code == 1
    assert "already exists" in capsys.readouterr().err


def test_delete_missing(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    assert cli.main(["saved-search", "delete", "nope", "--database", str(db_path)]) == 1
    assert "No saved search named 'nope'" in capsys.readouterr().err


def test_read_commands_available_while_locked(tmp_path: Path, capsys) -> None:
    db_path, _ = _create_search(tmp_path)
    with collection_lock(db_path):
        assert cli.main(["saved-search", "list", "--database", str(db_path)]) == 0
        assert (
            cli.main(["saved-search", "show", "engineer", "--database", str(db_path)])
            == 0
        )
        assert (
            cli.main(["saved-search", "run", "engineer", "--database", str(db_path)])
            == 0
        )
    capsys.readouterr()


def test_create_conflicts_with_held_lock(tmp_path: Path, capsys) -> None:
    db_path = _seed(tmp_path)
    with collection_lock(db_path):
        exit_code = cli.main(
            [
                "saved-search",
                "create",
                "engineer",
                "Engineer",
                "--mode",
                "search",
                "--database",
                str(db_path),
            ]
        )
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    assert _rows(db_path, "saved_searches") == []
