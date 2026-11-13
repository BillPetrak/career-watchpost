"""Tests for the review CLI command, its exit codes, and locking."""

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
    title: str = "Python Engineer",
    location: str = "Berlin",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Tech Corp",
        location=location,
        description="Build Python backends.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed_jobs(connection, *, count: int = 1) -> int:
    jobs = [
        _make_job(source="arbeitnow", source_job_id="python-dev-1"),
        _make_job(
            source="remoteok",
            source_job_id="frontend-1",
            title="Frontend Developer",
            location="Munich",
        ),
    ][:count]
    save_jobs(connection, jobs)
    ensure_grouped(connection)
    return connection.execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()[
        "id"
    ]


def _write_profile(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def _table_rows(db_path: Path, table: str) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
        ]


def test_review_set_first_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert (
        cli.main(
            [
                "review",
                "set",
                str(job_id),
                "shortlist",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert f"Set decision Shortlist for job {job_id}." in captured.out


def test_review_set_with_note(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert (
        cli.main(
            [
                "review",
                "set",
                str(job_id),
                "dismiss",
                "--note",
                "not a fit",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert f"Set decision Dismiss for job {job_id}." in captured.out
    with connect(db_path) as connection:
        note = connection.execute("SELECT note FROM review_events").fetchone()["note"]
    assert note == "not a fit"


def test_review_set_same_decision_is_noop(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    cli.main(["review", "set", str(job_id), "shortlist", "--database", str(db_path)])
    capsys.readouterr()

    assert (
        cli.main(
            [
                "review",
                "set",
                str(job_id),
                "shortlist",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert f"Job {job_id} is already Shortlist; no change." in captured.out


def test_review_set_transition_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    cli.main(["review", "set", str(job_id), "shortlist", "--database", str(db_path)])
    capsys.readouterr()

    assert (
        cli.main(
            [
                "review",
                "set",
                str(job_id),
                "dismiss",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert f"Updated job {job_id} to Dismiss." in captured.out


def test_review_set_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(
        ["review", "set", "999", "shortlist", "--database", str(db_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_review_set_invalid_decision(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["review", "set", "1", "apply", "--database", str(db_path)])
    assert exc_info.value.code == 2


def test_review_set_invalid_job_id(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["review", "set", "0", "shortlist", "--database", str(db_path)])
    assert exc_info.value.code == 2


def test_review_show_reviewed_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    cli.main(
        [
            "review",
            "set",
            str(job_id),
            "shortlist",
            "--note",
            "top pick",
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()

    exit_code = cli.main(["review", "show", str(job_id), "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Review decision for job {job_id} (Python Engineer)" in captured.out
    assert "Python Engineer" in captured.out
    assert "Tech Corp" in captured.out
    assert "arbeitnow" in captured.out
    assert "Shortlist" in captured.out
    assert "First reviewed:" in captured.out
    assert "Last updated:" in captured.out
    assert "top pick" in captured.out


def test_review_show_unreviewed_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    exit_code = cli.main(["review", "show", str(job_id), "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Job {job_id} is not currently reviewed." in captured.out


def test_review_show_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(["review", "show", "999", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_review_clear_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    cli.main(["review", "set", str(job_id), "shortlist", "--database", str(db_path)])
    capsys.readouterr()

    assert cli.main(["review", "clear", str(job_id), "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert f"Cleared decision for job {job_id}." in captured.out
    with connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[
            0
        ]
    assert count == 0


def test_review_clear_unreviewed_is_noop(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    assert cli.main(["review", "clear", str(job_id), "--database", str(db_path)]) == 0
    captured = capsys.readouterr()
    assert f"Job {job_id} has no decision; no change." in captured.out


def test_review_clear_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(["review", "clear", "999", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_review_history_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    cli.main(["review", "set", str(job_id), "shortlist", "--database", str(db_path)])
    cli.main(
        [
            "review",
            "set",
            str(job_id),
            "dismiss",
            "--note",
            "changed my mind",
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()

    exit_code = cli.main(["review", "history", str(job_id), "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Review history for job {job_id}" in captured.out
    assert "- -> Shortlist" in captured.out
    assert "Shortlist -> Dismiss" in captured.out
    assert "changed my mind" in captured.out


def test_review_history_unreviewed(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    exit_code = cli.main(["review", "history", str(job_id), "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No history recorded." in captured.out


def test_review_history_unknown_job(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(["review", "history", "999", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No job found with ID 999." in captured.err


def test_review_list_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_ids = []
        for source, slug in (("arbeitnow", "a-1"), ("remoteok", "r-1")):
            save_jobs(connection, [_make_job(source=source, source_job_id=slug)])
            job_ids.append(
                connection.execute(
                    "SELECT id FROM jobs WHERE source_job_id = ?", (slug,)
                ).fetchone()["id"]
            )
    cli.main(
        [
            "review",
            "set",
            str(job_ids[0]),
            "shortlist",
            "--database",
            str(db_path),
        ]
    )
    cli.main(
        [
            "review",
            "set",
            str(job_ids[1]),
            "dismiss",
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()

    exit_code = cli.main(["review", "list", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Review decisions" in captured.out
    assert "Python Engineer" in captured.out
    assert "Shortlist" in captured.out
    assert "Dismiss" in captured.out


def test_review_list_decision_filter(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(source="arbeitnow", source_job_id="a-1"),
                _make_job(
                    source="remoteok",
                    source_job_id="r-1",
                    title="Frontend Developer",
                ),
            ],
        )
        job_ids = {
            row["source_job_id"]: row["id"]
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
    cli.main(
        [
            "review",
            "set",
            str(job_ids["a-1"]),
            "shortlist",
            "--database",
            str(db_path),
        ]
    )
    cli.main(
        [
            "review",
            "set",
            str(job_ids["r-1"]),
            "dismiss",
            "--database",
            str(db_path),
        ]
    )
    capsys.readouterr()

    exit_code = cli.main(
        [
            "review",
            "list",
            "--decision",
            "shortlist",
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Filter: decision=Shortlist" in captured.out
    assert "Decision: Shortlist" in captured.out
    assert "dismiss" not in captured.out


def test_review_list_empty(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection)

    exit_code = cli.main(["review", "list", "--database", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No review decisions recorded." in captured.out


def test_review_list_invalid_decision(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["review", "list", "--decision", "apply", "--database", str(db_path)])
    assert exc_info.value.code == 2


def test_review_commands_invalid_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["review", "list", "--limit", "0", "--database", str(db_path)])
    assert exc_info.value.code == 2
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["review", "history", "1", "--limit", "0", "--database", str(db_path)])
    assert exc_info.value.code == 2


def _seed_reviewed_db(tmp_path: Path) -> tuple[Path, int]:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)
    cli.main(
        [
            "review",
            "set",
            str(job_id),
            "shortlist",
            "--note",
            "top pick",
            "--database",
            str(db_path),
        ]
    )
    return db_path, job_id


def test_review_set_conflicts_with_held_lock(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection)

    with collection_lock(db_path):
        exit_code = cli.main(
            [
                "review",
                "set",
                str(job_id),
                "shortlist",
                "--database",
                str(db_path),
            ]
        )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    assert _table_rows(db_path, "review_decisions") == []


def test_review_clear_conflicts_with_held_lock(tmp_path: Path, capsys) -> None:
    db_path, job_id = _seed_reviewed_db(tmp_path)

    with collection_lock(db_path):
        exit_code = cli.main(
            ["review", "clear", str(job_id), "--database", str(db_path)]
        )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_TEMP_FAILURE
    assert "Collection already running for database:" in captured.err
    assert len(_table_rows(db_path, "review_decisions")) == 1


def test_review_read_commands_available_while_locked(tmp_path: Path, capsys) -> None:
    db_path, job_id = _seed_reviewed_db(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Python Engineer"]})

    before_decisions = _table_rows(db_path, "review_decisions")
    before_events = _table_rows(db_path, "review_events")

    with collection_lock(db_path):
        assert (
            cli.main(["review", "show", str(job_id), "--database", str(db_path)]) == 0
        )
    capsys.readouterr()
    with collection_lock(db_path):
        assert cli.main(["review", "list", "--database", str(db_path)]) == 0
    capsys.readouterr()
    with collection_lock(db_path):
        assert (
            cli.main(["review", "history", str(job_id), "--database", str(db_path)])
            == 0
        )
    capsys.readouterr()
    with collection_lock(db_path):
        assert (
            cli.main(
                [
                    "review",
                    "next",
                    "--profile",
                    str(profile_path),
                    "--database",
                    str(db_path),
                ]
            )
            == 0
        )
    capsys.readouterr()

    assert _table_rows(db_path, "review_decisions") == before_decisions
    assert _table_rows(db_path, "review_events") == before_events


def test_review_next_output(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection, count=2)
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {"desired_titles": ["Python Engineer"], "skills": ["Python"]},
    )

    exit_code = cli.main(
        [
            "review",
            "next",
            "--profile",
            str(profile_path),
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Review queue" in captured.out
    assert "Mode:    grouped" in captured.out
    assert "State:   Unreviewed" in captured.out
    assert "[Score" in captured.out


def test_review_next_state_filter(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        job_id = _seed_jobs(connection, count=2)
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {"desired_titles": ["Python Engineer"], "skills": ["Python"]},
    )
    cli.main(["review", "set", str(job_id), "shortlist", "--database", str(db_path)])
    capsys.readouterr()

    exit_code = cli.main(
        [
            "review",
            "next",
            "--state",
            "shortlist",
            "--profile",
            str(profile_path),
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Decision: Shortlist" in captured.out
    assert "Python Engineer" in captured.out


def test_review_next_json_format(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection, count=2)
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {"desired_titles": ["Python Engineer"], "skills": ["Python"]},
    )

    exit_code = cli.main(
        [
            "review",
            "next",
            "--format",
            "json",
            "--profile",
            str(profile_path),
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["grouped"] is True
    assert payload["state"] == "unreviewed"
    assert payload["candidate_count"] == 2
    assert len(payload["items"]) == 2


def test_review_next_flat_mode(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        _seed_jobs(connection, count=2)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": [], "skills": []})

    exit_code = cli.main(
        [
            "review",
            "next",
            "--no-grouped",
            "--profile",
            str(profile_path),
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Mode:    flat" in captured.out


def test_review_next_no_eligible(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Python Engineer"]})

    exit_code = cli.main(
        [
            "review",
            "next",
            "--profile",
            str(profile_path),
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No eligible opportunities found." in captured.out


def test_review_next_missing_profile(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)

    exit_code = cli.main(
        [
            "review",
            "next",
            "--profile",
            str(tmp_path / "nope.json"),
            "--database",
            str(db_path),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Failed to load profile:" in captured.err


def test_review_next_invalid_state(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "review",
                "next",
                "--state",
                "bogus",
                "--profile",
                "p.json",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_review_next_invalid_min_score(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "review",
                "next",
                "--min-score",
                "101",
                "--profile",
                "p.json",
                "--database",
                str(db_path),
            ]
        )
    assert exc_info.value.code == 2


def test_review_next_missing_profile_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "careeros.sqlite3"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["review", "next", "--database", str(db_path)])
    assert exc_info.value.code == 2
