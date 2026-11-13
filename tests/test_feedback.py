"""Tests for review-feedback ranking: pure module, scorer, adapter, and CLI.

All tests are offline. The pure similarity/adjustment tests use the model
directly; the scorer/adapter/CLI tests use a seeded ``tmp_path`` SQLite
database.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import StoredJob, save_jobs
from careeros.database.schema import initialize_schema
from careeros.ranking.feedback import (
    DISMISS_POINTS,
    MAX_CONTRIBUTING_SOURCES,
    MAX_FEEDBACK_POINTS,
    REVIEW_LATER_POINTS,
    SHORTLIST_POINTS,
    CandidateJob,
    FeedbackSet,
    FeedbackSource,
    compute_feedback_adjustment,
    decision_points,
    feedback_label,
    sources_similar,
)
from careeros.ranking.scorer import CandidateProfile, score_job, score_jobs
from careeros.review.feedback import build_feedback_set
from careeros.review.service import set_decision


def _make_job(
    *,
    job_id: int = 1,
    title: str = "Python Engineer",
    company: str | None = "Acme",
    location: str | None = "Berlin",
    remote_status: str | None = "remote",
    employment_type: str | None = "Full-time",
) -> StoredJob:
    return StoredJob(
        id=job_id,
        source="test",
        source_job_id=f"test-{job_id}",
        title=title,
        company=company,
        location=location,
        description="Build Python backends.",
        url="https://example.com",
        employment_type=employment_type,
        remote_status=remote_status,
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-07-29T12:00:00+00:00",
        created_at="2026-07-29T12:00:00+00:00",
        updated_at="2026-07-29T12:00:00+00:00",
    )


def _source(
    *,
    job_id: int = 1,
    title: str = "Python Engineer",
    company: str | None = "Acme",
    location: str | None = "Berlin",
    remote_status: str | None = "remote",
    employment_type: str | None = "Full-time",
    decision: str = "shortlist",
) -> FeedbackSource:
    return FeedbackSource(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        remote_status=remote_status,
        employment_type=employment_type,
        decision=decision,
    )


def _candidate(
    *,
    job_ids: frozenset[int] = frozenset({2}),
    titles: tuple[str, ...] = ("Python Engineer",),
    company: str | None = "Acme",
    location: str | None = "Berlin",
    remote_status: str | None = "remote",
    employment_type: str | None = "Full-time",
) -> CandidateJob:
    return CandidateJob(
        job_ids=job_ids,
        titles=titles,
        company=company,
        location=location,
        remote_status=remote_status,
        employment_type=employment_type,
    )


# --- (1) fixed weights and validation ---------------------------------------


def test_decision_points_are_fixed() -> None:
    assert decision_points("shortlist") == SHORTLIST_POINTS == 5
    assert decision_points("dismiss") == DISMISS_POINTS == -5
    assert decision_points("review_later") == REVIEW_LATER_POINTS == 0


def test_validate_decision_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="Invalid feedback decision"):
        decision_points("applied")


def test_feedback_bounds_are_documented() -> None:
    assert MAX_CONTRIBUTING_SOURCES == 2
    assert MAX_FEEDBACK_POINTS == 2 * SHORTLIST_POINTS == 10


def test_feedback_label_format() -> None:
    label = feedback_label(_source(job_id=3, title="Senior Engineer"))
    assert label == ("similar to shortlisted job 3 (Senior Engineer, Acme)")


# --- (2) similarity and adjustment (pure) -----------------------------------


def test_similar_source_contributes_plus_five() -> None:
    adjustment = compute_feedback_adjustment(_candidate(), [_source()])
    assert adjustment.points == 5
    assert [source.job_id for source in adjustment.sources] == [1]


def test_candidate_never_receives_feedback_from_itself() -> None:
    adjustment = compute_feedback_adjustment(
        _candidate(job_ids=frozenset({1})), [_source()]
    )
    assert adjustment.points == 0
    assert adjustment.sources == ()


def test_dismiss_contributes_minus_five() -> None:
    adjustment = compute_feedback_adjustment(
        _candidate(), [_source(decision="dismiss")]
    )
    assert adjustment.points == -5


def test_review_later_is_neutral_and_never_occupies_a_slot() -> None:
    later = _source(job_id=1, decision="review_later")
    shortlisted = _source(job_id=3)
    adjustment = compute_feedback_adjustment(_candidate(), [later, shortlisted])
    assert adjustment.points == 5
    assert [source.job_id for source in adjustment.sources] == [3]


def test_at_most_two_sources_contribute() -> None:
    sources = [_source(job_id=job_id) for job_id in (1, 3, 5)]
    adjustment = compute_feedback_adjustment(_candidate(), sources)
    assert adjustment.points == 2 * SHORTLIST_POINTS
    assert len(adjustment.sources) == MAX_CONTRIBUTING_SOURCES


def test_different_company_requires_specific_titles() -> None:
    candidate = _candidate(company="Other Corp")
    assert sources_similar(candidate, _source(title="Python Engineer")) is True
    assert sources_similar(candidate, _source(title="Engineer")) is False


def test_remote_conflict_vetoes_similarity() -> None:
    candidate = _candidate(remote_status="remote")
    source = _source(remote_status="onsite_or_unspecified")
    assert sources_similar(candidate, source) is False
    assert compute_feedback_adjustment(candidate, [source]).points == 0


def test_location_conflict_vetoes_similarity() -> None:
    candidate = _candidate(location="Berlin")
    source = _source(location="Munich")
    assert sources_similar(candidate, source) is False


def test_compatible_location_prefixes_are_similar() -> None:
    candidate = _candidate(location="Berlin")
    source = _source(location="Berlin, Germany")
    assert sources_similar(candidate, source) is True


def test_employment_type_conflict_vetoes_similarity() -> None:
    candidate = _candidate(employment_type="Full-time")
    source = _source(employment_type="Contract")
    assert sources_similar(candidate, source) is False


def test_missing_location_is_not_a_conflict() -> None:
    candidate = _candidate(location=None)
    assert sources_similar(candidate, _source(location="Berlin")) is True


# --- (3) scorer integration --------------------------------------------------


def test_score_job_applies_feedback_and_exposes_component() -> None:
    job = _make_job(job_id=2)
    feedback = FeedbackSet(sources=(_source(job_id=1),))
    profile = CandidateProfile(skills=("Python",))

    plain = score_job(job, profile)
    adjusted = score_job(job, profile, feedback=feedback)

    assert adjusted.score == plain.score + SHORTLIST_POINTS
    feedback_kinds = [
        component.kind
        for component in adjusted.result.components
        if component.kind == "feedback"
    ]
    assert feedback_kinds == ["feedback"]
    assert adjusted.result.feedback is not None
    assert adjusted.result.feedback.points == SHORTLIST_POINTS
    assert plain.result.feedback is None


def test_feedback_never_appears_in_reasons() -> None:
    job = _make_job(job_id=2)
    feedback = FeedbackSet(sources=(_source(job_id=1),))
    adjusted = score_job(job, CandidateProfile(), feedback=feedback)
    assert not any("shortlist" in reason for reason in adjusted.reasons)
    assert adjusted.match_score == adjusted.score


def test_feedback_is_bounded_and_never_bypasses_hard_exclusion() -> None:
    job = _make_job(job_id=2, title="Intern Engineer")
    feedback = FeedbackSet(
        sources=(
            _source(job_id=1),
            _source(job_id=3),
            _source(job_id=5),
        )
    )
    profile = CandidateProfile(hard_excluded_terms=("intern",))
    adjusted = score_job(job, profile, feedback=feedback)
    assert adjusted.match_score == 0
    assert adjusted.result.hard_excluded_terms == ("intern",)


def test_score_jobs_orders_by_adjusted_score() -> None:
    plain = _make_job(job_id=10, title="Data Scientist", company="Data Inc")
    boosted = _make_job(job_id=20, title="Python Engineer", company="Acme")
    feedback = FeedbackSet(sources=(_source(job_id=1),))
    profile = CandidateProfile(skills=("Python",))

    ranked = score_jobs([plain, boosted], profile, feedback=feedback)
    assert [entry.job.id for entry in ranked] == [20, 10]


# --- (4) read-only review adapter -------------------------------------------


def _open(tmp_path: Path) -> sqlite3.Connection:
    database_path = tmp_path / "careeros.sqlite3"
    connection = connect(database_path)
    initialize_schema(connection)
    return connection


def _collected(
    *,
    source_job_id: str,
    title: str,
    company: str,
) -> CollectedJob:
    return CollectedJob(
        source="arbeitnow",
        source_job_id=source_job_id,
        title=title,
        company=company,
        location="Berlin",
        description="Python backend engineering.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def test_build_feedback_set_is_empty_without_decisions(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [_collected(source_job_id="a", title="Engineer", company="Acme")],
        )
        assert build_feedback_set(connection) == FeedbackSet()


def test_build_feedback_set_collapses_cross_source_duplicates(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _collected(
                    source_job_id="a-1", title="Python Engineer", company="Acme"
                ),
                _collected(
                    source_job_id="a-2", title="Python Engineer", company="Acme"
                ),
            ],
        )
        ensure_grouped(connection)
        set_decision(connection, job_id=1, decision="shortlist")
        set_decision(connection, job_id=2, decision="shortlist")

        feedback = build_feedback_set(connection)

    assert len(feedback.sources) == 1
    assert feedback.sources[0].job_id == 1
    assert feedback.sources[0].decision == "shortlist"


def test_build_feedback_set_is_deterministic_and_ordered(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _collected(source_job_id="b", title="Backend Engineer", company="Beta"),
                _collected(
                    source_job_id="a", title="Frontend Developer", company="Alpha"
                ),
            ],
        )
        set_decision(connection, job_id=2, decision="shortlist")
        set_decision(connection, job_id=1, decision="dismiss")

        feedback = build_feedback_set(connection)

    assert [source.job_id for source in feedback.sources] == [1, 2]
    assert feedback.sources[0].decision == "dismiss"


# --- (5) CLI integration -----------------------------------------------------


def _seed_cli(connection) -> None:
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _collected(
                source_job_id="decided", title="Python Engineer", company="Acme"
            ),
            _collected(
                source_job_id="candidate", title="Python Engineer", company="Acme"
            ),
        ],
    )
    set_decision(connection, job_id=1, decision="shortlist")


def _profile_path(tmp_path: Path) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"skills": ["Python"]}), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_rank_json_exposes_score_components(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import run_rank

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        _seed_cli(connection)

    exit_code = run_rank(
        "Python", _profile_path(tmp_path), db_path, output_format="json"
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["grouped"] is False
    assert len(payload["jobs"]) == 2
    components = payload["jobs"][0]["score_components"]
    kinds = {component["kind"] for component in components}
    assert "feedback" in kinds
    feedback = next(c for c in components if c["kind"] == "feedback")
    assert feedback["points"] == 5
    assert feedback["is_veto"] is False
    assert set(feedback) == {"kind", "label", "points", "is_veto"}


def test_rank_text_shows_feedback_line_only_with_feedback(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import run_rank

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        _seed_cli(connection)

    exit_code = run_rank("Python", _profile_path(tmp_path), db_path)
    assert exit_code == 0
    assert "Feedback:" in capsys.readouterr().out

    exit_code = run_rank("Python", _profile_path(tmp_path), db_path, use_feedback=False)
    assert exit_code == 0
    assert "Feedback:" not in capsys.readouterr().out


def test_no_feedback_flag_through_main(tmp_path: Path) -> None:
    from careeros.cli import main

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        _seed_cli(connection)

    exit_code = main(
        [
            "rank",
            "Python",
            "--profile",
            str(_profile_path(tmp_path)),
            "--database",
            str(db_path),
            "--no-feedback",
            "--format",
            "json",
        ]
    )
    assert exit_code == 0


def test_review_next_shows_feedback_line(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import run_review_next

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        _seed_cli(connection)

    exit_code = run_review_next(
        db_path,
        profile_path=_profile_path(tmp_path),
        state="unreviewed",
        min_score=0,
        include_tracked=False,
        source=None,
        location=None,
        remote=None,
        limit=20,
        grouped=True,
        output_format="text",
    )
    assert exit_code == 0
    assert "Feedback:" in capsys.readouterr().out

    exit_code = run_review_next(
        db_path,
        profile_path=_profile_path(tmp_path),
        state="unreviewed",
        min_score=0,
        include_tracked=False,
        source=None,
        location=None,
        remote=None,
        limit=20,
        grouped=True,
        output_format="text",
        use_feedback=False,
    )
    assert exit_code == 0
    assert "Feedback:" not in capsys.readouterr().out


def test_review_next_json_carries_score_components(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import run_review_next

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        _seed_cli(connection)

    exit_code = run_review_next(
        db_path,
        profile_path=_profile_path(tmp_path),
        state="unreviewed",
        min_score=0,
        include_tracked=False,
        source=None,
        location=None,
        remote=None,
        limit=20,
        grouped=True,
        output_format="json",
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    item = next(entry for entry in payload["items"])
    kinds = {component["kind"] for component in item["score_components"]}
    assert "feedback" in kinds


def test_grouped_rank_applies_feedback_per_group(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from careeros.cli import run_rank

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _collected(
                    source_job_id="decided", title="Python Engineer", company="Acme"
                ),
                _collected(
                    source_job_id="similar", title="Python Engineer", company="Beta Inc"
                ),
                _collected(
                    source_job_id="other", title="DevOps Engineer", company="Ops Co"
                ),
            ],
        )
        ensure_grouped(connection)
        set_decision(connection, job_id=1, decision="shortlist")

    exit_code = run_rank("Python", _profile_path(tmp_path), db_path, grouped=True)
    assert exit_code == 0
    assert "Feedback:" in capsys.readouterr().out


def test_report_shows_feedback_in_recommendations(tmp_path: Path) -> None:
    from careeros.cli import main

    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        _seed_cli(connection)

    exit_code = main(
        [
            "report",
            "--profile",
            str(_profile_path(tmp_path)),
            "--database",
            str(db_path),
        ]
    )
    assert exit_code == 0
