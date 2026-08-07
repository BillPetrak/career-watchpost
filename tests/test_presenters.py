"""Focused tests for the shared presentation helpers and CLI conventions.

Milestone M1 consolidated every job/group/ranked/coverage dict builder into
``careeros.presenters`` (used by the CLI and the reporting, review, and
source-stats renderers) and the CLI error conventions into the
``load_profile_or_error`` / ``no_job_found_message`` helpers. These tests pin
the exact key order and values of each projection so the refactor cannot
silently drift from the long-standing contract, and assert the renderer and CLI
output are driven by the shared helpers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.applications import ApplicationEvent
from careeros.database.connection import connect
from careeros.database.groups import GroupedJob
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.presenters import (
    application_event_dict,
    field_coverage_dict,
    group_dict,
    job_dict,
    location_display,
    ranked_group_dict,
    ranked_job_dict,
    source_stats_dict,
)
from careeros.ranking.scorer import MatchResult, RankedGroup, RankedJob
from careeros.source_stats.models import FieldCoverage, SourceStats

JOB_KEYS = (
    "id",
    "source",
    "source_job_id",
    "title",
    "company",
    "location",
    "remote_status",
    "employment_type",
    "published_at",
    "url",
)

GROUP_KEYS = ("group_id", "representative", "members", "sources", "urls")

RANKED_KEYS = (
    "job",
    "match_score",
    "score",
    "reasons",
    "score_components",
)

COVERAGE_KEYS = ("field", "missing_count", "provided_count", "coverage_pct")

SOURCE_STATS_KEYS = ("source", "total_jobs", "never_groupable_jobs", "fields")


def _seed_job(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    connection = connect(tmp_path / "careeros.sqlite3")
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            CollectedJob(
                source="arbeitnow",
                source_job_id="python-dev-1",
                title="Python Developer",
                company="Tech Corp",
                location="Athens",
                description="Build Python backends.",
                url="https://example.com/1",
                employment_type="Full-time",
                remote_status="remote",
                published_at="2026-07-29T10:00:00Z",
                collected_at="2026-07-29T12:00:00+00:00",
            )
        ],
    )
    job_id = connection.execute("SELECT id FROM jobs").fetchone()["id"]
    return connection, job_id


def _stored_job(tmp_path: Path):
    connection, job_id = _seed_job(tmp_path)
    from careeros.database.jobs import get_job_by_id

    return get_job_by_id(connection, job_id)


def _match_result() -> MatchResult:
    return MatchResult(
        score=75,
        matched_titles=("Python Developer",),
        matched_skills=(),
        missing_required_skills=(),
        preferred_skills_not_observed=(),
        location_matched=None,
        remote_matched=False,
        employment_matched=None,
        employment_mismatch=False,
        seniority_matched=None,
        seniority_mismatch=False,
        excluded_terms=(),
        hard_excluded_terms=(),
        description_unavailable=False,
        components=(
            __import__(
                "careeros.ranking.scorer", fromlist=["ScoreComponent"]
            ).ScoreComponent(kind="title", label="Matches desired title", points=75),
        ),
    )


def test_job_dict_key_order_and_values(tmp_path: Path) -> None:
    job = _stored_job(tmp_path)
    assert job is not None
    result = job_dict(job)

    assert tuple(result) == JOB_KEYS
    assert result["id"] == job.id
    assert result["source"] == "arbeitnow"
    assert result["source_job_id"] == "python-dev-1"
    assert result["title"] == "Python Developer"
    assert result["company"] == "Tech Corp"
    assert result["location"] == "Athens"
    assert result["remote_status"] == "remote"
    assert result["employment_type"] == "Full-time"
    assert result["published_at"] == "2026-07-29T10:00:00Z"
    assert result["url"] == "https://example.com/1"


def test_application_event_dict_key_order_and_note_omission() -> None:
    event = ApplicationEvent(
        id=7,
        application_id=3,
        old_status="applied",
        new_status="interview",
        note="secret-note",
        created_at="2026-08-03T12:00:00+00:00",
    )
    result = application_event_dict(event, job_id=42)

    assert tuple(result) == (
        "event_id",
        "job_id",
        "old_status",
        "new_status",
        "created_at",
    )
    assert result == {
        "event_id": 7,
        "job_id": 42,
        "old_status": "applied",
        "new_status": "interview",
        "created_at": "2026-08-03T12:00:00+00:00",
    }
    assert "note" not in result
    assert "application_id" not in result


def test_group_dict_key_order_and_values(tmp_path: Path) -> None:
    job = _stored_job(tmp_path)
    assert job is not None
    group = GroupedJob(
        group_id="abc",
        representative=job,
        members=(job,),
        sources=("arbeitnow",),
        urls=("https://example.com/1",),
    )
    result = group_dict(group)

    assert tuple(result) == GROUP_KEYS
    assert result["group_id"] == "abc"
    assert result["representative"] == job_dict(job)
    assert result["members"] == [job_dict(job)]
    assert result["sources"] == ["arbeitnow"]
    assert result["urls"] == ["https://example.com/1"]


def test_ranked_job_dict_key_order_and_values(tmp_path: Path) -> None:
    job = _stored_job(tmp_path)
    assert job is not None
    ranked = RankedJob(job=job, result=_match_result())
    result = ranked_job_dict(ranked)

    assert tuple(result) == RANKED_KEYS
    assert result["job"] == job_dict(job)
    assert result["match_score"] == 75
    assert result["score"] == 75
    assert result["reasons"] == ["Matches desired title"]
    assert result["score_components"][0]["kind"] == "title"


def test_ranked_group_dict_key_order_and_values(tmp_path: Path) -> None:
    job = _stored_job(tmp_path)
    assert job is not None
    group = GroupedJob(
        group_id="abc",
        representative=job,
        members=(job,),
        sources=("arbeitnow",),
        urls=("https://example.com/1",),
    )
    ranked = RankedGroup(group=group, result=_match_result())
    result = ranked_group_dict(ranked)

    assert tuple(result) == (
        "group",
        "match_score",
        "score",
        "reasons",
        "score_components",
    )
    assert result["group"] == group_dict(group)
    assert result["match_score"] == 75


@pytest.mark.parametrize(
    ("location", "remote_status", "expected"),
    [
        ("Athens", "remote", "Athens (remote)"),
        ("Berlin", "onsite_or_unspecified", "Berlin"),
        (None, "remote", "N/A (remote)"),
        (None, None, "N/A"),
    ],
)
def test_location_display_variants(
    location: str | None, remote_status: str | None, expected: str
) -> None:
    assert location_display(location, remote_status) == expected


def test_field_coverage_dict_key_order_and_values() -> None:
    field = FieldCoverage(
        field="location", missing_count=2, provided_count=8, coverage_pct=80
    )
    result = field_coverage_dict(field)

    assert tuple(result) == COVERAGE_KEYS
    assert result["field"] == "location"
    assert result["missing_count"] == 2
    assert result["provided_count"] == 8
    assert result["coverage_pct"] == 80


def test_source_stats_dict_key_order_and_values() -> None:
    field = FieldCoverage(
        field="location", missing_count=0, provided_count=1, coverage_pct=100
    )
    stats = SourceStats(
        source="arbeitnow", total_jobs=1, never_groupable_jobs=0, fields=(field,)
    )
    result = source_stats_dict(stats)

    assert tuple(result) == SOURCE_STATS_KEYS
    assert result["source"] == "arbeitnow"
    assert result["total_jobs"] == 1
    assert result["never_groupable_jobs"] == 0
    assert result["fields"] == [field_coverage_dict(field)]


def test_source_stats_json_uses_shared_dict(tmp_path: Path) -> None:
    connection, _ = _seed_job(tmp_path)
    from careeros.source_stats.render import render_source_stats_json
    from careeros.source_stats.service import compute_source_stats

    stats = compute_source_stats(connection)
    text = render_source_stats_json(stats)

    import json

    payload = json.loads(text)
    assert payload["sources"] == [source_stats_dict(entry) for entry in stats]


def test_no_job_found_message() -> None:
    from careeros.cli import no_job_found_message

    assert no_job_found_message(12) == "No job found with ID 12."


def test_load_profile_or_error_valid(tmp_path: Path) -> None:
    from careeros.cli import load_profile_or_error

    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"desired_titles": ["Engineer"]}', encoding="utf-8")

    profile = load_profile_or_error(profile_path)

    assert profile is not None
    assert profile.desired_titles == ("Engineer",)


def test_load_profile_or_error_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import load_profile_or_error

    result = load_profile_or_error(tmp_path / "missing.json")
    captured = capsys.readouterr()

    assert result is None
    assert captured.err.startswith("Failed to load profile: ")
    assert captured.out == ""


def test_load_profile_or_error_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import load_profile_or_error

    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{broken", encoding="utf-8")

    result = load_profile_or_error(profile_path)
    captured = capsys.readouterr()

    assert result is None
    assert captured.err.startswith("Failed to load profile: ")


def test_show_failure_uses_domain_label(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import run_show

    bad_database = tmp_path / "iamadir"
    bad_database.mkdir()

    exit_code = run_show(1, bad_database)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.startswith("Show failed: ")


def test_search_failure_uses_domain_label(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import run_search

    bad_database = tmp_path / "iamadir"
    bad_database.mkdir()

    exit_code = run_search("engineer", bad_database)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.startswith("Search failed: ")
