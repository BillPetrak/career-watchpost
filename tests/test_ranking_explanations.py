import json

import pytest

from careeros.ranking.explanations import explain_result, explanation_to_dict
from careeros.ranking.feedback import CandidateJob, FeedbackSet, FeedbackSource
from careeros.ranking.scorer import (
    CandidateProfile,
    MatchFacts,
    MatchResult,
    ScoreComponent,
    score_facts,
)


def test_explanation_uses_structured_evidence_and_reconciles_score() -> None:
    result = score_facts(
        MatchFacts(
            titles=("Senior Python Engineer",),
            description="Python and SQL",
            location="Berlin",
        ),
        CandidateProfile(
            desired_titles=("Engineer", "DistinctiveUnusedTitle"),
            skills=("Python", "DistinctiveUnusedSkill"),
            required_skills=("Rust",),
            preferred_locations=("Berlin", "DistinctiveUnusedLocation"),
        ),
    )
    explanation = explain_result(result)
    assert explanation.score.contribution_total == result.score
    assert explanation.score.match_score == result.match_score
    assert (
        sum(
            f.points
            for section in (
                explanation.matches,
                explanation.gaps,
                explanation.unknowns,
                explanation.other_factors,
            )
            for f in section
        )
        == result.score
    )
    values = [
        f.value
        for section in (
            explanation.matches,
            explanation.gaps,
            explanation.unknowns,
            explanation.other_factors,
        )
        for f in section
    ]
    assert "Engineer" in values
    assert "Python" in values
    assert "DistinctiveUnusedSkill" in values
    assert "DistinctiveUnusedTitle" not in values
    assert "DistinctiveUnusedLocation" not in values


def test_resolution_paths_are_canonical() -> None:
    capped = score_facts(
        MatchFacts(
            titles=("Engineer Berlin Remote Senior",),
            description="Python Rust Go Java SQL TypeScript",
            location="Berlin",
            remote_status="remote",
            employment_type="full-time",
        ),
        CandidateProfile(
            desired_titles=("Engineer", "Senior Engineer"),
            skills=("Python", "Rust", "Go", "Java", "SQL", "TypeScript"),
            preferred_locations=("Berlin",),
            remote_preference="remote",
            preferred_employment_types=("full-time",),
            preferred_seniority=("senior",),
        ),
    )
    assert capped.score > 100
    assert explain_result(capped).score.resolution == "capped"

    floored = score_facts(
        MatchFacts(
            titles=("Junior Engineer",),
            description="contract",
            employment_type="contract",
        ),
        CandidateProfile(
            desired_titles=("Nope",),
            required_skills=("Rust", "Go", "Java", "Kotlin"),
            preferred_employment_types=("full-time",),
            preferred_seniority=("senior",),
            excluded_terms=("contract", "temporary", "agency"),
        ),
    )
    assert floored.score < 0
    assert explain_result(floored).score.resolution == "floored"

    excluded = score_facts(
        MatchFacts(titles=("Engineer",)),
        CandidateProfile(hard_excluded_terms=("Engineer",)),
    )
    assert explain_result(excluded).score.resolution == "excluded"
    assert explain_result(excluded).score.match_score == 0

    unchanged = score_facts(MatchFacts(titles=("Engineer",)), CandidateProfile())
    assert unchanged.score == 0
    assert explain_result(unchanged).score.resolution == "unchanged"


def test_description_unavailable_is_unknown_not_missing_penalty() -> None:
    result = score_facts(
        MatchFacts(titles=("Engineer",), description=None),
        CandidateProfile(required_skills=("Rust",)),
    )
    explanation = explain_result(result)
    assert result.score == 0
    assert explanation.gaps == ()
    assert [factor.value for factor in explanation.unknowns] == [None, "Rust"]


def test_group_explanation_is_publicly_serializable() -> None:
    result = score_facts(MatchFacts(titles=("Engineer",)), CandidateProfile())
    payload = explanation_to_dict(
        explain_result(result, scope="opportunity", listing_count=2)
    )
    assert payload["scope"] == "opportunity"
    assert payload["listing_count"] == 2
    assert json.dumps(payload) == json.dumps(
        explanation_to_dict(
            explain_result(result, scope="opportunity", listing_count=2)
        )
    )
    assert set(payload) == {
        "scope",
        "listing_count",
        "summary",
        "score",
        "matches",
        "gaps",
        "unknowns",
        "other_factors",
    }


def test_listing_count_must_be_positive() -> None:
    result = score_facts(MatchFacts(titles=("Engineer",)), CandidateProfile())
    with pytest.raises(ValueError, match="listing_count"):
        explain_result(result, scope="opportunity", listing_count=0)


def test_unknown_zero_point_component_fails_closed() -> None:
    result = MatchResult(
        score=0,
        matched_titles=(),
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
            ScoreComponent(
                kind="future_veto",
                label="private future diagnostic",
                points=0,
                is_veto=True,
            ),
        ),
    )
    with pytest.raises(RuntimeError, match="unknown score component"):
        explain_result(result)


def test_public_factors_cover_caps_and_neutral_observations() -> None:
    result = score_facts(
        MatchFacts(
            titles=("Engineer", "Senior Engineer"),
            description="Python Rust Go Java SQL TypeScript Kotlin",
            location="Berlin",
            remote_status="remote",
            employment_type="full-time",
        ),
        CandidateProfile(
            desired_titles=("Engineer", "Senior Engineer", "Lead Engineer"),
            skills=("Python", "Rust", "Go", "Java", "SQL", "TypeScript", "Kotlin"),
            required_skills=(
                "Python",
                "Rust",
                "Go",
                "Java",
                "SQL",
                "TypeScript",
                "Kotlin",
            ),
            preferred_locations=("Berlin",),
            remote_preference="remote",
            preferred_employment_types=("full-time",),
            preferred_seniority=("senior",),
        ),
    )
    explanation = explain_result(result)
    assert len([f for f in explanation.matches if f.criterion == "title"]) == 2
    assert len([f for f in explanation.matches if f.criterion == "skill"]) == 6
    assert [f.value for f in explanation.matches if f.criterion == "skill"] == [
        "Python",
        "Rust",
        "Go",
        "Java",
        "SQL",
        "TypeScript",
    ]
    assert (
        sum(
            f.points
            for section in (
                explanation.matches,
                explanation.gaps,
                explanation.unknowns,
                explanation.other_factors,
            )
            for f in section
        )
        == result.score
    )


def test_duplicate_title_and_skill_terms_are_projected_once() -> None:
    result = score_facts(
        MatchFacts(titles=("Python Engineer",), description="Python"),
        CandidateProfile(
            desired_titles=("Engineer", "Engineer"),
            skills=("Python", "Python"),
            required_skills=("Python",),
        ),
    )
    explanation = explain_result(result)
    assert [factor.value for factor in explanation.matches] == ["Engineer", "Python"]


def test_missing_required_cap_and_preferred_zero_point() -> None:
    result = score_facts(
        MatchFacts(titles=("Engineer",), description="Python"),
        CandidateProfile(
            required_skills=("Rust", "Go", "Java", "Kotlin", "Elixir"),
            skills=("Python", "UnlistedPreferred"),
        ),
    )
    explanation = explain_result(result)
    required = [f for f in explanation.gaps if f.criterion == "required_skill"]
    assert len(required) == 5
    assert [f.points for f in required[:3]] == [-4, -4, -4]
    assert [f.points for f in required[3:]] == [0, 0]
    preferred = [f for f in explanation.gaps if f.criterion == "preferred_skill"]
    assert [(f.value, f.points, f.effect) for f in preferred] == [
        ("UnlistedPreferred", 0, "neutral")
    ]


def test_optional_criteria_match_mismatch_and_absence_are_truthful() -> None:
    matched = score_facts(
        MatchFacts(
            titles=("Senior Engineer",),
            location="Berlin",
            remote_status="remote",
            employment_type="full-time",
        ),
        CandidateProfile(
            preferred_locations=("Berlin",),
            remote_preference="remote",
            preferred_employment_types=("full-time",),
            preferred_seniority=("senior",),
        ),
    )
    assert {f.criterion for f in explain_result(matched).matches} == {
        "location",
        "remote",
        "employment",
        "seniority",
    }
    mismatch = score_facts(
        MatchFacts(
            titles=("Junior Engineer",), location="Berlin", employment_type="contract"
        ),
        CandidateProfile(
            preferred_employment_types=("full-time",), preferred_seniority=("senior",)
        ),
    )
    assert {f.criterion for f in explain_result(mismatch).gaps} == {
        "employment",
        "seniority",
    }
    absent = explain_result(
        score_facts(
            MatchFacts(titles=("Engineer",)),
            CandidateProfile(
                preferred_locations=("Berlin",),
                remote_preference="remote",
                preferred_employment_types=("full-time",),
                preferred_seniority=("senior",),
            ),
        )
    )
    assert absent.matches == () and absent.gaps == ()


def test_feedback_is_generic_and_does_not_leak_source() -> None:
    result = score_facts(
        MatchFacts(titles=("Engineer",), location="Berlin"),
        CandidateProfile(),
        feedback=FeedbackSet(
            sources=(
                FeedbackSource(
                    job_id=99,
                    title="Senior Engineer",
                    company="Private Company",
                    location="Berlin",
                    remote_status=None,
                    employment_type=None,
                    decision="shortlist",
                ),
            )
        ),
        candidate=CandidateJob(
            job_ids=frozenset({1}),
            titles=("Senior Engineer",),
            company="Acme",
            location="Berlin",
        ),
    )
    payload = explanation_to_dict(explain_result(result))
    assert payload["other_factors"][0]["criterion"] == "feedback"
    assert payload["other_factors"][0]["points"] == 5
    assert "99" not in str(payload)
    assert "Senior Engineer" not in str(payload)
    assert "Private Company" not in str(payload)


def test_exclusion_factors_are_public_and_arithmetic() -> None:
    result = score_facts(
        MatchFacts(titles=("Engineer contract",), description="contract"),
        CandidateProfile(
            excluded_terms=("contract",), hard_excluded_terms=("Engineer",)
        ),
    )
    explanation = explain_result(result)
    assert [(f.criterion, f.effect, f.points) for f in explanation.other_factors] == [
        ("excluded_term", "negative", -5),
        ("hard_exclusion", "blocking", 0),
    ]
