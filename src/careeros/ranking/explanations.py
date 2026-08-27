"""Stable public projections of deterministic ranking evidence.

This module deliberately sits between the scorer and presentation layers.  It
uses typed ``MatchResult`` fields and numeric components, never diagnostic
labels or ``reasons`` strings, so the HTTP contract does not expose scorer
internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from careeros.ranking.scorer import MatchResult

Effect = Literal["positive", "negative", "neutral", "blocking", "unknown"]
Resolution = Literal["unchanged", "capped", "floored", "excluded"]
Criterion = Literal[
    "title",
    "skill",
    "location",
    "remote",
    "employment",
    "seniority",
    "required_skill",
    "preferred_skill",
    "description",
    "excluded_term",
    "hard_exclusion",
    "feedback",
]

_SUPPORTED_COMPONENT_KINDS = frozenset(
    {
        "title",
        "skill",
        "missing_required",
        "location",
        "remote",
        "employment",
        "seniority",
        "excluded",
        "hard_excluded",
        "feedback",
    }
)


@dataclass(frozen=True)
class ExplanationFactor:
    criterion: Criterion
    label: str
    value: str | None
    effect: Effect
    points: int


@dataclass(frozen=True)
class ExplanationScore:
    contribution_total: int
    match_score: int
    resolution: Resolution


@dataclass(frozen=True)
class RankingExplanation:
    scope: Literal["job", "opportunity"]
    listing_count: int
    summary: str
    score: ExplanationScore
    matches: tuple[ExplanationFactor, ...]
    gaps: tuple[ExplanationFactor, ...]
    unknowns: tuple[ExplanationFactor, ...]
    other_factors: tuple[ExplanationFactor, ...]


def _factor(
    criterion: Criterion,
    label: str,
    value: str | None,
    effect: Effect,
    points: int,
) -> ExplanationFactor:
    return ExplanationFactor(criterion, label, value, effect, points)


def explain_result(
    result: MatchResult,
    *,
    scope: Literal["job", "opportunity"] = "job",
    listing_count: int = 1,
) -> RankingExplanation:
    """Project one canonical score result without performing scoring."""
    if listing_count < 1:
        raise ValueError("listing_count must be at least 1")
    component_points: dict[str, list[int]] = {}
    for component in result.components:
        if component.kind not in _SUPPORTED_COMPONENT_KINDS:
            raise RuntimeError(
                "public ranking explanation cannot project unknown score component"
            )
        component_points.setdefault(component.kind, []).append(component.points)
    title_points = component_points.get("title", ())
    skill_points = component_points.get("skill", ())
    missing_points = component_points.get("missing_required", ())
    location_points = component_points.get("location", ())
    remote_points = component_points.get("remote", ())
    employment_points = component_points.get("employment", ())
    seniority_points = component_points.get("seniority", ())
    excluded_points = component_points.get("excluded", ())

    matches: list[ExplanationFactor] = [
        *(
            _factor("title", "Title match", value, "positive", points)
            for value, points in zip(result.matched_titles, title_points, strict=True)
        ),
        *(
            _factor("skill", "Skill mentioned", value, "positive", points)
            for value, points in zip(result.matched_skills, skill_points, strict=True)
        ),
    ]
    if result.location_matched:
        matches.append(
            _factor(
                "location",
                "Preferred location",
                result.location_matched,
                "positive",
                location_points[0] if location_points else 0,
            )
        )
    if result.remote_matched:
        matches.append(
            _factor(
                "remote",
                "Work arrangement preference matched",
                None,
                "positive",
                remote_points[0] if remote_points else 0,
            )
        )
    if result.employment_matched:
        matches.append(
            _factor(
                "employment",
                "Employment type",
                result.employment_matched,
                "positive",
                employment_points[0] if employment_points else 0,
            )
        )
    if result.seniority_matched:
        matches.append(
            _factor(
                "seniority",
                "Seniority",
                result.seniority_matched,
                "positive",
                seniority_points[0] if seniority_points else 0,
            )
        )

    gaps: list[ExplanationFactor] = [
        *(
            _factor(
                "required_skill",
                "Required skill not found",
                value,
                "negative",
                missing_points[index] if index < len(missing_points) else 0,
            )
            for index, value in enumerate(result.missing_required_skills)
            if not result.description_unavailable
        ),
        *(
            _factor(
                "preferred_skill", "Preferred skill not mentioned", value, "neutral", 0
            )
            for value in result.preferred_skills_not_observed
        ),
    ]
    if result.employment_mismatch:
        gaps.append(
            _factor(
                "employment",
                "Employment type mismatch",
                None,
                "negative",
                employment_points[0] if employment_points else 0,
            )
        )
    if result.seniority_mismatch:
        gaps.append(
            _factor(
                "seniority",
                "Seniority mismatch",
                None,
                "negative",
                seniority_points[0] if seniority_points else 0,
            )
        )

    unknowns: list[ExplanationFactor] = []
    if result.description_unavailable:
        unknowns.append(
            _factor("description", "Description unavailable", None, "unknown", 0)
        )
        unknowns.extend(
            _factor(
                "required_skill",
                "Required skill could not be verified",
                value,
                "unknown",
                0,
            )
            for value in result.missing_required_skills
        )

    other: list[ExplanationFactor] = [
        *(
            _factor("excluded_term", "Soft exclusion", value, "negative", points)
            for value, points in zip(
                result.excluded_terms, excluded_points, strict=True
            )
        ),
        *(
            _factor("hard_exclusion", "Hard profile exclusion", value, "blocking", 0)
            for value in result.hard_excluded_terms
        ),
    ]
    if result.feedback is not None:
        points = result.feedback.points
        other.append(
            _factor(
                "feedback",
                "Review feedback adjustment",
                None,
                "positive" if points > 0 else "negative",
                points,
            )
        )

    if result.hard_excluded_terms:
        resolution: Resolution = "excluded"
    elif result.score > 100:
        resolution = "capped"
    elif result.score < 0:
        resolution = "floored"
    else:
        resolution = "unchanged"
    score = ExplanationScore(result.score, result.match_score, resolution)
    positive = sum(1 for item in matches if item.effect == "positive")
    gaps_count = len(gaps)
    unknown_count = len(unknowns)
    summary = (
        f"{positive} match{'es' if positive != 1 else ''}, "
        f"{gaps_count} gap{'s' if gaps_count != 1 else ''}, and "
        f"{unknown_count} unknown{'s' if unknown_count != 1 else ''}."
    )
    explanation = RankingExplanation(
        scope,
        listing_count,
        summary,
        score,
        tuple(matches),
        tuple(gaps),
        tuple(unknowns),
        tuple(other),
    )
    public_points = sum(
        factor.points
        for section in (
            explanation.matches,
            explanation.gaps,
            explanation.unknowns,
            explanation.other_factors,
        )
        for factor in section
    )
    if public_points != result.score:
        raise RuntimeError(
            "public ranking explanation does not reconcile to canonical score"
        )
    return explanation


def explanation_to_dict(explanation: RankingExplanation) -> dict[str, Any]:
    """Serialize the public explanation with stable, JSON-friendly values."""

    def factor_dict(factor: ExplanationFactor) -> dict[str, Any]:
        return {
            "criterion": factor.criterion,
            "label": factor.label,
            "value": factor.value,
            "effect": factor.effect,
            "points": factor.points,
        }

    def factors(items: tuple[ExplanationFactor, ...]) -> list[dict[str, Any]]:
        return [factor_dict(item) for item in items]

    return {
        "scope": explanation.scope,
        "listing_count": explanation.listing_count,
        "summary": explanation.summary,
        "score": {
            "contribution_total": explanation.score.contribution_total,
            "match_score": explanation.score.match_score,
            "resolution": explanation.score.resolution,
        },
        "matches": factors(explanation.matches),
        "gaps": factors(explanation.gaps),
        "unknowns": factors(explanation.unknowns),
        "other_factors": factors(explanation.other_factors),
    }


__all__ = [
    "ExplanationFactor",
    "ExplanationScore",
    "Criterion",
    "RankingExplanation",
    "explain_result",
    "explanation_to_dict",
]
