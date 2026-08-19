"""Tests for the Career Snapshot reporting service and renderers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from careeros.applications.service import set_application_status
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import initialize_schema
from careeros.locking import collection_lock
from careeros.ranking.profile import load_profile
from careeros.reporting.models import ACTION_ITEM_LABELS, Report
from careeros.reporting.render import render_report, render_report_json
from careeros.reporting.service import build_report
from careeros.review.service import set_decision
from careeros.saved_searches.models import SavedSearch, SavedSearchFields
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
    company: str = "Acme GmbH",
    location: str = "Berlin",
    remote_status: str = "remote",
    description: str = "Build things with Python.",
    url: str | None = None,
    published_at: str = "2026-07-29T10:00:00Z",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=url or f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "careeros.sqlite3")
    initialize_schema(connection)
    return connection


def _seed_jobs(connection: sqlite3.Connection) -> dict[str, int]:
    jobs = [
        _make_job(source="arbeitnow", source_job_id="a-1", title="Python Engineer"),
        _make_job(source="remoteok", source_job_id="r-1", title="Rust Engineer"),
        _make_job(
            source="weworkremotely",
            source_job_id="w-1",
            title="Python Engineer",
            company="Acme Inc",
        ),
        _make_job(source="jobicy", source_job_id="j-1", title="Frontend Engineer"),
    ]
    save_jobs(connection, jobs)
    return {
        row["source_job_id"]: row["id"]
        for row in connection.execute("SELECT id, source_job_id FROM jobs")
    }


def _track(
    connection: sqlite3.Connection,
    job_ids: dict[str, int],
    state: dict[str, str],
    *,
    now: datetime = _EPOCH,
) -> None:
    for index, (key, status) in enumerate(state.items()):
        set_application_status(
            connection,
            job_id=job_ids[key],
            status=status,
            now=now + timedelta(minutes=index),
        )


def _write_profile(tmp_path: Path) -> Path:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(_PROFILE_JSON, encoding="utf-8")
    return profile_path


def _create_rank_search(
    tmp_path: Path,
    connection: sqlite3.Connection,
    *,
    name: str = "top-roles",
    query: str = "Engineer",
    profile_path: Path | None = None,
    **overrides: object,
) -> SavedSearch:
    profile_path = profile_path or _write_profile(tmp_path)
    defaults = dict(
        name=name,
        query=query,
        mode="rank",
        profile_path=str(profile_path),
    )
    defaults.update(overrides)
    return create_search(connection, fields=SavedSearchFields(**defaults))


def _seed_run(
    connection: sqlite3.Connection,
    source: str,
    started_at: datetime,
    *,
    succeeded: bool = True,
    error: str | None = None,
) -> None:
    run_id = insert_run(connection, source=source, started_at=started_at)
    complete_run(
        connection,
        run_id=run_id,
        finished_at=started_at + timedelta(seconds=5),
        succeeded=succeeded,
        fetched=4,
        inserted=3,
        updated=1,
        error=error,
    )


def _build(tmp_path: Path, **kwargs: object) -> Report:
    with connect(tmp_path / "careeros.sqlite3") as connection:
        initialize_schema(connection)
        return build_report(connection, now=_EPOCH, **kwargs)


def _seed_profile(tmp_path: Path) -> Path:
    return _write_profile(tmp_path)


def _seed_recency_jobs(connection: sqlite3.Connection) -> None:
    """Seed one old high-scoring job plus three newer lower-scoring jobs.

    The profile prefers "Python Engineer"/"Python"/"Berlin"/remote, so
    "old-high" scores ~53 while each "Rust Engineer" scores ~25. All three
    newer jobs publish after "old-high", so a load-time ``limit`` that picks
    the newest-N first would silently drop the highest-scoring job.
    """
    save_jobs(
        connection,
        [
            _make_job(
                source="arbeitnow",
                source_job_id="old-high",
                title="Python Engineer",
                published_at="2026-07-01T10:00:00Z",
            ),
            _make_job(
                source="remoteok",
                source_job_id="new-1",
                title="Rust Engineer",
                company="Alpha GmbH",
                description="Systems programming in Rust.",
                published_at="2026-07-29T10:00:00Z",
            ),
            _make_job(
                source="jobicy",
                source_job_id="new-2",
                title="Rust Engineer",
                company="Beta GmbH",
                description="Systems programming in Rust.",
                published_at="2026-07-28T10:00:00Z",
            ),
            _make_job(
                source="weworkremotely",
                source_job_id="new-3",
                title="Rust Engineer",
                company="Gamma GmbH",
                description="Systems programming in Rust.",
                published_at="2026-07-27T10:00:00Z",
            ),
        ],
    )


def test_empty_database_report(tmp_path: Path) -> None:
    report = _build(tmp_path)

    assert report.overview.total_jobs == 0
    assert report.overview.canonical_groups == 0
    assert report.overview.grouped_members == 0
    assert report.overview.distinct_opportunities == 0
    assert report.overview.tracked_applications == 0
    assert report.overview.saved_searches == 0
    assert report.overview.reviewed_groups == 0
    assert report.overview.unreviewed_groups == 0
    assert report.overview.shortlisted_groups == 0
    assert report.overview.dismissed_groups == 0
    assert report.overview.review_later_groups == 0
    assert all(status.due for status in report.overview.source_snapshots)
    assert tuple(report.overview.due_sources) == tuple(
        status.source for status in report.overview.source_snapshots
    )

    assert report.pipeline.status_counts == (
        ("interested", 0),
        ("applied", 0),
        ("interview", 0),
        ("offer", 0),
        ("rejected", 0),
        ("withdrawn", 0),
    )
    assert report.pipeline.recently_changed == ()
    assert report.actions.interview == ()
    assert report.actions.offer == ()
    assert report.actions.interested == ()
    assert report.actions.awaiting_progress == ()
    assert report.review.source == "none"
    assert report.review.note is not None
    assert report.recommendations.source == "none"
    assert report.recommendations.note is not None
    assert len(report.health.per_source) == 7


def test_overview_counts(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(
            connection,
            job_ids,
            {"a-1": "applied", "r-1": "interested"},
        )
        _create_rank_search(tmp_path, connection)
        ensure_grouped(connection)

    report = _build(tmp_path)
    assert report.overview.total_jobs == 4
    assert report.overview.grouped_members == 2
    assert report.overview.canonical_groups == 1
    assert report.overview.distinct_opportunities == 3
    assert report.overview.tracked_applications == 2
    assert report.overview.saved_searches == 1


def test_overview_review_counters(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        set_decision(connection, job_id=job_ids["r-1"], decision="dismiss", now=_EPOCH)
        ensure_grouped(connection)

    report = _build(tmp_path)
    assert report.overview.reviewed_groups == 2
    assert report.overview.unreviewed_groups == 1
    assert report.overview.shortlisted_groups == 1
    assert report.overview.dismissed_groups == 1
    assert report.overview.review_later_groups == 0


def test_review_queue_section_ranks_unreviewed_groups(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_decision(
            connection, job_id=job_ids["a-1"], decision="shortlist", now=_EPOCH
        )
        ensure_grouped(connection)

    report = _build(tmp_path, profile=load_profile(profile), grouped=True)
    assert report.review.source == "profile"
    assert report.review.grouped is True
    assert report.review.candidate_count == 2
    assert len(report.review.items) == 2
    assert all(item.match_score > 0 for item in report.review.items)
    text = render_report(report)
    assert "Review queue" in text
    assert "Ranked 2 unreviewed groups:" in text
    assert "Profile: command-line --profile" in text


def test_review_queue_excludes_tracked_applications(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "applied"})

    report = _build(tmp_path, profile=load_profile(profile), grouped=False)
    assert all(
        item.group.representative.id != job_ids["a-1"] for item in report.review.items
    )


def test_review_queue_flat_mode(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_jobs(connection)

    report = _build(tmp_path, profile=load_profile(profile), grouped=False)
    assert report.review.grouped is False
    text = render_report(report)
    assert "Mode:    flat" in text
    assert "Ranked 4 unreviewed jobs:" in text


def test_review_queue_source_saved_search(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        saved = _create_rank_search(tmp_path, connection, profile_path=profile)

    report = _build(tmp_path, saved_search=saved, profile=load_profile(profile))
    assert report.review.source == "saved-search"
    assert report.review.source_name == "top-roles"
    assert "Profile: saved search 'top-roles'" in render_report(report)


def test_review_queue_none_without_profile(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)

    report = _build(tmp_path)
    assert report.review.source == "none"
    assert report.review.candidate_count == 0
    text = render_report(report)
    assert "Review queue" in text
    assert "No profile or saved search provided; no review queue computed." in text
    payload = json.loads(render_report_json(report))
    assert payload["review_queue"]["source"] == "none"
    assert payload["review_queue"]["note"] is not None


def test_pipeline_status_counts_are_zero_filled(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "applied"})

    report = _build(tmp_path)
    assert report.pipeline.status_counts == (
        ("interested", 0),
        ("applied", 1),
        ("interview", 0),
        ("offer", 0),
        ("rejected", 0),
        ("withdrawn", 0),
    )


def test_recently_changed_orders_newest_first(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "applied", "r-1": "interested"})

    report = _build(tmp_path)
    changed = report.pipeline.recently_changed
    assert [snapshot.job.source_job_id for snapshot in changed] == ["r-1", "a-1"]


def test_recently_changed_ties_break_by_application_id(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        for key in ("a-1", "r-1"):
            set_application_status(
                connection,
                job_id=job_ids[key],
                status="applied",
                now=_EPOCH,
            )

    report = _build(tmp_path)
    changed = report.pipeline.recently_changed
    assert [snapshot.job.source_job_id for snapshot in changed] == ["r-1", "a-1"]


def test_recently_changed_capped_by_limit(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "applied", "r-1": "interested"})

    report = _build(tmp_path, limit=1)
    assert len(report.pipeline.recently_changed) == 1


def test_action_items_classify_active_statuses(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(
            connection,
            job_ids,
            {
                "a-1": "interview",
                "r-1": "offer",
                "w-1": "interested",
                "j-1": "applied",
            },
        )

    report = _build(tmp_path)
    assert [item.job.source_job_id for item in report.actions.interview] == ["a-1"]
    assert [item.job.source_job_id for item in report.actions.offer] == ["r-1"]
    assert [item.job.source_job_id for item in report.actions.interested] == ["w-1"]
    assert [item.job.source_job_id for item in report.actions.awaiting_progress] == [
        "j-1"
    ]
    assert report.actions.interview[0].label == ACTION_ITEM_LABELS["interview"]


def test_rejected_and_withdrawn_only_in_counts(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(
            connection,
            job_ids,
            {"a-1": "rejected", "r-1": "withdrawn", "w-1": "interview"},
        )

    report = _build(tmp_path)
    assert [item.job.source_job_id for item in report.actions.interview] == ["w-1"]
    assert report.actions.offer == ()
    assert report.actions.interested == ()
    assert report.actions.awaiting_progress == ()
    rejected_job_ids = {item.job.source_job_id for item in report.actions.interview}
    assert "a-1" not in rejected_job_ids
    assert dict(report.pipeline.status_counts)["rejected"] == 1
    assert dict(report.pipeline.status_counts)["withdrawn"] == 1


def test_action_items_capped_by_limit(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(
            connection,
            job_ids,
            {"a-1": "interview", "r-1": "interview", "w-1": "interview"},
        )

    report = _build(tmp_path, limit=2)
    assert len(report.actions.interview) == 2


def test_latest_note_comes_from_most_recent_event(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="first note",
            now=_EPOCH,
        )
        set_application_status(
            connection,
            job_id=job_ids["a-1"],
            status="applied",
            note="second note",
            now=_EPOCH + timedelta(hours=1),
        )

    report = _build(tmp_path)
    snapshot = report.pipeline.recently_changed[0]
    assert snapshot.latest_note == "second note"


def test_collection_health_reports_success_and_due(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_run(connection, "jobicy", _EPOCH - timedelta(minutes=30))
        _seed_run(
            connection,
            "arbeitnow",
            _EPOCH - timedelta(days=2),
            succeeded=False,
            error="RuntimeError: boom",
        )

    report = _build(tmp_path)
    by_source = {status.source: status for status in report.health.per_source}
    assert by_source["jobicy"].last_succeeded is True
    assert by_source["jobicy"].due is False
    assert by_source["arbeitnow"].last_succeeded is False
    assert by_source["arbeitnow"].last_error == "RuntimeError: boom"
    assert by_source["remoteok"].last_attempt is None
    assert by_source["remoteok"].due is True
    assert report.overview.due_sources == (
        "arbeitnow",
        "remoteok",
        "weworkremotely",
        "remotive",
        "workingnomads",
        "himalayas",
    )


def test_malformed_timestamp_is_due_not_crash(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        connection.execute(
            """
            INSERT INTO collection_runs (source, started_at, finished_at, succeeded)
            VALUES ('jobicy', 'not-a-date', 'not-a-date', 1)
            """
        )
        connection.commit()

    report = _build(tmp_path)
    by_source = {status.source: status for status in report.health.per_source}
    assert by_source["jobicy"].last_attempt is None
    assert by_source["jobicy"].due is True


def test_recommendations_from_profile_flat(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "applied"})

    report = _build(
        tmp_path,
        profile=load_profile(profile),
        grouped=False,
    )
    assert report.recommendations.source == "profile"
    assert report.recommendations.grouped is False
    assert report.recommendations.candidate_count == 3
    titles = [job.job.title for job in report.recommendations.ranked_jobs]
    assert "Python Engineer" in titles
    assert all(
        snapshot.job.id != job_ids["a-1"]
        for snapshot in report.recommendations.ranked_jobs
    )


def test_recommendations_from_profile_grouped(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        ensure_grouped(connection)

    report = _build(
        tmp_path,
        profile=load_profile(profile),
        grouped=True,
    )
    assert report.recommendations.grouped is True
    assert report.recommendations.ranked_groups
    assert report.recommendations.candidate_count == 3


def test_recommendations_without_profile_or_saved_search(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)

    report = _build(tmp_path)
    assert report.recommendations.source == "none"
    assert report.recommendations.ranked_jobs == ()
    assert report.recommendations.note is not None


def test_recommendations_from_saved_search(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-2",
                    title="Python Engineer",
                    location="Munich",
                ),
                _make_job(
                    source="remoteok",
                    source_job_id="r-1",
                    title="Python Engineer",
                    description="Write Python services.",
                ),
                _make_job(
                    source="jobicy",
                    source_job_id="j-1",
                    title="Rust Engineer",
                    description="Systems programming.",
                ),
            ],
        )
        saved = _create_rank_search(
            tmp_path,
            connection,
            name="top-roles",
            query="Python",
            profile_path=profile,
            min_score=0,
        )

    report = _build(tmp_path, saved_search=saved, profile=load_profile(profile))
    assert report.recommendations.source == "saved-search"
    assert report.recommendations.source_name == "top-roles"
    titles = [job.job.title for job in report.recommendations.ranked_jobs]
    assert titles == ["Python Engineer"] * 2


def test_saved_search_result_limit_then_report_limit(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        saved = _create_rank_search(
            tmp_path,
            connection,
            name="all",
            query="Engineer",
            profile_path=profile,
            result_limit=3,
        )

    report = _build(
        tmp_path,
        saved_search=saved,
        profile=load_profile(profile),
        limit=2,
    )
    assert len(report.recommendations.ranked_jobs) == 2


def test_recommendations_honor_saved_search_min_score(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        saved = _create_rank_search(
            tmp_path,
            connection,
            name="strict",
            query="Engineer",
            profile_path=profile,
            min_score=100,
        )

    report = _build(
        tmp_path,
        saved_search=saved,
        profile=load_profile(profile),
    )
    assert report.recommendations.ranked_jobs == ()
    assert report.recommendations.candidate_count == 4


def test_grouped_recommendations_do_not_double_count(
    tmp_path: Path,
) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_jobs(connection)
        ensure_grouped(connection)

    flat = _build(
        tmp_path,
        profile=load_profile(profile),
        grouped=False,
    )
    grouped = _build(
        tmp_path,
        profile=load_profile(profile),
        grouped=True,
    )
    assert flat.recommendations.candidate_count == 4
    assert grouped.recommendations.candidate_count == 3
    assert len(grouped.recommendations.ranked_groups) < len(
        flat.recommendations.ranked_jobs
    )


def test_recommendations_include_older_highest_scoring_job(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_recency_jobs(connection)

    report = _build(tmp_path, profile=load_profile(profile), grouped=False, limit=2)
    ranked = report.recommendations.ranked_jobs
    # "old-high" is older than the three newest jobs but scores highest; it must
    # be ranked first even though it is outside the newest two.
    assert [job.job.source_job_id for job in ranked] == ["old-high", "new-1"]
    assert ranked[0].job.title == "Python Engineer"


def test_recommendations_limit_applied_after_scoring(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_recency_jobs(connection)

    report = _build(tmp_path, profile=load_profile(profile), grouped=False, limit=2)
    # The limit trims the scored, sorted results: two items out of a pool of
    # four, ordered by match score (highest first), not by recency.
    assert len(report.recommendations.ranked_jobs) == 2
    assert [job.job.source_job_id for job in report.recommendations.ranked_jobs] == [
        "old-high",
        "new-1",
    ]


def test_recommendations_candidate_count_reflects_full_pool(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_recency_jobs(connection)

    report = _build(tmp_path, profile=load_profile(profile), grouped=False, limit=2)
    # candidate_count is the full eligible untracked pool, not the pre-limited
    # newest-N count.
    assert report.recommendations.candidate_count == 4


def test_recommendations_older_high_scorer_in_grouped_mode(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_recency_jobs(connection)
        ensure_grouped(connection)

    report = _build(tmp_path, profile=load_profile(profile), grouped=True, limit=2)
    ranked = report.recommendations.ranked_groups
    assert len(ranked) == 2
    assert ranked[0].group.representative.source_job_id == "old-high"
    assert ranked[0].group.representative.title == "Python Engineer"
    assert report.recommendations.candidate_count == 4


def test_saved_search_result_limit_still_caps_before_scoring(
    tmp_path: Path,
) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_recency_jobs(connection)
        saved = _create_rank_search(
            tmp_path,
            connection,
            name="engineer",
            query="Engineer",
            profile_path=profile,
            result_limit=1,
        )

    report = _build(tmp_path, saved_search=saved, profile=load_profile(profile))
    ranked = report.recommendations.ranked_jobs
    # The saved-search branch is unchanged: result_limit caps the candidate
    # pool before scoring, so the newest matching job wins, never the older
    # highest-scoring one.
    assert len(ranked) == 1
    assert ranked[0].job.source_job_id == "new-1"
    assert report.recommendations.candidate_count == 1


def test_review_queue_and_recommendations_share_top_job(tmp_path: Path) -> None:
    profile = _seed_profile(tmp_path)
    with _open(tmp_path) as connection:
        _seed_recency_jobs(connection)

    report = _build(tmp_path, profile=load_profile(profile), grouped=False, limit=2)
    queue_ids = [item.group.representative.id for item in report.review.items]
    rec_ids = [job.job.id for job in report.recommendations.ranked_jobs]
    assert queue_ids and rec_ids
    # The top eligible untracked/unreviewed job appears in both sections.
    assert queue_ids[0] == rec_ids[0]


def test_report_is_deterministic_with_fixed_now(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "interview"})
        _seed_run(connection, "jobicy", _EPOCH - timedelta(minutes=30))

    first = _build(tmp_path)
    second = _build(tmp_path)
    assert first == second
    assert render_report(first) == render_report(second)
    assert render_report_json(first) == render_report_json(second)


def test_report_does_not_mutate_database(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    profile = _write_profile(tmp_path)
    with connect(database_path) as connection:
        initialize_schema(connection)
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "interview"})
        _create_rank_search(tmp_path, connection)
        ensure_grouped(connection)
        _seed_run(connection, "jobicy", _EPOCH - timedelta(minutes=30))

        def _snapshot(table: str) -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")
            ]

        tables = [
            "jobs",
            "applications",
            "application_events",
            "job_groups",
            "job_group_members",
            "collection_runs",
            "saved_searches",
            "review_decisions",
            "review_events",
        ]
        before = {table: _snapshot(table) for table in tables}

    with connect(database_path) as connection:
        build_report(
            connection,
            now=_EPOCH,
            profile=load_profile(profile),
        )

    with connect(database_path) as connection:
        for table in tables:
            assert _snapshot(table) == before[table], table


def test_report_works_while_collection_lock_is_held(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "applied"})

    with collection_lock(database_path, command="collect due"):
        report = _build(tmp_path)
    assert report.overview.tracked_applications == 1


def test_render_json_matches_text_data(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        job_ids = _seed_jobs(connection)
        _track(connection, job_ids, {"a-1": "interview"})

    report = _build(tmp_path)
    payload = json.loads(render_report_json(report))
    assert payload["as_of"] == _EPOCH.isoformat()
    assert payload["overview"]["total_jobs"] == 4
    assert payload["overview"]["tracked_applications"] == 1
    assert payload["overview"]["reviewed_groups"] == 0
    assert payload["overview"]["unreviewed_groups"] == 4
    assert payload["application_pipeline"]["status_counts"]["interview"] == 1
    assert payload["action_items"]["interview"][0]["status"] == "interview"
    assert payload["review_queue"]["source"] == "none"
    assert payload["recommendations"]["source"] == "none"
    assert len(payload["collection_health"]["sources"]) == 7


def test_render_text_mentions_career_snapshot(tmp_path: Path) -> None:
    text = render_report(_build(tmp_path))
    assert text.startswith("Career Snapshot")
    assert "CareerOS never infers deadlines or follow-up dates from text." in text


def test_overview_source_stats_include_all_registered_sources(
    tmp_path: Path,
) -> None:
    from careeros.collectors import COLLECTORS

    with _open(tmp_path) as connection:
        _seed_jobs(connection)

    report = _build(tmp_path)
    stats = report.overview.source_stats
    assert tuple(entry.source for entry in stats) == tuple(COLLECTORS)
    for entry in stats:
        assert len(entry.fields) == 8


def test_overview_source_stats_count_missing_fields(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-1",
                    title="Python Engineer",
                    location=None,
                    description=None,
                ),
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-2",
                    title="Python Engineer",
                    location="Munich",
                ),
                _make_job(
                    source="jobicy", source_job_id="j-1", title="Frontend Engineer"
                ),
            ],
        )

    report = _build(tmp_path)
    by_source = {entry.source: entry for entry in report.overview.source_stats}
    arbeitnow = by_source["arbeitnow"]
    assert arbeitnow.total_jobs == 2
    location = next(f for f in arbeitnow.fields if f.field == "location")
    assert location.missing_count == 1
    assert location.provided_count == 1
    assert location.coverage_pct == 50
    description = next(f for f in arbeitnow.fields if f.field == "description")
    assert description.missing_count == 1
    assert description.coverage_pct == 50
    assert by_source["jobicy"].total_jobs == 1


def test_source_stats_include_never_groupable_in_overview(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(
                    source="remoteok",
                    source_job_id="r-1",
                    title="Rust Engineer",
                    company=None,
                ),
                _make_job(
                    source="remoteok", source_job_id="r-2", title="Rust Engineer"
                ),
            ],
        )

    report = _build(tmp_path)
    by_source = {entry.source: entry for entry in report.overview.source_stats}
    assert by_source["remoteok"].never_groupable_jobs == 1


def test_source_stats_text_section_shows_missing_and_coverage(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-1",
                    title="Python Engineer",
                    location=None,
                ),
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-2",
                    title="Python Engineer",
                    location="Berlin",
                ),
            ],
        )

    report = _build(tmp_path)
    text = render_report(report)
    assert "Source quality (fields not provided by source):" in text
    assert "arbeitnow: 2 jobs, never groupable: 0" in text
    assert "location 1" in text
    assert "location 50%" in text
    assert "company 100%" in text


def test_source_stats_text_compact_when_nothing_missing(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        _seed_jobs(connection)

    text = render_report(_build(tmp_path))
    assert "Source quality (fields not provided by source):" in text
    for source in ("arbeitnow", "remoteok", "weworkremotely", "jobicy"):
        assert f"{source}: 1 jobs, never groupable: 0, no fields missing" in text


def test_source_stats_text_empty_database_compact(tmp_path: Path) -> None:
    text = render_report(_build(tmp_path))
    assert "Source quality (fields not provided by source):" in text
    assert "arbeitnow: 0 jobs" in text


def test_source_stats_json_carries_full_counts_and_coverage(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(
                    source="jobicy",
                    source_job_id="j-1",
                    title="Frontend Engineer",
                    location=None,
                ),
                _make_job(
                    source="jobicy", source_job_id="j-2", title="Frontend Engineer"
                ),
            ],
        )

    payload = json.loads(render_report_json(_build(tmp_path)))
    source_stats = payload["overview"]["source_stats"]
    assert len(source_stats) == 7
    by_source = {entry["source"]: entry for entry in source_stats}
    jobicy = by_source["jobicy"]
    assert jobicy["total_jobs"] == 2
    assert jobicy["never_groupable_jobs"] == 0
    location = next(f for f in jobicy["fields"] if f["field"] == "location")
    assert location["missing_count"] == 1
    assert location["provided_count"] == 1
    assert location["coverage_pct"] == 50
    company = next(f for f in jobicy["fields"] if f["field"] == "company")
    assert company["missing_count"] == 0
    assert company["provided_count"] == 2
    assert company["coverage_pct"] == 100


def test_report_and_source_stats_renderers_share_source_stats_dict(
    tmp_path: Path,
) -> None:
    from careeros.presenters import source_stats_dict
    from careeros.source_stats.render import render_source_stats_json

    with _open(tmp_path) as connection:
        _seed_jobs(connection)

    report_payload = json.loads(render_report_json(_build(tmp_path)))
    standalone = json.loads(
        render_source_stats_json(_build(tmp_path).overview.source_stats)
    )

    report_stats = report_payload["overview"]["source_stats"]
    assert report_stats == standalone["sources"]
    assert report_stats == [
        source_stats_dict(entry) for entry in _build(tmp_path).overview.source_stats
    ]
