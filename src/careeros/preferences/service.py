"""Transaction-owning explicit-profile persistence and authority resolution."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from careeros.database import preferences as preference_database
from careeros.preferences.models import (
    EXPLICIT_LIST_DIMENSIONS,
    EXPLICIT_PREFERENCE_DIMENSIONS,
    MAX_EXPLICIT_VALUE_LENGTH,
    MAX_EXPLICIT_VALUES,
    MAX_REMOTE_PREFERENCE_LENGTH,
    EffectivePreferenceContext,
    ExplicitPreferenceProfile,
)
from careeros.ranking.scorer import CandidateProfile


def _serialize(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _validate_profile(profile: ExplicitPreferenceProfile) -> None:
    for dimension in EXPLICIT_LIST_DIMENSIONS:
        values = getattr(profile, dimension)
        if len(values) > MAX_EXPLICIT_VALUES:
            raise ValueError(
                f"{dimension} contains more than {MAX_EXPLICIT_VALUES} values"
            )
        for value in values:
            if not isinstance(value, str):
                raise ValueError(f"{dimension} values must be strings")
            if not value.strip():
                raise ValueError(f"{dimension} values must not be empty")
            if len(value) > MAX_EXPLICIT_VALUE_LENGTH:
                raise ValueError(
                    f"{dimension} values must be at most "
                    f"{MAX_EXPLICIT_VALUE_LENGTH} characters"
                )
    remote = profile.remote_preference
    if remote is not None:
        if not isinstance(remote, str):
            raise ValueError("remote_preference must be a string or null")
        if not remote.strip():
            raise ValueError("remote_preference must not be empty")
        if len(remote) > MAX_REMOTE_PREFERENCE_LENGTH:
            raise ValueError(
                "remote_preference must be at most "
                f"{MAX_REMOTE_PREFERENCE_LENGTH} characters"
            )


def _profile_rows(
    profile: ExplicitPreferenceProfile,
) -> tuple[tuple[str, int, str], ...]:
    rows: list[tuple[str, int, str]] = []
    for dimension in EXPLICIT_PREFERENCE_DIMENSIONS:
        if dimension == "remote_preference":
            if profile.remote_preference is not None:
                rows.append((dimension, 0, profile.remote_preference))
            continue
        rows.extend(
            (dimension, position, value)
            for position, value in enumerate(getattr(profile, dimension))
        )
    return tuple(rows)


def replace_explicit_profile(
    connection: sqlite3.Connection,
    profile: CandidateProfile | ExplicitPreferenceProfile,
    *,
    now: datetime | None = None,
) -> ExplicitPreferenceProfile:
    """Atomically replace stored explicit state, including configured-empty."""
    explicit = (
        profile
        if isinstance(profile, ExplicitPreferenceProfile)
        else ExplicitPreferenceProfile.from_candidate_profile(profile)
    )
    _validate_profile(explicit)
    rows = _profile_rows(explicit)
    timestamp = _serialize(now if now is not None else datetime.now(UTC))

    connection.execute("BEGIN IMMEDIATE")
    try:
        preference_database.delete_explicit_preferences(connection)
        for dimension, position, value in rows:
            preference_database.insert_explicit_preference(
                connection,
                dimension=dimension,
                position=position,
                value=value,
            )
        preference_database.set_explicit_configuration(
            connection,
            configured=True,
            updated_at=timestamp,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return explicit


def clear_explicit_profile(connection: sqlite3.Connection) -> None:
    """Atomically remove stored explicit authority without touching learned state."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        preference_database.delete_explicit_preferences(connection)
        preference_database.set_explicit_configuration(
            connection,
            configured=False,
            updated_at=None,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def load_stored_explicit_profile(
    connection: sqlite3.Connection,
) -> ExplicitPreferenceProfile | None:
    """Load configured stored authority; return None only when it is absent."""
    state = preference_database.get_preference_state(connection)
    rows = preference_database.list_explicit_preferences(connection)
    if not state.explicit_configured:
        if rows or state.explicit_updated_at is not None:
            raise ValueError("stored explicit preference state is inconsistent")
        return None
    if state.explicit_updated_at is None:
        raise ValueError("configured explicit preference state has no update time")

    values: dict[str, list[str]] = {
        dimension: [] for dimension in EXPLICIT_LIST_DIMENSIONS
    }
    remote: str | None = None
    expected_positions = {dimension: 0 for dimension in EXPLICIT_PREFERENCE_DIMENSIONS}
    for row in rows:
        if row.dimension not in expected_positions:
            raise ValueError("stored explicit preference dimension is invalid")
        expected = expected_positions[row.dimension]
        if row.position != expected:
            raise ValueError("stored explicit preference positions are not contiguous")
        expected_positions[row.dimension] += 1
        if row.dimension == "remote_preference":
            if row.position != 0 or remote is not None:
                raise ValueError("stored remote preference must use position zero")
            remote = row.value
        else:
            values[row.dimension].append(row.value)

    profile = ExplicitPreferenceProfile(
        desired_titles=tuple(values["desired_titles"]),
        skills=tuple(values["skills"]),
        required_skills=tuple(values["required_skills"]),
        preferred_locations=tuple(values["preferred_locations"]),
        remote_preference=remote,
        excluded_terms=tuple(values["excluded_terms"]),
        hard_excluded_terms=tuple(values["hard_excluded_terms"]),
        preferred_employment_types=tuple(values["preferred_employment_types"]),
        preferred_seniority=tuple(values["preferred_seniority"]),
    )
    _validate_profile(profile)
    return profile


def resolve_explicit_profile(
    connection: sqlite3.Connection,
    *,
    supplied_profile: CandidateProfile | ExplicitPreferenceProfile | None = None,
) -> EffectivePreferenceContext:
    """Resolve supplied, stored-configured, then empty-default authority."""
    if supplied_profile is not None:
        explicit = (
            supplied_profile
            if isinstance(supplied_profile, ExplicitPreferenceProfile)
            else ExplicitPreferenceProfile.from_candidate_profile(supplied_profile)
        )
        _validate_profile(explicit)
        return EffectivePreferenceContext(explicit_profile=explicit, source="supplied")
    stored = load_stored_explicit_profile(connection)
    if stored is not None:
        return EffectivePreferenceContext(explicit_profile=stored, source="stored")
    return EffectivePreferenceContext(
        explicit_profile=ExplicitPreferenceProfile(), source="default"
    )
