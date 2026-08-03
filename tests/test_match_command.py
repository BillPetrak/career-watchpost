"""CLI-level tests for profile-aware ranking and match explanations.

These tests use disposable ``tmp_path`` databases and never touch a real
database or the network.
"""

import json
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _seed(connection) -> None:
    jobs = [
        CollectedJob(
            source="arbeitnow",
            source_job_id="python-1",
            title="Senior Python Developer",
            company="Tech Corp",
            location="Athens, Greece",
            description="Python SQL FastAPI AWS backend.",
            url="https://example.com/1",
            employment_type="Full-time",
            remote_status="remote",
            published_at="2026-07-29T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
        CollectedJob(
            source="remoteok",
            source_job_id="python-1",
            title="Senior Python Developer",
            company="Tech Corp",
            location="Athens",
            description="Python SQL backend.",
            url="https://example.com/1r",
            employment_type="Full-time",
            remote_status="remote",
            published_at="2026-07-29T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
        CollectedJob(
            source="arbeitnow",
            source_job_id="data-1",
            title="Data Scientist",
            company="Data Inc",
            location="Berlin",
            description="Analyze data with Python and SQL.",
            url="https://example.com/2",
            employment_type="Full-time",
            remote_status="remote",
            published_at="2026-07-28T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
        CollectedJob(
            source="arbeitnow",
            source_job_id="rust-1",
            title="Rust Engineer",
            company="Systems Co",
            location="Athens",
            description="Build systems software.",
            url="https://example.com/3",
            employment_type="Contract",
            remote_status="onsite_or_unspecified",
            published_at="2026-07-27T10:00:00Z",
            collected_at="2026-07-29T12:00:00+00:00",
        ),
    ]
    save_jobs(connection, jobs)


def _make_db(tmp_path: Path, grouped: bool = False) -> Path:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as conn:
        initialize_schema(conn)
        _seed(conn)
    if grouped:
        from careeros.database.groups import ensure_grouped

        with connect(db_path) as conn:
            ensure_grouped(conn)
    return db_path


def _write_profile(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _profile_data() -> dict[str, object]:
    return {
        "desired_titles": ["Senior Python Developer", "Data Scientist"],
        "skills": ["Python", "SQL", "FastAPI", "AWS"],
        "required_skills": ["Python", "SQL"],
        "preferred_locations": ["Athens", "Greece"],
        "remote_preference": "remote",
        "excluded_terms": [],
        "hard_excluded_terms": [],
        "preferred_employment_types": ["Full-time"],
        "preferred_seniority": ["Senior"],
    }


def test_rank_output_has_explainable_sections(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, _profile_data())

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[Score " in captured.out
    assert "/100" in captured.out
    assert "Match:" in captured.out
    assert "Matched:" in captured.out
    assert "Missing or unmatched:" in captured.out
    assert "Reasons:" in captured.out
    assert "+20" in captured.out
    assert "+8" in captured.out
    assert "URL:" in captured.out


def test_rank_grouped_collapses_and_scores_members(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path, grouped=True)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, _profile_data())

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path, grouped=True)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[Score " in captured.out
    assert "2 listings grouped" in captured.out
    assert "arbeitnow, remoteok" in captured.out


def test_min_score_filters_results(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, _profile_data())

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path, min_score=60)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Senior Python Developer" in captured.out
    assert "Data Scientist" not in captured.out


def test_min_score_empty_message(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, _profile_data())

    from careeros.cli import run_rank

    exit_code = run_rank("Rust", profile_path, db_path, min_score=100)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "no jobs meet the minimum score of 100." in captured.out.lower()


def test_min_score_out_of_range_via_main(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})
    db_path = tmp_path / "careeros.sqlite3"

    from careeros.cli import main

    with pytest.raises(SystemExit):
        main(
            [
                "rank",
                "python",
                "--profile",
                str(profile_path),
                "--database",
                str(db_path),
                "--min-score",
                "101",
            ]
        )


def test_source_filter_still_behaves(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, _profile_data())

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path, source="remoteok")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Senior Python Developer" in captured.out


def test_example_profile_rank(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    example = PROJECT_ROOT / "config" / "candidate_profile.example.json"

    from careeros.cli import run_rank

    exit_code = run_rank("Python", example, db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[Score " in captured.out
    assert "Reasons:" in captured.out


def test_rank_does_not_mutate_database(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path, grouped=True)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, _profile_data())

    def snapshot() -> dict[str, object]:
        with connect(db_path) as conn:
            tables = [
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ]
            data: dict[str, object] = {}
            for table in tables:
                data[table] = conn.execute(f"SELECT * FROM {table}").fetchall()
            return data

    before = snapshot()

    from careeros.cli import run_rank

    run_rank("Python", profile_path, db_path)
    run_rank("Python", profile_path, db_path, grouped=True)
    capsys.readouterr()

    after = snapshot()

    assert before == after


def test_malformed_profile_returns_error(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    profile_path = tmp_path / "bad.json"
    profile_path.write_text('{"required_skills": 42}', encoding="utf-8")

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Failed to load profile" in captured.err


def test_empty_profile_rank(tmp_path: Path, capsys) -> None:
    db_path = _make_db(tmp_path)
    profile_path = tmp_path / "empty.json"
    _write_profile(profile_path, {})

    from careeros.cli import run_rank

    exit_code = run_rank("Python", profile_path, db_path)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[Score 0/100]" in captured.out
