"""CLI tests for the read-only report command (careeros report)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros import cli
from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.saved_searches.models import SavedSearchFields
from careeros.saved_searches.service import create_search

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

_PROFILE_JSON = (
    '{"desired_titles": ["Python Engineer"], "skills": ["Python"],'
    ' "preferred_locations": ["Berlin"], "remote_preference": "remote",'
    ' "excluded_terms": []}'
)


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Acme GmbH",
        location="Berlin",
        description="Build things with Python.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _seed(
    tmp_path: Path,
    *,
    profile: bool = True,
    saved_search: str | None = None,
    applications: dict[str, str] | None = None,
) -> Path:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        jobs = [
            _make_job(source="arbeitnow", source_job_id="a-1", title="Python Engineer"),
            _make_job(source="remoteok", source_job_id="r-1", title="Rust Engineer"),
            _make_job(
                source="weworkremotely",
                source_job_id="w-1",
                title="Python Engineer",
            ),
            _make_job(source="jobicy", source_job_id="j-1", title="Frontend Engineer"),
        ]
        save_jobs(connection, jobs)
        job_ids = {
            row["source_job_id"]: row["id"]
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        for index, (key, status) in enumerate((applications or {}).items()):
            set_application_status(
                connection,
                job_id=job_ids[key],
                status=status,
                now=_EPOCH + timedelta(minutes=index),
            )
        if profile:
            profile_path = tmp_path / "profile.json"
            profile_path.write_text(_PROFILE_JSON, encoding="utf-8")
        if saved_search is not None:
            create_search(
                connection,
                fields=SavedSearchFields(
                    name=saved_search,
                    query="Engineer",
                    mode="rank",
                    profile_path=str(tmp_path / "profile.json"),
                ),
            )
    return database_path


def test_cli_report_empty_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    monkeypatch.setattr("careeros.cli.common._utc_now", lambda: _EPOCH)
    exit_code = cli.main(["report", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.startswith("Career Snapshot")
    assert f"As of: {_EPOCH.isoformat()}" in captured.out
    assert "Stored jobs:            0" in captured.out
    assert "Tracked applications:   0" in captured.out
    assert "Sources due:" in captured.out
    assert "arbeitnow" in captured.out


def test_cli_report_json_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _seed(tmp_path, applications={"a-1": "interview"})
    monkeypatch.setattr("careeros.cli.common._utc_now", lambda: _EPOCH)
    exit_code = cli.main(
        ["report", "--database", str(database_path), "--format", "json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["as_of"] == _EPOCH.isoformat()
    assert payload["overview"]["total_jobs"] == 4
    assert payload["application_pipeline"]["status_counts"]["interview"] == 1


def test_cli_report_renders_action_items(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(
        tmp_path,
        applications={"a-1": "interview", "r-1": "offer", "j-1": "rejected"},
    )
    exit_code = cli.main(["report", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Interview attention:" in captured.out
    assert "Offer attention:" in captured.out
    assert "[1] Python Engineer (Acme GmbH, arbeitnow)" in captured.out
    assert "CareerOS never infers deadlines" in captured.out


def test_cli_report_zero_limit_is_parse_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--database", str(database_path), "--limit", "0"])
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_report_unknown_saved_search(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    exit_code = cli.main(
        ["report", "--database", str(database_path), "--saved-search", "missing"]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "No saved search named 'missing'" in captured.err


def test_cli_report_saved_search_in_search_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    with connect(database_path) as connection:
        create_search(
            connection,
            fields=SavedSearchFields(name="lookup", query="Engineer", mode="search"),
        )
    exit_code = cli.main(
        ["report", "--database", str(database_path), "--saved-search", "lookup"]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "requires a rank-mode saved search" in captured.err


def test_cli_report_grouped_with_saved_search_is_parse_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path, saved_search="top-roles")
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "report",
                "--database",
                str(database_path),
                "--saved-search",
                "top-roles",
                "--grouped",
            ]
        )
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_report_missing_profile_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path, profile=False)
    exit_code = cli.main(
        [
            "report",
            "--database",
            str(database_path),
            "--profile",
            "/does/not/exist.json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Failed to load profile" in captured.err


def test_cli_report_with_saved_search(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path, saved_search="top-roles")
    exit_code = cli.main(
        ["report", "--database", str(database_path), "--saved-search", "top-roles"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Profile: saved search 'top-roles'" in captured.out
    assert "Ranked 4 jobs:" in captured.out


def test_cli_report_profile_flag_overrides_saved_search(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path, saved_search="top-roles")
    override = tmp_path / "override.json"
    override.write_text(_PROFILE_JSON, encoding="utf-8")
    exit_code = cli.main(
        [
            "report",
            "--database",
            str(database_path),
            "--saved-search",
            "top-roles",
            "--profile",
            str(override),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Profile: saved search 'top-roles'" in captured.out


def test_cli_report_renders_review_queue_without_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path, profile=False)
    exit_code = cli.main(["report", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Review queue" in captured.out
    assert "Unreviewed groups:" in captured.out
    assert (
        "No profile or saved search provided; no review queue computed." in captured.out
    )


def test_cli_report_renders_review_queue_with_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    exit_code = cli.main(
        [
            "report",
            "--database",
            str(database_path),
            "--profile",
            str(tmp_path / "profile.json"),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Review queue" in captured.out
    assert "Profile: command-line --profile" in captured.out
    assert "Ranked 4 unreviewed jobs:" in captured.out


def test_cli_report_review_queue_in_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    exit_code = cli.main(
        [
            "report",
            "--database",
            str(database_path),
            "--profile",
            str(tmp_path / "profile.json"),
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["review_queue"]["source"] == "profile"
    assert payload["review_queue"]["candidate_count"] == 4
    assert len(payload["review_queue"]["items"]) == 4
    assert "reviewed_groups" in payload["overview"]
    assert "unreviewed_groups" in payload["overview"]


def test_cli_report_no_recommendations_note(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path, profile=False)
    exit_code = cli.main(["report", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No profile or saved search provided" in captured.out


def test_cli_report_does_not_mutate_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path, saved_search="top-roles")
    with connect(database_path) as connection:

        def snapshot(table: str) -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]

        tables = [
            "jobs",
            "applications",
            "application_events",
            "collection_runs",
            "saved_searches",
        ]
        before = {table: snapshot(table) for table in tables}

    exit_code = cli.main(
        [
            "report",
            "--database",
            str(database_path),
            "--saved-search",
            "top-roles",
            "--format",
            "json",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["as_of"]

    with connect(database_path) as connection:
        for table in tables:
            assert snapshot(table) == before[table], table


def test_cli_report_never_exits_75_while_lock_held(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _seed(tmp_path)
    monkeypatch.setattr("careeros.cli.common._utc_now", lambda: _EPOCH)
    with collection_lock(database_path, command="collect due"):
        exit_code = cli.main(["report", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Sources due:" in captured.out


def test_cli_report_rejects_unknown_format(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = _seed(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["report", "--database", str(database_path), "--format", "xml"])
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_report_text_includes_source_quality(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _seed(tmp_path)
    monkeypatch.setattr("careeros.cli.common._utc_now", lambda: _EPOCH)
    exit_code = cli.main(["report", "--database", str(database_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Source quality (fields not provided by source):" in captured.out
    assert "arbeitnow: 1 jobs, never groupable: 0, no fields missing" in captured.out
    assert "himalayas: 0 jobs" in captured.out


def test_cli_report_json_includes_source_stats(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = _seed(tmp_path)
    monkeypatch.setattr("careeros.cli.common._utc_now", lambda: _EPOCH)
    exit_code = cli.main(
        ["report", "--database", str(database_path), "--format", "json"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    source_stats = payload["overview"]["source_stats"]
    assert len(source_stats) == 7
    by_source = {entry["source"]: entry for entry in source_stats}
    arbeitnow = by_source["arbeitnow"]
    assert arbeitnow["total_jobs"] == 1
    assert arbeitnow["never_groupable_jobs"] == 0
    assert len(arbeitnow["fields"]) == 8
    company = next(f for f in arbeitnow["fields"] if f["field"] == "company")
    assert company["missing_count"] == 0
    assert company["coverage_pct"] == 100
