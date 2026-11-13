"""Focused R4 M4 adaptive scoring and surface-propagation coverage."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from careeros import cli
from careeros.api.main import create_app
from careeros.cli.parser import build_parser
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped, list_all_groups
from careeros.database.jobs import save_jobs, search_jobs
from careeros.database.schema import initialize_schema
from careeros.export.models import ExportRequest
from careeros.export.service import build_ranked_records, build_review_records
from careeros.preferences.learning import LearnedPreference
from careeros.preferences.models import AdaptivePreferenceSet, ExplicitPreferenceProfile
from careeros.preferences.service import (
    build_adaptive_preference_set,
    replace_explicit_profile,
    restore_learned_preference,
    suppress_learned_preference,
)
from careeros.ranking.adaptive import (
    MAX_LEARNED_DISPLAY_VALUE_LENGTH,
    MAX_LEARNED_DISPLAY_VALUES,
    AdaptiveCandidate,
    LearnedMatch,
    RawLearnedAdjustment,
    apply_legacy_envelope,
    compute_raw_learned_adjustment,
    learned_display_summary,
    learned_display_values,
)
from careeros.ranking.explanations import explain_result, explanation_to_dict
from careeros.ranking.feedback import CandidateJob, FeedbackSet, FeedbackSource
from careeros.ranking.scorer import CandidateProfile, MatchFacts, score_facts
from careeros.reporting.service import build_report
from careeros.review.feedback import build_feedback_set
from careeros.review.service import build_queue, set_decision
from careeros.saved_searches.execution import execute_search, rank_candidates
from careeros.saved_searches.models import SavedSearch, SavedSearchFields
from careeros.saved_searches.service import create_search
from tests.support import TestClient

NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)


def _learned(
    dimension: str,
    value: str,
    *,
    polarity: str = "positive",
    strength: int = 2,
    active: bool = True,
    suppressed: bool = False,
) -> LearnedPreference:
    return LearnedPreference(
        preference_key=(dimension[0] * 32),
        dimension=dimension,
        normalized_value=value,
        display_value=value,
        positive_count=3 if polarity == "positive" else 0,
        negative_count=3 if polarity == "negative" else 0,
        net_evidence=3 if polarity == "positive" else -3,
        active=active,
        polarity=polarity if active or suppressed else None,
        strength_level=strength if active or suppressed else 0,
        suppressed=suppressed,
        activation_reason="threshold met",
        positive_examples=(),
        negative_examples=(),
    )


def _candidate() -> AdaptiveCandidate:
    return AdaptiveCandidate(
        titles=("Senior Backend Engineer",),
        company="Acme GmbH",
        location="Berlin, Germany",
        remote_status="remote",
        employment_types=("full time",),
        seniority_levels=("senior",),
    )


def _all_dimensions(polarity: str = "positive") -> AdaptivePreferenceSet:
    return AdaptivePreferenceSet(
        learned=(
            _learned("title", "senior backend engineer", polarity=polarity),
            _learned("company", "acme", polarity=polarity),
            _learned("location", "berlin", polarity=polarity),
            _learned("remote", "remote", polarity=polarity),
            _learned("employment_type", "full time", polarity=polarity),
            _learned("seniority", "senior", polarity=polarity),
        ),
        locked_dimensions=frozenset(),
    )


@pytest.mark.parametrize(
    ("legacy", "raw", "effective"),
    [(5, 8, 5), (0, 8, 8), (-5, -8, -5), (0, -8, -8)],
)
def test_combined_legacy_and_learned_envelope(
    legacy: int, raw: int, effective: int
) -> None:
    adjustment = apply_legacy_envelope(
        RawLearnedAdjustment(points=raw, matches=()),
        legacy_feedback=legacy,
    )
    assert adjustment is not None
    assert adjustment.effective_points == effective


def test_zero_effective_headroom_emits_no_adaptive_adjustment() -> None:
    assert (
        apply_legacy_envelope(
            RawLearnedAdjustment(points=8, matches=()),
            legacy_feedback=10,
        )
        is None
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["rank", "Engineer", "--profile", "profile.json", "--no-adaptive"],
        ["review", "next", "--profile", "profile.json", "--no-adaptive"],
        ["report", "--profile", "profile.json", "--no-adaptive"],
        ["saved-search", "run", "ranked", "--no-adaptive"],
        [
            "export",
            "ranked",
            "--query",
            "Engineer",
            "--profile",
            "profile.json",
            "--no-adaptive",
        ],
        ["export", "review", "--profile", "profile.json", "--no-adaptive"],
    ],
)
def test_all_frozen_cli_surfaces_accept_distinct_adaptive_opt_out(
    arguments: list[str],
) -> None:
    parsed = build_parser().parse_args(arguments)
    assert parsed.no_adaptive is True


def test_raw_learning_uses_frozen_weights_dimension_caps_and_global_bounds() -> None:
    positive = compute_raw_learned_adjustment(_candidate(), _all_dimensions())
    negative = compute_raw_learned_adjustment(_candidate(), _all_dimensions("negative"))
    assert positive.points == 8
    assert negative.points == -8
    assert [item.dimension for item in positive.matches] == [
        "title",
        "company",
        "location",
        "remote",
        "employment_type",
        "seniority",
    ]
    title_capped = compute_raw_learned_adjustment(
        AdaptiveCandidate(
            titles=("Senior Backend Engineer", "Senior Backend"),
            company=None,
            location=None,
            remote_status=None,
            employment_types=(),
            seniority_levels=(),
        ),
        AdaptivePreferenceSet(
            learned=(
                _learned("title", "senior backend engineer"),
                _learned("title", "senior backend"),
            ),
            locked_dimensions=frozenset(),
        ),
    )
    assert title_capped.points == 4


def test_zero_inactive_suppressed_locked_and_opted_out_state_is_neutral() -> None:
    inactive = AdaptivePreferenceSet(
        learned=(_learned("title", "senior backend engineer", active=False),),
        locked_dimensions=frozenset(),
    )
    suppressed = AdaptivePreferenceSet(
        learned=(
            _learned("title", "senior backend engineer", active=False, suppressed=True),
        ),
        locked_dimensions=frozenset(),
    )
    locked = AdaptivePreferenceSet(
        learned=(_learned("title", "senior backend engineer"),),
        locked_dimensions=frozenset({"title"}),
    )
    for preferences in (
        AdaptivePreferenceSet.disabled(),
        inactive,
        suppressed,
        locked,
    ):
        assert compute_raw_learned_adjustment(_candidate(), preferences).points == 0


def test_scorer_applies_effective_points_without_reviving_hard_exclusions() -> None:
    facts = MatchFacts(
        titles=("Senior Backend Engineer",),
        company="Acme",
        description="blocked term",
        location="Berlin",
        remote_status="remote",
        employment_type="Full-time",
    )
    candidate = CandidateJob(
        job_ids=frozenset({99}),
        titles=facts.titles,
        company=facts.company,
        location=facts.location,
        remote_status=facts.remote_status,
        employment_type=facts.employment_type,
    )
    feedback = FeedbackSet(
        sources=(
            FeedbackSource(
                job_id=1,
                title="Senior Backend Engineer",
                company="Acme",
                location="Berlin",
                remote_status="remote",
                employment_type="Full-time",
                decision="shortlist",
            ),
        )
    )
    result = score_facts(
        facts,
        CandidateProfile(hard_excluded_terms=("blocked",)),
        feedback=feedback,
        candidate=candidate,
        adaptive_preferences=_all_dimensions(),
    )
    assert result.feedback is not None and result.feedback.points == 5
    assert result.adaptive is not None
    assert result.adaptive.raw_points == 8
    assert result.adaptive.effective_points == 5
    assert result.preference_adjustment == 5
    assert result.match_score == 0
    assert result.score == 10

    no_adaptive = score_facts(
        facts,
        CandidateProfile(hard_excluded_terms=("blocked",)),
        feedback=feedback,
        candidate=candidate,
        adaptive_preferences=AdaptivePreferenceSet.disabled(),
    )
    assert no_adaptive.feedback is not None and no_adaptive.feedback.points == 5
    assert no_adaptive.score == 5
    assert no_adaptive.adaptive is None

    explanation = explanation_to_dict(explain_result(result))
    assert explanation["score"] == {
        "base_score": 5,
        "preference_adjustment": 5,
        "contribution_total": 10,
        "match_score": 0,
        "resolution": "excluded",
    }
    learned = [
        factor
        for factor in explanation["other_factors"]
        if factor["criterion"] == "learned_preference"
    ]
    assert len(learned) == 1
    assert learned[0]["points"] == 5
    assert set(learned[0]) == {"criterion", "label", "value", "effect", "points"}
    assert "blocked term" not in repr(learned[0])


def _job(source_id: str, title: str, company: str) -> CollectedJob:
    return CollectedJob(
        source="arbeitnow",
        source_job_id=source_id,
        title=title,
        company=company,
        location=company,
        description="Build reliable systems.",
        url=f"https://example.test/{source_id}",
        employment_type=None,
        remote_status="onsite_or_unspecified",
        published_at="2026-09-15T10:00:00+00:00",
        collected_at=NOW.isoformat(),
    )


def _seed_learning(connection: sqlite3.Connection) -> int:
    save_jobs(
        connection,
        [
            _job("positive-1", "Backend Engineer", "Alpha"),
            _job("positive-2", "Backend Engineer", "Beta"),
            _job("target", "Backend Engineer", "Gamma"),
        ],
    )
    ensure_grouped(connection)
    ids = [int(row[0]) for row in connection.execute("SELECT id FROM jobs ORDER BY id")]
    set_decision(connection, job_id=ids[0], decision="shortlist", now=NOW)
    set_decision(connection, job_id=ids[1], decision="shortlist", now=NOW)
    return ids[2]


def test_configured_and_supplied_empty_authority_create_zero_locks(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "authority.sqlite3") as connection:
        initialize_schema(connection)
        _seed_learning(connection)
        replace_explicit_profile(connection, ExplicitPreferenceProfile())

        configured_empty = build_adaptive_preference_set(connection)
        supplied_empty = build_adaptive_preference_set(
            connection,
            supplied_profile=CandidateProfile(),
        )
        assert configured_empty.locked_dimensions == frozenset()
        assert supplied_empty.locked_dimensions == frozenset()
        backend_candidate = AdaptiveCandidate(
            titles=("Backend Engineer",),
            company=None,
            location=None,
            remote_status=None,
            employment_types=(),
            seniority_levels=(),
        )
        assert (
            compute_raw_learned_adjustment(backend_candidate, configured_empty).points
            > 0
        )
        assert (
            compute_raw_learned_adjustment(backend_candidate, supplied_empty).points > 0
        )

        replace_explicit_profile(
            connection,
            ExplicitPreferenceProfile(desired_titles=("Backend Engineer",)),
        )
        assert build_adaptive_preference_set(connection).locked_dimensions == {"title"}
        assert (
            build_adaptive_preference_set(
                connection,
                supplied_profile=CandidateProfile(),
            ).locked_dimensions
            == frozenset()
        )


def test_suppression_restore_and_all_shared_ranked_surfaces(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "surfaces.sqlite3") as connection:
        initialize_schema(connection)
        target_id = _seed_learning(connection)
        profile = CandidateProfile()

        ranked = rank_candidates(
            connection,
            query="Gamma",
            profile=profile,
            feedback=None,
        )
        assert [item.job.id for item in ranked.flat_ranked] == [target_id]
        assert ranked.flat_ranked[0].score == 2
        assert ranked.flat_ranked[0].result.preference_adjustment == 2

        opted_out = rank_candidates(
            connection,
            query="Gamma",
            profile=profile,
            feedback=None,
            use_adaptive=False,
        )
        assert opted_out.flat_ranked[0].score == 0
        assert opted_out.flat_ranked[0].result.adaptive is None

        queue = build_queue(connection, profile=profile, feedback=None)
        target_queue_item = next(
            item for item in queue.items if item.group.representative.id == target_id
        )
        assert target_queue_item.ranked.score == 2

        report = build_report(
            connection,
            now=NOW,
            profile=profile,
            feedback=None,
        )
        target_recommendation = next(
            item
            for item in report.recommendations.ranked_jobs
            if item.job.id == target_id
        )
        assert target_recommendation.score == 2

        saved = SavedSearch(
            name="gamma",
            query="Gamma",
            mode="rank",
            profile_path="unused.json",
            id=1,
            created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        )
        from careeros.saved_searches.execution import execute_rank

        saved_result = execute_rank(
            connection,
            saved,
            profile=profile,
            feedback=None,
        )
        assert saved_result.flat_ranked[0].score == 2

        title_preference = next(
            item
            for item in build_adaptive_preference_set(connection).learned
            if item.dimension == "title" and item.normalized_value == "backend engineer"
        )
        suppressed = suppress_learned_preference(
            connection, title_preference.preference_key, now=NOW
        )
        assert suppressed.suppressed is True and suppressed.active is False
        assert (
            rank_candidates(connection, query="Gamma", profile=profile, feedback=None)
            .flat_ranked[0]
            .score
            == 0
        )

        restored = restore_learned_preference(
            connection, title_preference.preference_key
        )
        assert restored.suppressed is False and restored.active is True
        assert (
            rank_candidates(connection, query="Gamma", profile=profile, feedback=None)
            .flat_ranked[0]
            .score
            == 2
        )


def _surface_job(
    source_id: str,
    *,
    title: str,
    company: str,
    location: str | None,
    published_at: str | None = "2026-09-15T10:00:00+00:00",
    description: str = "Build reliable systems.",
    employment_type: str | None = "Full-time",
    remote_status: str = "remote",
    source: str = "arbeitnow",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=f"https://example.test/{source_id}",
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=published_at,
        collected_at=NOW.isoformat(),
    )


def _write_empty_profile(path: Path) -> None:
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o600)


def _seed_positive_surface(tmp_path: Path) -> tuple[Path, Path, int]:
    """Create raw learned +8 and exactly one legacy +5 source for the target."""
    database = tmp_path / "adaptive-positive.sqlite3"
    profile_path = tmp_path / "profile.json"
    _write_empty_profile(profile_path)
    with connect(database) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _surface_job(
                    "learn-berlin",
                    title="Backend Engineer",
                    company="Alpha",
                    location="Berlin",
                ),
                _surface_job(
                    "learn-munich",
                    title="Backend Engineer",
                    company="Beta",
                    location="Munich",
                ),
                _surface_job(
                    "learn-paris",
                    title="Backend Engineer",
                    company="Delta",
                    location="Paris",
                ),
                _surface_job(
                    "target",
                    title="Backend Engineer Platform",
                    company="TargetCo",
                    location="Berlin",
                    published_at="2026-09-14T10:00:00+00:00",
                ),
            ],
        )
        ensure_grouped(connection)
        ids = {
            row["source_job_id"]: int(row["id"])
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        for source_id in ("learn-berlin", "learn-munich", "learn-paris"):
            set_decision(
                connection,
                job_id=ids[source_id],
                decision="shortlist",
                now=NOW,
            )
        create_search(
            connection,
            fields=SavedSearchFields(
                name="adaptive-target",
                query="TargetCo",
                mode="rank",
                profile_path=str(profile_path),
            ),
        )
    return database, profile_path, ids["target"]


def _component_points(record: dict[str, object]) -> dict[str, int]:
    return {
        str(component["kind"]): int(component["points"])
        for component in record["score_components"]  # type: ignore[union-attr]
    }


@pytest.mark.parametrize("builder_name", ["ranked", "review"])
def test_exports_forward_adaptive_and_preserve_independent_legacy_feedback(
    tmp_path: Path,
    builder_name: str,
) -> None:
    database, profile_path, target_id = _seed_positive_surface(tmp_path)
    builder = build_ranked_records if builder_name == "ranked" else build_review_records

    def exported(*, use_feedback: bool, use_adaptive: bool) -> dict[str, object]:
        request = ExportRequest(
            profile=profile_path,
            query="TargetCo" if builder_name == "ranked" else None,
            grouped=False,
            use_feedback=use_feedback,
            use_adaptive=use_adaptive,
        )
        with connect(database) as connection:
            records = builder(connection, request).records
        record = next(item for item in records if item["id"] == target_id)
        return record

    default = exported(use_feedback=True, use_adaptive=True)
    no_adaptive = exported(use_feedback=True, use_adaptive=False)
    no_feedback = exported(use_feedback=False, use_adaptive=True)
    neither = exported(use_feedback=False, use_adaptive=False)

    assert [
        default["score"],
        no_adaptive["score"],
        no_feedback["score"],
        neither["score"],
    ] == [10, 5, 8, 0]
    assert _component_points(default)["feedback"] == 5
    assert _component_points(default)["learned_preference"] == 5
    assert _component_points(no_adaptive) == {"feedback": 5}
    assert _component_points(no_feedback) == {"learned_preference": 8}
    assert _component_points(neither) == {}


def _cli_command(
    surface: str,
    *,
    database: Path,
    profile_path: Path,
    flags: tuple[str, ...] = (),
) -> list[str]:
    commands = {
        "rank": [
            "rank",
            "TargetCo",
            "--profile",
            str(profile_path),
            "--format",
            "json",
        ],
        "review": [
            "review",
            "next",
            "--profile",
            str(profile_path),
            "--format",
            "json",
        ],
        "saved-search": ["saved-search", "run", "adaptive-target"],
        "report": [
            "report",
            "--profile",
            str(profile_path),
            "--format",
            "json",
        ],
        "export-ranked": [
            "export",
            "ranked",
            "--query",
            "TargetCo",
            "--profile",
            str(profile_path),
        ],
        "export-review": [
            "export",
            "review",
            "--profile",
            str(profile_path),
            "--no-grouped",
        ],
    }
    return [*commands[surface], *flags, "--database", str(database)]


def _target_scores_from_cli(surface: str, output: str, target_id: int) -> list[int]:
    if surface == "saved-search":
        match = re.search(r"\[Score (\d+)/100\] Backend Engineer Platform", output)
        assert match is not None
        return [int(match.group(1))]

    payload = json.loads(output)
    if surface == "rank":
        item = next(row for row in payload["jobs"] if row["job"]["id"] == target_id)
        return [item["score"]]
    if surface == "review":
        item = next(
            row
            for row in payload["items"]
            if row["group"]["representative"]["id"] == target_id
        )
        return [item["score"]]
    if surface == "report":
        recommendation = next(
            row
            for row in payload["recommendations"]["ranked_jobs"]
            if row["job"]["id"] == target_id
        )
        queue_item = next(
            row
            for row in payload["review_queue"]["items"]
            if row["group"]["representative"]["id"] == target_id
        )
        return [recommendation["score"], queue_item["match_score"]]
    record = next(row for row in payload["records"] if row["id"] == target_id)
    return [record["score"]]


@pytest.mark.parametrize(
    ("surface", "supports_no_feedback"),
    [
        ("rank", True),
        ("review", True),
        ("saved-search", False),
        ("report", True),
        ("export-ranked", True),
        ("export-review", True),
    ],
)
def test_ranked_cli_surfaces_propagate_adaptive_control_without_repurposing_feedback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    surface: str,
    supports_no_feedback: bool,
) -> None:
    database, profile_path, target_id = _seed_positive_surface(tmp_path)

    def run(*flags: str) -> tuple[list[int], str]:
        assert (
            cli.main(
                _cli_command(
                    surface,
                    database=database,
                    profile_path=profile_path,
                    flags=tuple(flags),
                )
            )
            == 0
        )
        output = capsys.readouterr().out
        return _target_scores_from_cli(surface, output, target_id), output

    default_scores, default_output = run()
    no_adaptive_scores, no_adaptive_output = run("--no-adaptive")
    assert default_scores == [10] * len(default_scores)
    assert no_adaptive_scores == [5] * len(no_adaptive_scores)
    assert "Learned preferences:" in default_output
    assert "Learned preferences:" not in no_adaptive_output

    if supports_no_feedback:
        no_feedback_scores, _ = run("--no-feedback")
        neither_scores, _ = run("--no-feedback", "--no-adaptive")
        assert no_feedback_scores == [8] * len(no_feedback_scores)
        assert neither_scores == [0] * len(neither_scores)


def _single_feedback(decision: str) -> FeedbackSet:
    return FeedbackSet(
        sources=(
            FeedbackSource(
                job_id=1,
                title="Senior Backend Engineer",
                company="Acme",
                location="Berlin",
                remote_status="remote",
                employment_type="Full-time",
                decision=decision,
            ),
        )
    )


@pytest.mark.parametrize(
    ("legacy", "polarity", "effective"),
    [
        (5, "positive", 5),
        (0, "positive", 8),
        (-5, "negative", -5),
        (0, "negative", -8),
        (5, "negative", -8),
        (-5, "positive", 8),
    ],
)
def test_real_scorer_applies_headroom_and_opposite_direction_adjustments(
    legacy: int,
    polarity: str,
    effective: int,
) -> None:
    facts = MatchFacts(
        titles=("Senior Backend Engineer",),
        company="Acme",
        location="Berlin",
        remote_status="remote",
        employment_type="Full-time",
    )
    candidate = CandidateJob(
        job_ids=frozenset({99}),
        titles=facts.titles,
        company=facts.company,
        location=facts.location,
        remote_status=facts.remote_status,
        employment_type=facts.employment_type,
    )
    feedback = (
        _single_feedback("shortlist" if legacy > 0 else "dismiss") if legacy else None
    )
    result = score_facts(
        facts,
        CandidateProfile(),
        feedback=feedback,
        candidate=candidate,
        adaptive_preferences=_all_dimensions(polarity),
    )

    if legacy == 0:
        assert result.feedback is None
    else:
        assert result.feedback is not None
    if result.feedback is not None:
        assert result.feedback.points == legacy
    assert result.adaptive is not None
    assert result.adaptive.raw_points == (8 if polarity == "positive" else -8)
    assert result.adaptive.effective_points == effective
    assert result.preference_adjustment == effective
    assert result.score == legacy + effective
    component = next(
        item for item in result.components if item.kind == "learned_preference"
    )
    assert component.points == effective
    explanation = explain_result(result)
    factor = next(
        item
        for item in explanation.other_factors
        if item.criterion == "learned_preference"
    )
    assert factor.points == effective


def test_different_base_surfaces_share_raw_learning_but_keep_their_legacy_headroom(
    tmp_path: Path,
) -> None:
    database, _, target_id = _seed_positive_surface(tmp_path)
    profile = CandidateProfile()
    with connect(database) as connection:
        preferences = build_adaptive_preference_set(
            connection, supplied_profile=profile
        )
        feedback = build_feedback_set(connection)
        service = rank_candidates(
            connection,
            query="TargetCo",
            profile=profile,
            feedback=feedback,
            adaptive_preferences=preferences,
        ).flat_ranked[0]
        http_style = rank_candidates(
            connection,
            query="TargetCo",
            profile=profile,
            feedback=None,
            adaptive_preferences=preferences,
        ).flat_ranked[0]

    assert service.job.id == http_style.job.id == target_id
    assert service.result.feedback is not None
    assert http_style.result.feedback is None
    assert service.result.adaptive is not None
    assert http_style.result.adaptive is not None
    assert (
        service.result.adaptive.raw_points == http_style.result.adaptive.raw_points == 8
    )
    assert service.result.adaptive.effective_points == 5
    assert http_style.result.adaptive.effective_points == 8
    assert (service.score, http_style.score) == (10, 8)
    assert service.result.preference_adjustment == 5
    assert http_style.result.preference_adjustment == 8


def test_negative_learned_authority_lowers_real_surface_score_and_order(
    tmp_path: Path,
) -> None:
    database = tmp_path / "adaptive-negative.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                *(
                    _surface_job(
                        f"dismiss-{index}",
                        title="Backend Engineer",
                        company=f"Dismissed {index}",
                        location="Berlin",
                        employment_type=None,
                        published_at=f"2026-09-0{index}T10:00:00+00:00",
                    )
                    for index in range(1, 4)
                ),
                _surface_job(
                    "negative-target",
                    title="Backend Engineer Candidate",
                    company="Negative Target",
                    location="Berlin",
                    description="Python systems",
                    employment_type=None,
                    published_at="2026-09-16T10:00:00+00:00",
                ),
                _surface_job(
                    "neutral",
                    title="Frontend Candidate",
                    company="Neutral",
                    location=None,
                    description="Frontend systems",
                    employment_type="Contract",
                    remote_status="onsite_or_unspecified",
                    published_at="2026-09-15T10:00:00+00:00",
                ),
            ],
        )
        ensure_grouped(connection)
        ids = {
            row["source_job_id"]: int(row["id"])
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        for index in range(1, 4):
            set_decision(
                connection,
                job_id=ids[f"dismiss-{index}"],
                decision="dismiss",
                now=NOW,
            )
        profile = CandidateProfile(
            skills=("Python",), preferred_employment_types=("Contract",)
        )
        adaptive = rank_candidates(
            connection,
            query="Candidate",
            profile=profile,
            feedback=None,
        )
        opted_out = rank_candidates(
            connection,
            query="Candidate",
            profile=profile,
            feedback=None,
            use_adaptive=False,
        )

    assert [item.job.id for item in adaptive.flat_ranked] == [
        ids["neutral"],
        ids["negative-target"],
    ]
    assert [item.job.id for item in opted_out.flat_ranked] == [
        ids["negative-target"],
        ids["neutral"],
    ]
    target = next(
        item for item in adaptive.flat_ranked if item.job.id == ids["negative-target"]
    )
    target_without = next(
        item for item in opted_out.flat_ranked if item.job.id == ids["negative-target"]
    )
    assert (target.score, target_without.score) == (0, 8)
    assert target.result.adaptive is not None
    assert target.result.adaptive.raw_points == -8
    assert target.result.adaptive.effective_points == -8
    assert target.result.hard_excluded_terms == ()
    explanation = explain_result(target.result)
    learned = next(
        factor
        for factor in explanation.other_factors
        if factor.criterion == "learned_preference"
    )
    assert learned.points == -8
    assert learned.effect == "negative"
    assert target_without.result.adaptive is None


def test_grouped_and_flat_adaptive_ranking_preserves_groups_and_tie_breakers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "adaptive-ordering.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _surface_job(
                    "alpha-a",
                    title="Backend Engineer",
                    company="Alpha GmbH",
                    location="Berlin",
                    published_at="2026-09-12T10:00:00+00:00",
                ),
                _surface_job(
                    "alpha-b",
                    title="Backend Engineer",
                    company="Alpha",
                    location="Berlin, Germany",
                    published_at="2026-09-12T10:00:00+00:00",
                    source="remoteok",
                ),
                _surface_job(
                    "beta",
                    title="Backend Engineer",
                    company="Beta",
                    location="Berlin",
                    published_at="2026-09-16T10:00:00+00:00",
                ),
                _surface_job(
                    "gamma",
                    title="Backend Engineer",
                    company="Gamma",
                    location="Berlin",
                    published_at="2026-09-16T10:00:00+00:00",
                ),
                _surface_job(
                    "unknown-date",
                    title="Backend Engineer",
                    company="Unknown Date",
                    location="Berlin",
                    published_at=None,
                ),
                _surface_job(
                    "neutral",
                    title="Frontend Engineer",
                    company="Neutral",
                    location=None,
                    published_at="2026-09-15T10:00:00+00:00",
                    remote_status="onsite_or_unspecified",
                ),
            ],
        )
        ensure_grouped(connection)
        ids = {
            row["source_job_id"]: int(row["id"])
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        groups_before = [
            (
                group.group_id,
                group.representative.id,
                tuple(m.id for m in group.members),
            )
            for group in list_all_groups(connection)
        ]
        preferences = AdaptivePreferenceSet(
            learned=(_learned("title", "backend engineer"),),
            locked_dimensions=frozenset(),
        )
        flat = rank_candidates(
            connection,
            query="",
            profile=CandidateProfile(),
            grouped=False,
            feedback=None,
            adaptive_preferences=preferences,
        )
        grouped = rank_candidates(
            connection,
            query="",
            profile=CandidateProfile(),
            grouped=True,
            feedback=None,
            adaptive_preferences=preferences,
        )
        flat_without = rank_candidates(
            connection,
            query="",
            profile=CandidateProfile(),
            grouped=False,
            feedback=None,
            use_adaptive=False,
        )
        groups_after = [
            (
                group.group_id,
                group.representative.id,
                tuple(m.id for m in group.members),
            )
            for group in list_all_groups(connection)
        ]

    assert [item.job.id for item in flat.flat_ranked] == [
        ids["beta"],
        ids["gamma"],
        ids["alpha-a"],
        ids["alpha-b"],
        ids["unknown-date"],
        ids["neutral"],
    ]
    assert [item.group.representative.id for item in grouped.grouped_ranked] == [
        ids["beta"],
        ids["gamma"],
        ids["alpha-a"],
        ids["unknown-date"],
        ids["neutral"],
    ]
    assert flat.flat_ranked[0].score == flat.flat_ranked[1].score == 4
    assert grouped.grouped_ranked[0].score == grouped.grouped_ranked[1].score == 4
    assert [item.job.id for item in flat_without.flat_ranked][:3] == [
        ids["beta"],
        ids["gamma"],
        ids["neutral"],
    ]
    assert groups_after == groups_before
    alpha = next(group for group in groups_after if ids["alpha-a"] in group[2])
    assert alpha[1:] == (ids["alpha-a"], (ids["alpha-a"], ids["alpha-b"]))


def test_active_learning_does_not_adapt_chronological_saved_search_or_get_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "unchanged-surfaces.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                *(
                    _surface_job(
                        f"learn-{index}",
                        title="Backend Engineer",
                        company=f"Learn {index}",
                        location="Berlin",
                        published_at=f"2026-09-0{index}T10:00:00+00:00",
                    )
                    for index in range(1, 4)
                ),
                _surface_job(
                    "adaptive-candidate",
                    title="Backend Engineer Candidate",
                    company="Adaptive Candidate",
                    location="Berlin",
                    published_at="2026-09-14T10:00:00+00:00",
                ),
                _surface_job(
                    "newer-candidate",
                    title="Frontend Candidate",
                    company="Newer Candidate",
                    location=None,
                    published_at="2026-09-16T10:00:00+00:00",
                    remote_status="onsite_or_unspecified",
                ),
            ],
        )
        ensure_grouped(connection)
        ids = {
            row["source_job_id"]: int(row["id"])
            for row in connection.execute("SELECT id, source_job_id FROM jobs")
        }
        for index in range(1, 4):
            set_decision(
                connection,
                job_id=ids[f"learn-{index}"],
                decision="shortlist",
                now=NOW,
            )
        preferences = build_adaptive_preference_set(connection)
        candidate = next(
            job
            for job in search_jobs(connection, "Candidate")
            if job.id == ids["adaptive-candidate"]
        )
        raw = compute_raw_learned_adjustment(
            AdaptiveCandidate(
                titles=(candidate.title,),
                company=candidate.company,
                location=candidate.location,
                remote_status=candidate.remote_status,
                employment_types=("full time",),
                seniority_levels=(),
            ),
            preferences,
        )
        assert raw.points == 8
        chronological = search_jobs(connection, "Candidate")
        search = create_search(
            connection,
            fields=SavedSearchFields(
                name="chronological",
                query="Candidate",
                mode="search",
            ),
        )
        saved_result = execute_search(connection, search)

    expected = [ids["newer-candidate"], ids["adaptive-candidate"]]
    assert [job.id for job in chronological] == expected
    assert [job.id for job in saved_result.flat] == expected

    assert (
        cli.main(["saved-search", "run", "chronological", "--database", str(database)])
        == 0
    )
    default_output = capsys.readouterr().out
    assert (
        cli.main(
            [
                "saved-search",
                "run",
                "chronological",
                "--no-adaptive",
                "--database",
                str(database),
            ]
        )
        == 0
    )
    opted_out_output = capsys.readouterr().out
    assert default_output == opted_out_output
    assert default_output.index("Frontend Candidate") < default_output.index(
        "Backend Engineer Candidate"
    )
    assert "Learned preferences:" not in default_output

    with TestClient(create_app(database, tmp_path / "materials")) as client:
        jobs = client.get("/jobs?query=Candidate").json()
        report = client.get("/report").json()
    assert [item["id"] for item in jobs["items"]] == expected
    assert report["recommendations"]["ranked_jobs"] == []
    assert report["recommendations"]["ranked_groups"] == []
    assert report["review_queue"]["items"] == []
    assert report["recommendations"]["source"] == "none"
    assert report["review_queue"]["source"] == "none"


def test_ordinary_learned_explanations_are_bounded_without_changing_scoring() -> None:
    normalized_values = (
        "backend",
        "backend platform",
        "backend platform reliability",
        "backend platform reliability distributed",
        "backend platform reliability distributed systems",
        "backend platform reliability distributed systems engineer",
    )
    preferences = AdaptivePreferenceSet(
        learned=tuple(
            LearnedPreference(
                preference_key=f"{index:032d}",
                dimension="title",
                normalized_value=value,
                display_value=f"{value} {'x' * 100}",
                positive_count=3,
                negative_count=0,
                net_evidence=3,
                active=True,
                polarity="positive",
                strength_level=2,
                suppressed=False,
                activation_reason="positive threshold met",
                positive_examples=(),
                negative_examples=(),
            )
            for index, value in enumerate(normalized_values, start=1)
        ),
        locked_dimensions=frozenset(),
    )
    facts = MatchFacts(
        titles=("Backend Platform Reliability Distributed Systems Engineer",),
        company=None,
    )
    one_match = score_facts(
        facts,
        CandidateProfile(),
        adaptive_preferences=AdaptivePreferenceSet(
            learned=preferences.learned[:1], locked_dimensions=frozenset()
        ),
    )
    many_matches = score_facts(
        facts,
        CandidateProfile(),
        adaptive_preferences=preferences,
    )

    assert one_match.score == many_matches.score == 4
    assert many_matches.adaptive is not None
    assert len(many_matches.adaptive.matches) == len(normalized_values)
    values = learned_display_values(many_matches.adaptive.matches)
    assert len(values) == MAX_LEARNED_DISPLAY_VALUES + 1
    assert values[-1] == "... and 3 more"
    assert all(
        len(value) <= MAX_LEARNED_DISPLAY_VALUE_LENGTH
        for value in values[:MAX_LEARNED_DISPLAY_VALUES]
    )
    assert values == learned_display_values(many_matches.adaptive.matches)

    component = next(
        item for item in many_matches.components if item.kind == "learned_preference"
    )
    explanation = explain_result(many_matches)
    factor = next(
        item
        for item in explanation.other_factors
        if item.criterion == "learned_preference"
    )
    display = learned_display_summary(many_matches.adaptive.matches)
    assert component.label == f"Learned preferences: {display} (+4)"
    assert factor.value == display
    assert component.points == factor.points == many_matches.preference_adjustment == 4
    assert "x" * 61 not in component.label


def test_bounded_display_deduplicates_after_compaction_in_match_order() -> None:
    matches = (
        LearnedMatch("a", "title", "First", 1),
        LearnedMatch("b", "company", "Second", 1),
        LearnedMatch("c", "location", "First", 1),
        LearnedMatch("d", "remote", "Third", 1),
        LearnedMatch("e", "seniority", "Fourth", 1),
    )
    assert learned_display_values(matches) == (
        "First",
        "Second",
        "Third",
        "... and 1 more",
    )
