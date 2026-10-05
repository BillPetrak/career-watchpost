"""Immutable, presentation-agnostic preference domain models and constants."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from careeros.preferences.learning import LearnedPreference
    from careeros.ranking.scorer import CandidateProfile

EXPLICIT_PREFERENCE_DIMENSIONS: tuple[str, ...] = (
    "desired_titles",
    "skills",
    "required_skills",
    "preferred_locations",
    "remote_preference",
    "excluded_terms",
    "hard_excluded_terms",
    "preferred_employment_types",
    "preferred_seniority",
)

EXPLICIT_LIST_DIMENSIONS: tuple[str, ...] = tuple(
    dimension
    for dimension in EXPLICIT_PREFERENCE_DIMENSIONS
    if dimension != "remote_preference"
)

LEARNED_PREFERENCE_DIMENSIONS: tuple[str, ...] = (
    "title",
    "company",
    "location",
    "remote",
    "employment_type",
    "seniority",
)

MAX_EXPLICIT_VALUES = 100
MAX_EXPLICIT_VALUE_LENGTH = 100
MAX_REMOTE_PREFERENCE_LENGTH = 50

EXPLICIT_RANKING_AUTHORITY_VERSION = "careeros-explicit-ranking-authority-v1"
LEARNED_RANKING_AUTHORITY_VERSION = "careeros-learned-ranking-authority-v1"

ExplicitAuthoritySource = Literal["supplied", "stored", "default"]


@dataclass(frozen=True)
class ExplicitPreferenceProfile:
    """All nine explicit profile fields, preserving tuple order and duplicates."""

    desired_titles: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    preferred_locations: tuple[str, ...] = ()
    remote_preference: str | None = None
    excluded_terms: tuple[str, ...] = ()
    hard_excluded_terms: tuple[str, ...] = ()
    preferred_employment_types: tuple[str, ...] = ()
    preferred_seniority: tuple[str, ...] = ()

    @classmethod
    def from_candidate_profile(
        cls, profile: CandidateProfile
    ) -> ExplicitPreferenceProfile:
        """Copy the current scorer profile without normalization or deduplication."""
        return cls(
            desired_titles=profile.desired_titles,
            skills=profile.skills,
            required_skills=profile.required_skills,
            preferred_locations=profile.preferred_locations,
            remote_preference=profile.remote_preference,
            excluded_terms=profile.excluded_terms,
            hard_excluded_terms=profile.hard_excluded_terms,
            preferred_employment_types=profile.preferred_employment_types,
            preferred_seniority=profile.preferred_seniority,
        )

    @property
    def locked_dimensions(self) -> frozenset[str]:
        """Return only learned dimensions populated by explicit authority."""
        locks: set[str] = set()
        if self.desired_titles:
            locks.add("title")
        if self.preferred_locations:
            locks.add("location")
        if self.remote_preference:
            locks.add("remote")
        if self.preferred_employment_types:
            locks.add("employment_type")
        if self.preferred_seniority:
            locks.add("seniority")
        return frozenset(locks)

    def to_candidate_profile(self) -> CandidateProfile:
        """Copy this authority into the existing deterministic scorer profile."""
        from careeros.ranking.scorer import CandidateProfile

        return CandidateProfile(
            desired_titles=self.desired_titles,
            skills=self.skills,
            preferred_locations=self.preferred_locations,
            remote_preference=self.remote_preference,
            excluded_terms=self.excluded_terms,
            required_skills=self.required_skills,
            hard_excluded_terms=self.hard_excluded_terms,
            preferred_employment_types=self.preferred_employment_types,
            preferred_seniority=self.preferred_seniority,
        )


@dataclass(frozen=True)
class PreferenceState:
    """The singleton persistent preference-authority metadata row."""

    explicit_configured: bool
    explicit_updated_at: str | None
    learning_reset_at: str | None
    learning_reset_review_event_id: int
    created_at: str
    explicit_ranking_revision: int = 0
    explicit_ranking_fingerprint: str = ""


@dataclass(frozen=True)
class ExplicitPreferenceValue:
    """One ordered explicit-profile value row."""

    dimension: str
    position: int
    value: str


@dataclass(frozen=True)
class PreferenceEvidence:
    """One current structured evidence snapshot row."""

    job_id: int
    review_event_id: int
    dimension: str
    value: str
    normalized_value: str
    signal: int
    observed_at: str


@dataclass(frozen=True)
class LearnedPreferenceSuppression:
    """One persistent suppression control for a derived learned preference."""

    preference_key: str
    dimension: str
    normalized_value: str
    suppressed_at: str


@dataclass(frozen=True)
class EffectivePreferenceContext:
    """Resolved explicit authority for one invocation; learned state comes later."""

    explicit_profile: ExplicitPreferenceProfile
    source: ExplicitAuthoritySource

    @property
    def locked_dimensions(self) -> frozenset[str]:
        return self.explicit_profile.locked_dimensions


@dataclass(frozen=True)
class AdaptivePreferenceSet:
    """One immutable learned-preference authority snapshot for a ranking run."""

    learned: tuple[LearnedPreference, ...]
    locked_dimensions: frozenset[str]

    @classmethod
    def disabled(cls) -> AdaptivePreferenceSet:
        """Return an explicitly empty adaptive authority for opt-out paths."""
        return cls(learned=(), locked_dimensions=frozenset())


def preference_key(dimension: str, normalized_value: str) -> str:
    """Return the stable 32-character SHA-256 key for one learned preference."""
    if dimension not in LEARNED_PREFERENCE_DIMENSIONS:
        raise ValueError(f"unknown learned preference dimension: {dimension!r}")
    if not normalized_value:
        raise ValueError("normalized preference value must not be empty")
    identity = (dimension, normalized_value)
    blob = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()[:32]


def explicit_ranking_authority_fingerprint(
    profile: ExplicitPreferenceProfile,
) -> str:
    """Fingerprint the exact nine effective scorer fields, preserving order."""
    values = tuple(
        getattr(profile, dimension) for dimension in EXPLICIT_PREFERENCE_DIMENSIONS
    )
    blob = json.dumps(
        (EXPLICIT_RANKING_AUTHORITY_VERSION, values),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def learned_ranking_authority_fingerprint(
    learned: tuple[LearnedPreference, ...],
) -> str:
    """Fingerprint only active, unsuppressed, polarity-bearing preferences."""
    effective = sorted(
        (
            item.preference_key,
            item.dimension,
            item.normalized_value,
            item.display_value,
            item.polarity,
            item.strength_level,
        )
        for item in learned
        if item.active and not item.suppressed and item.polarity is not None
    )
    blob = json.dumps(
        (LEARNED_RANKING_AUTHORITY_VERSION, effective),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
