"""Deterministic learned-preference matching and bounded score adjustment."""

from __future__ import annotations

from dataclasses import dataclass

from careeros.grouping.normalize import (
    normalize_company,
    normalize_location,
    normalize_title,
)
from careeros.preferences.models import AdaptivePreferenceSet

_DIMENSION_WEIGHTS = {
    "title": 2,
    "company": 1,
    "location": 1,
    "remote": 1,
    "employment_type": 1,
    "seniority": 1,
}
_DIMENSION_CAPS = {
    "title": 4,
    "company": 2,
    "location": 2,
    "remote": 2,
    "employment_type": 2,
    "seniority": 2,
}

# Ordinary ranking explanations are intentionally compact. Dedicated preference
# inspection retains every learned value and its bounded provenance.
MAX_LEARNED_DISPLAY_VALUES = 3
MAX_LEARNED_DISPLAY_VALUE_LENGTH = 60


@dataclass(frozen=True)
class AdaptiveCandidate:
    """Normalized-source facts needed by the frozen learned matching rules."""

    titles: tuple[str, ...]
    company: str | None
    location: str | None
    remote_status: str | None
    employment_types: tuple[str, ...]
    seniority_levels: tuple[str, ...]


@dataclass(frozen=True)
class LearnedMatch:
    """One active preference that matched a candidate before score caps."""

    preference_key: str
    dimension: str
    display_value: str
    points: int


@dataclass(frozen=True)
class RawLearnedAdjustment:
    """The shared surface-independent learned result, globally bounded to ±8."""

    points: int
    matches: tuple[LearnedMatch, ...]


@dataclass(frozen=True)
class AdaptiveAdjustment:
    """A learned result after the surface's legacy-feedback headroom is applied."""

    raw_points: int
    effective_points: int
    matches: tuple[LearnedMatch, ...]

    @property
    def summary(self) -> str:
        return (
            f"Learned preferences: {learned_display_summary(self.matches)} "
            f"({self.effective_points:+d})"
        )


def _compact_display_value(value: str) -> str:
    if len(value) <= MAX_LEARNED_DISPLAY_VALUE_LENGTH:
        return value
    return value[: MAX_LEARNED_DISPLAY_VALUE_LENGTH - 3].rstrip() + "..."


def learned_display_values(matches: tuple[LearnedMatch, ...]) -> tuple[str, ...]:
    """Select a compact, deterministic projection of matched learned values."""
    unique: list[str] = []
    seen: set[str] = set()
    for match in matches:
        value = _compact_display_value(match.display_value)
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)

    selected = unique[:MAX_LEARNED_DISPLAY_VALUES]
    omitted = len(unique) - len(selected)
    if omitted:
        selected.append(f"... and {omitted} more")
    return tuple(selected)


def learned_display_summary(matches: tuple[LearnedMatch, ...]) -> str:
    """Return the shared ordinary-ranking display value for learned matches."""
    return ", ".join(learned_display_values(matches))


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def _token_prefix(left: str, right: str) -> bool:
    left_tokens = left.split()
    right_tokens = right.split()
    if not left_tokens or not right_tokens:
        return False
    short, long = (
        (left_tokens, right_tokens)
        if len(left_tokens) <= len(right_tokens)
        else (right_tokens, left_tokens)
    )
    return long[: len(short)] == short


def _matches(
    dimension: str,
    normalized_value: str,
    candidate: AdaptiveCandidate,
) -> bool:
    if dimension == "title":
        return any(
            normalized_value == title or _token_prefix(normalized_value, title)
            for title in (normalize_title(value) for value in candidate.titles)
            if title
        )
    if dimension == "company":
        return normalize_company(candidate.company) == normalized_value
    if dimension == "location":
        location = normalize_location(candidate.location)
        return bool(
            location
            and (
                location == normalized_value
                or _token_prefix(location, normalized_value)
            )
        )
    if dimension == "remote":
        return candidate.remote_status == normalized_value
    if dimension == "employment_type":
        return normalized_value in candidate.employment_types
    if dimension == "seniority":
        return normalized_value in candidate.seniority_levels
    raise ValueError(f"unknown learned preference dimension: {dimension!r}")


def compute_raw_learned_adjustment(
    candidate: AdaptiveCandidate,
    preferences: AdaptivePreferenceSet,
) -> RawLearnedAdjustment:
    """Apply the frozen match rules, dimension caps, and global ±8 cap."""
    matches: list[LearnedMatch] = []
    by_dimension: dict[str, int] = {}
    for preference in preferences.learned:
        if (
            not preference.active
            or preference.suppressed
            or preference.polarity is None
            or preference.dimension in preferences.locked_dimensions
            or not _matches(
                preference.dimension,
                preference.normalized_value,
                candidate,
            )
        ):
            continue
        sign = 1 if preference.polarity == "positive" else -1
        points = (
            sign * _DIMENSION_WEIGHTS[preference.dimension] * preference.strength_level
        )
        matches.append(
            LearnedMatch(
                preference_key=preference.preference_key,
                dimension=preference.dimension,
                display_value=preference.display_value,
                points=points,
            )
        )
        by_dimension[preference.dimension] = (
            by_dimension.get(preference.dimension, 0) + points
        )

    total = sum(
        _clamp(points, -_DIMENSION_CAPS[dimension], _DIMENSION_CAPS[dimension])
        for dimension, points in by_dimension.items()
    )
    return RawLearnedAdjustment(
        points=_clamp(total, -8, 8),
        matches=tuple(matches),
    )


def apply_legacy_envelope(
    raw: RawLearnedAdjustment,
    *,
    legacy_feedback: int,
) -> AdaptiveAdjustment | None:
    """Apply the shared combined ±10 envelope and omit zero-effective output."""
    effective = _clamp(legacy_feedback + raw.points, -10, 10) - legacy_feedback
    if effective == 0:
        return None
    return AdaptiveAdjustment(
        raw_points=raw.points,
        effective_points=effective,
        matches=raw.matches,
    )


__all__ = [
    "AdaptiveAdjustment",
    "AdaptiveCandidate",
    "LearnedMatch",
    "MAX_LEARNED_DISPLAY_VALUE_LENGTH",
    "MAX_LEARNED_DISPLAY_VALUES",
    "RawLearnedAdjustment",
    "apply_legacy_envelope",
    "compute_raw_learned_adjustment",
    "learned_display_summary",
    "learned_display_values",
]
