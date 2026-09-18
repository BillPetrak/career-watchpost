"""Tests for the daily review queue builder and its renderers."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.ranking.scorer import CandidateProfile
from careeros.review.render import render_queue_json, render_queue_text
from careeros.review.service import (
    ValidationError,
    build_queue,
    clear_decision,
    resolve_group_decision,
    set_decision,
)

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")

_PROFILE = CandidateProfile(
    desired_titles=("Python Engineer",),
    skills=("Python",),
    preferred_locations=("Berlin",),
    remote_preference="remote",
)


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str,
    remote_status: str = "remote",
    description: str = "Python backend engineering.",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status=remote_status,
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    database_path = tmp_path / "careeros.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    return connection


def _seed(
    connection,
    *,
    with_duplicate: bool = False,
    count: int = 3,
) -> dict[str, int]:
    jobs = [
        _make_job(
            source="arbeitnow",
            source_job_id="a-1",
            title="Python Engineer",
            company="Acme GmbH",
            location="Berlin",
        ),
        _make_job(
            source="remoteok",
            source_job_id="r-1",
            title="Frontend Developer",
            company="Web Inc",
            location="Berlin",
        ),
        _make_job(
            source="jobicy",
            source_job_id="j-1",
            title="Rust Engineer",
            company="Sys Co",
            location="Munich",
        ),
    ][:count]
    if with_duplicate:
        jobs.append(
            _make_job(
                source="weworkremotely",
                source_job_id="w-1",
                title="Python Engineer",
                company="Acme PLC",
                location="Berlin",
            )
        )
    save_jobs(connection, jobs)
    ensure_grouped(connection)
    return {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }


def test_empty_database_returns_empty_queue(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        outcome = build_queue(connection, profile=_PROFILE, limit=10)

    assert outcome.items == ()
    assert outcome.candidate_count == 0
    assert "No eligible opportunities found." in render_queue_text(outcome)


def test_unreviewed_queue_excludes_decided_jobs(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        set_decision(connection, job_id=job_ids["a-1"], decision="dismiss", now=_EPOCH)
        outcome = build_queue(connection, profile=_PROFILE, state="unreviewed")

    assert [item.group.representative.id for item in outcome.items] == [
        job_ids["r-1"],
        job_ids["j-1"],
    ]
    assert outcome.candidate_count == 2
    assert all(item.group_decision.decision is None for item in outcome.items)


def test_state_shortlist_returns_only_shortlisted(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        set_decision(connection, job_id=job_ids["r-1"], decision="dismiss", now=_EPOCH)
        outcome = build_queue(connection, profile=_PROFILE, state="shortlist")

    assert [item.group.representative.id for item in outcome.items] == [job_ids["a-1"]]
    assert outcome.items[0].group_decision.decision == "shortlist"


def test_state_any_returns_all_states(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        outcome = build_queue(connection, profile=_PROFILE, state="any")

    assert {item.group.representative.id for item in outcome.items} == {
        job_ids["a-1"],
        job_ids["r-1"],
        job_ids["j-1"],
    }


def test_review_later_excluded_from_default_unreviewed(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="review_later", now=_EPOCH
        )
        unreviewed = build_queue(connection, profile=_PROFILE, state="unreviewed")
        review_later = build_queue(connection, profile=_PROFILE, state="review_later")

    assert job_ids["a-1"] not in {
        item.group.representative.id for item in unreviewed.items
    }
    assert [item.group.representative.id for item in review_later.items] == [
        job_ids["a-1"]
    ]


def test_clearing_review_later_returns_it_to_unreviewed(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection, count=1)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="review_later", now=_EPOCH
        )
        clear_decision(connection, job_id=job_ids["a-1"], now=_EPOCH)
        outcome = build_queue(connection, profile=_PROFILE, state="unreviewed")

    assert [item.group.representative.id for item in outcome.items] == [job_ids["a-1"]]


def test_grouped_default_collapses_duplicates(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection, with_duplicate=True, count=2)
        grouped = build_queue(connection, profile=_PROFILE, state="unreviewed")
        flat = build_queue(
            connection, profile=_PROFILE, state="unreviewed", grouped=False
        )

    assert len(grouped.items) == 2
    python_group = next(
        item for item in grouped.items if item.group.representative.id == job_ids["a-1"]
    )
    assert len(python_group.group.members) == 2
    assert flat.items[0].group.representative.id == job_ids["a-1"]
    assert len(flat.items) == 3


def test_group_decision_propagates_across_members(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection, with_duplicate=True, count=2)
        set_decision(
            connection, job_id=job_ids["w-1"], decision="shortlist", now=_EPOCH
        )
        outcome = build_queue(connection, profile=_PROFILE, state="shortlist")

    assert len(outcome.items) == 1
    assert outcome.items[0].group_decision.decision == "shortlist"
    assert outcome.items[0].group_decision.conflict is False


def test_group_decision_conflict_resolves_to_lowest_id(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection, with_duplicate=True, count=2)
        set_decision(connection, job_id=job_ids["a-1"], decision="dismiss", now=_EPOCH)
        set_decision(
            connection, job_id=job_ids["w-1"], decision="shortlist", now=_EPOCH
        )
        outcome = build_queue(connection, profile=_PROFILE, state="any")

    assert len(outcome.items) == 2
    conflicting = next(
        item for item in outcome.items if item.group.representative.id == job_ids["a-1"]
    )
    assert conflicting.group_decision.decision == "dismiss"
    assert conflicting.group_decision.conflict is True
    assert conflicting.group_decision.owner is not None
    assert conflicting.group_decision.owner.id == job_ids["a-1"]
    assert "Conflict: resolved from job" in render_queue_text(outcome)


def test_tracked_groups_excluded_by_default(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        outcome = build_queue(connection, profile=_PROFILE, state="unreviewed")

    assert job_ids["a-1"] not in {
        item.group.representative.id for item in outcome.items
    }


def test_include_tracked_annotates_application(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        set_application_status(
            connection, job_id=job_ids["a-1"], status="applied", now=_EPOCH
        )
        outcome = build_queue(
            connection,
            profile=_PROFILE,
            state="unreviewed",
            include_tracked=True,
        )

    tracked = next(
        item for item in outcome.items if item.group.representative.id == job_ids["a-1"]
    )
    assert tracked.application.status == "applied"
    assert "Application: Applied" in render_queue_text(outcome)


def test_min_score_filters_after_scoring(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed(connection)
        all_outcome = build_queue(connection, profile=_PROFILE, state="unreviewed")
        min_outcome = build_queue(
            connection, profile=_PROFILE, state="unreviewed", min_score=53
        )

    assert len(all_outcome.items) == 3
    assert len(min_outcome.items) == 1
    assert min_outcome.candidate_count == 3
    assert min_outcome.qualified_count == 1
    assert min_outcome.items[0].ranked.match_score == 53


def test_limit_applies_after_min_score(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed(connection)
        outcome = build_queue(connection, profile=_PROFILE, state="unreviewed", limit=1)

    assert len(outcome.items) == 1
    assert outcome.candidate_count == 3
    assert outcome.qualified_count == 3


def test_source_filter_before_grouping(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection, with_duplicate=True, count=2)
        outcome = build_queue(
            connection, profile=_PROFILE, state="unreviewed", source="remoteok"
        )

    assert [item.group.representative.id for item in outcome.items] == [job_ids["r-1"]]


def test_location_filter(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        outcome = build_queue(
            connection, profile=_PROFILE, state="unreviewed", location="Munich"
        )

    assert [item.group.representative.id for item in outcome.items] == [job_ids["j-1"]]


def test_remote_filter_excludes_onsite(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        with connect(tmp_path / "careeros.sqlite3") as conn:
            conn.execute(
                "UPDATE jobs SET remote_status = 'onsite_or_unspecified'"
                " WHERE source_job_id = 'j-1'"
            )
            conn.commit()
        outcome = build_queue(
            connection, profile=_PROFILE, state="unreviewed", remote=True
        )

    assert job_ids["j-1"] not in {
        item.group.representative.id for item in outcome.items
    }


def test_deterministic_ordering(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed(connection)
        first = build_queue(connection, profile=_PROFILE, state="unreviewed")
        second = build_queue(connection, profile=_PROFILE, state="unreviewed")

    assert [item.group.representative.id for item in first.items] == [
        item.group.representative.id for item in second.items
    ]
    scores = [item.ranked.match_score for item in first.items]
    assert scores == sorted(scores, reverse=True)


def test_resolve_group_decision_unreviewed(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed(connection, with_duplicate=True, count=2)
        from careeros.database.groups import list_all_groups

        groups = list_all_groups(connection)
        resolved = resolve_group_decision(groups[0], {})
        assert resolved.decision is None
        assert resolved.conflict is False


def test_invalid_state_raises(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        with pytest.raises(ValidationError):
            build_queue(connection, profile=_PROFILE, state="bogus")


def test_invalid_limit_raises(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        with pytest.raises(ValidationError):
            build_queue(connection, profile=_PROFILE, limit=0)


def test_invalid_min_score_raises(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        with pytest.raises(ValidationError):
            build_queue(connection, profile=_PROFILE, min_score=101)


def test_json_output_matches_text_data(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        outcome = build_queue(connection, profile=_PROFILE, state="any")
        payload = json.loads(render_queue_json(outcome))

    assert payload["grouped"] is True
    assert payload["state"] == "any"
    assert payload["candidate_count"] == 3
    assert len(payload["items"]) == 3
    item = payload["items"][0]
    assert item["decision"] == "shortlist"
    assert item["match_score"] == item["score"]
    assert "reasons" in item
    assert "group" in item
    assert item["group"]["representative"]["id"] == job_ids["a-1"]
    assert item["application_status"] is None
    text = render_queue_text(outcome)
    assert "Review queue" in text
    assert "[Score" in text


def test_json_keys_are_stable_and_sorted(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed(connection)
        outcome = build_queue(connection, profile=_PROFILE, state="unreviewed")
        payload = json.loads(render_queue_json(outcome))

    top_level = list(payload.keys())
    assert top_level == sorted(top_level)
    assert top_level == [
        "candidate_count",
        "grouped",
        "include_tracked",
        "items",
        "min_score",
        "state",
    ]
    group_keys = sorted(payload["items"][0]["group"].keys())
    assert group_keys == [
        "group_id",
        "members",
        "representative",
        "sources",
        "urls",
    ]
