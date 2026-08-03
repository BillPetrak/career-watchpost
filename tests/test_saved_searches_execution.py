"""Tests for the shared saved-search execution pipeline."""

from __future__ import annotations

from pathlib import Path

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.ranking.profile import load_profile
from careeros.saved_searches.execution import (
    execute_rank,
    execute_search,
    rank_candidates,
    search_candidates,
)
from careeros.saved_searches.models import SavedSearch, SavedSearchFields
from careeros.saved_searches.service import create_search


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str = "Berlin",
    remote_status: str = "remote",
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
        remote_status=remote_status,
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


def _stored(tmp_path: Path, *, mode: str = "search", **overrides) -> SavedSearch:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        defaults = dict(name="engineer", query="Engineer", mode=mode)
        if mode == "rank":
            defaults["profile_path"] = str(tmp_path / "profile.json")
        defaults.update(overrides)
        return create_search(connection, fields=SavedSearchFields(**defaults))


def _write_profile(path: Path) -> None:
    path.write_text(
        '{"desired_titles": ["Python Engineer"], "skills": ["Python"],'
        ' "preferred_locations": ["Berlin"], "remote_preference": "remote",'
        ' "excluded_terms": []}',
        encoding="utf-8",
    )


def test_search_candidates_flat_matches_stored_definition(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    definition = _stored(tmp_path, result_limit=10)

    with connect(db_path) as connection:
        outcome = execute_search(connection, definition)
        direct = search_candidates(
            connection, query="Engineer", grouped=False, limit=10
        )

    assert outcome.grouped is False
    assert [job.title for job in outcome.flat] == [job.title for job in direct.flat]
    assert len(outcome.flat) == 3
    assert outcome.tracking_statuses is None


def test_search_candidates_grouped_collapses_duplicates(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, grouped=True)
    definition = _stored(tmp_path, grouped=True)

    with connect(db_path) as connection:
        outcome = execute_search(connection, definition)

    assert outcome.grouped is True
    assert len(outcome.groups) == 2  # one duplicate group + singleton
    assert len(outcome.flat) == 0


def test_search_candidates_respects_definition_filters(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    definition = _stored(tmp_path, source="arbeitnow", location="Berlin")

    with connect(db_path) as connection:
        outcome = execute_search(connection, definition)

    assert len(outcome.flat) == 1
    assert outcome.flat[0].source == "arbeitnow"


def test_rank_candidates_flat_scores_and_filters(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    profile = load_profile(profile_path)

    with connect(db_path) as connection:
        outcome = rank_candidates(connection, query="Engineer", profile=profile)

    assert outcome.grouped is False
    assert len(outcome.flat_ranked) == 3
    assert all(item.match_score >= 0 for item in outcome.flat_ranked)
    assert outcome.flat_candidate_count == 3


def test_rank_candidates_min_score_and_no_match_distinction(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    profile = load_profile(profile_path)

    with connect(db_path) as connection:
        empty = rank_candidates(
            connection, query="NoSuchJob", profile=profile, min_score=50
        )
        scored = rank_candidates(
            connection, query="Engineer", profile=profile, min_score=999
        )

    assert empty.flat_candidate_count == 0
    assert empty.flat_ranked == ()
    assert scored.flat_candidate_count == 3
    assert scored.flat_ranked == ()


def test_execute_rank_uses_definition_min_score(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    definition = _stored(tmp_path, mode="rank", min_score=90, grouped=False)

    with connect(db_path) as connection:
        profile = load_profile(profile_path)
        outcome = execute_rank(connection, definition, profile=profile)

    assert outcome.grouped is False
    assert outcome.flat_candidate_count == 3
    # No job should clear an impossible threshold.
    assert outcome.flat_ranked == ()


def test_execute_rank_grouped(tmp_path: Path) -> None:
    db_path = _seed(tmp_path, grouped=True)
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path)
    definition = _stored(tmp_path, mode="rank", grouped=True)

    with connect(db_path) as connection:
        profile = load_profile(profile_path)
        outcome = execute_rank(connection, definition, profile=profile)

    assert outcome.grouped is True
    assert len(outcome.grouped_ranked) == 2
    assert outcome.grouped_candidate_count == 2


def test_application_filter_annotation_attached(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    definition = _stored(tmp_path, untracked=True)

    with connect(db_path) as connection:
        outcome = execute_search(connection, definition)

    assert outcome.tracking_statuses is not None
    assert len(outcome.flat) == 3
    for job in outcome.flat:
        assert outcome.tracking_statuses.get(job.id) is None
