"""Transaction-owning preference inspection and reversal operations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from careeros.database import preferences as preference_database
from careeros.preferences.learning import LearnedPreference, derive_learned_preferences
from careeros.preferences.models import (
    EXPLICIT_LIST_DIMENSIONS,
    EXPLICIT_PREFERENCE_DIMENSIONS,
    MAX_EXPLICIT_VALUE_LENGTH,
    MAX_EXPLICIT_VALUES,
    MAX_REMOTE_PREFERENCE_LENGTH,
    AdaptivePreferenceSet,
    EffectivePreferenceContext,
    ExplicitPreferenceProfile,
    LearnedPreferenceSuppression,
    PreferenceState,
    explicit_ranking_authority_fingerprint,
    learned_ranking_authority_fingerprint,
)
from careeros.ranking.scorer import CandidateProfile


def _ranking_authority_changed(
    connection: sqlite3.Connection,
    *,
    kind: str,
    fingerprint: str,
    previous_fingerprint: str | None = None,
) -> None:
    """Mark materialized ranking state dirty when the optional authority exists.

    The ranking persistence layer is deliberately imported lazily: this keeps
    the frozen preference service usable during schema upgrades and preserves
    the transaction owner here.  The layer receives the open transaction and
    therefore records invalidation atomically with the preference mutation.
    """
    if kind == "explicit" and previous_fingerprint == fingerprint:
        return
    try:
        from careeros.database.review_ranking import (
            get_authority,
            update_explicit_authority,
            update_learned_authority,
        )
    except ImportError:
        return
    if kind == "explicit":
        # The singleton is the CAS authority; use its stored prior identity
        # (including the schema-14 bootstrap identity) for the guard.
        previous_fingerprint = get_authority(connection).explicit_fingerprint
        update_explicit_authority(
            connection,
            fingerprint,
            previous_fingerprint=previous_fingerprint,
            effective_learned_fingerprint=_learned_authority_fingerprint(connection),
        )
        return
    update_learned_authority(
        connection,
        fingerprint,
        previous_fingerprint=previous_fingerprint,
    )


def _learned_authority_fingerprint(connection: sqlite3.Connection) -> str:
    """Reconstruct ranking-effective learned metadata without loading jobs."""
    explicit = load_stored_explicit_profile(connection) or ExplicitPreferenceProfile()
    return learned_ranking_authority_fingerprint(
        _ranking_learned_preferences(
            connection,
            locked_dimensions=explicit.locked_dimensions,
        )
    )


def refresh_learned_authority(
    connection: sqlite3.Connection,
    *,
    previous_fingerprint: str | None = None,
) -> bool:
    """Refresh learned ranking authority in the caller's transaction."""
    from careeros.database.review_ranking import update_learned_authority

    return update_learned_authority(
        connection,
        _learned_authority_fingerprint(connection),
        previous_fingerprint=previous_fingerprint,
    )


def effective_learned_authority_fingerprint(connection: sqlite3.Connection) -> str:
    """Return the scorer-effective learned identity for the current DB state."""
    return _learned_authority_fingerprint(connection)


def _ranking_learned_preferences(
    connection: sqlite3.Connection,
    *,
    locked_dimensions: frozenset[str],
) -> tuple[LearnedPreference, ...]:
    """Load only scorer-effective learned authority without a corpus scan."""
    state = preference_database.get_preference_state(connection)
    rows = connection.execute(
        """SELECT e.dimension, e.normalized_value, e.value, e.signal
           FROM preference_evidence AS e
           LEFT JOIN job_group_members AS gm ON gm.job_id = e.job_id
           WHERE e.review_event_id > ?
             AND EXISTS (
               SELECT 1 FROM review_decisions AS d
               WHERE d.job_id = e.job_id
             )
             AND NOT EXISTS (
               SELECT 1
               FROM job_group_members AS lower_gm
               JOIN review_decisions AS lower_d ON lower_d.job_id = lower_gm.job_id
               WHERE gm.group_id IS NOT NULL
                 AND lower_gm.group_id = gm.group_id
                 AND lower_d.job_id < e.job_id
             )
           ORDER BY e.job_id, e.dimension COLLATE BINARY,
                    e.normalized_value COLLATE BINARY""",
        (state.learning_reset_review_event_id,),
    ).fetchall()
    suppressed = {
        (row.dimension, row.normalized_value)
        for row in preference_database.list_suppressions(connection)
    }
    aggregates: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for row in rows:
        key = (str(row["dimension"]), str(row["normalized_value"]))
        aggregates.setdefault(key, []).append((str(row["value"]), int(row["signal"])))
    effective: list[LearnedPreference] = []
    for (dimension, normalized), values in sorted(aggregates.items()):
        positive = sum(signal == 1 for _value, signal in values)
        negative = sum(signal == -1 for _value, signal in values)
        net = positive - negative
        polarity = (
            "positive"
            if positive >= 2 and net >= 2
            else ("negative" if negative >= 3 and net <= -2 else None)
        )
        if (
            polarity is None
            or (dimension, normalized) in suppressed
            or dimension in locked_dimensions
        ):
            continue
        strength = min(2, abs(net) - 1)
        display = values[0][0]
        from careeros.preferences.models import preference_key

        effective.append(
            LearnedPreference(
                preference_key=preference_key(dimension, normalized),
                dimension=dimension,
                normalized_value=normalized,
                display_value=display,
                positive_count=positive,
                negative_count=negative,
                net_evidence=net,
                active=True,
                polarity=polarity,
                strength_level=strength,
                suppressed=False,
                activation_reason=(
                    "positive threshold met"
                    if polarity == "positive"
                    else "negative threshold met"
                ),
                positive_examples=(),
                negative_examples=(),
            )
        )
    return tuple(effective)


@dataclass(frozen=True)
class ExplicitPreferenceInspection:
    """Authoritative stored explicit state, including configured-empty."""

    configured: bool
    updated_at: str | None
    profile: ExplicitPreferenceProfile
    locked_dimensions: frozenset[str]


@dataclass(frozen=True)
class PreferenceInspection:
    """One immutable snapshot of persistent explicit and learned authority."""

    explicit: ExplicitPreferenceInspection
    learned: tuple[LearnedPreference, ...]
    state: PreferenceState


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
    rows = _profile_rows(explicit)
    timestamp = _serialize(now if now is not None else datetime.now(UTC))

    connection.execute("BEGIN IMMEDIATE")
    try:
        _validate_profile(explicit)
        previous = load_stored_explicit_profile(connection)
        previous = previous or ExplicitPreferenceProfile()
        previous_fingerprint = explicit_ranking_authority_fingerprint(previous)
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
        # Refresh after the requested state is written, still before commit.
        _ranking_authority_changed(
            connection,
            kind="explicit",
            fingerprint=explicit_ranking_authority_fingerprint(explicit),
            previous_fingerprint=previous_fingerprint,
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
        previous = load_stored_explicit_profile(connection)
        previous = previous or ExplicitPreferenceProfile()
        previous_fingerprint = explicit_ranking_authority_fingerprint(previous)
        preference_database.delete_explicit_preferences(connection)
        preference_database.set_explicit_configuration(
            connection,
            configured=False,
            updated_at=None,
        )
        _ranking_authority_changed(
            connection,
            kind="explicit",
            fingerprint=explicit_ranking_authority_fingerprint(
                ExplicitPreferenceProfile()
            ),
            previous_fingerprint=previous_fingerprint,
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


def build_adaptive_preference_set(
    connection: sqlite3.Connection,
    *,
    supplied_profile: CandidateProfile | ExplicitPreferenceProfile | None = None,
    enabled: bool = True,
) -> AdaptivePreferenceSet:
    """Build one immutable learned snapshot with invocation-specific locks."""
    if not enabled:
        return AdaptivePreferenceSet.disabled()
    context = resolve_explicit_profile(
        connection,
        supplied_profile=supplied_profile,
    )
    return AdaptivePreferenceSet(
        learned=derive_learned_preferences(connection),
        locked_dimensions=context.locked_dimensions,
    )


def build_ranking_adaptive_preference_set(
    connection: sqlite3.Connection,
    *,
    supplied_profile: CandidateProfile | ExplicitPreferenceProfile | None = None,
    enabled: bool = True,
) -> AdaptivePreferenceSet:
    """Build scorer authority from evidence/decisions without loading jobs."""
    if not enabled:
        return AdaptivePreferenceSet.disabled()
    context = resolve_explicit_profile(
        connection,
        supplied_profile=supplied_profile,
    )
    return AdaptivePreferenceSet(
        learned=_ranking_learned_preferences(
            connection,
            locked_dimensions=context.locked_dimensions,
        ),
        locked_dimensions=context.locked_dimensions,
    )


def inspect_preferences(connection: sqlite3.Connection) -> PreferenceInspection:
    """Assemble the allow-list-ready persistent preference authority snapshot."""
    state = preference_database.get_preference_state(connection)
    stored = load_stored_explicit_profile(connection)
    profile = stored if stored is not None else ExplicitPreferenceProfile()
    return PreferenceInspection(
        explicit=ExplicitPreferenceInspection(
            configured=state.explicit_configured,
            updated_at=state.explicit_updated_at,
            profile=profile,
            locked_dimensions=profile.locked_dimensions,
        ),
        learned=derive_learned_preferences(connection),
        state=state,
    )


def _learned_preference_for_key(
    connection: sqlite3.Connection, preference_key: str
) -> LearnedPreference:
    if len(preference_key) != 32 or any(
        character not in "0123456789abcdef" for character in preference_key
    ):
        raise ValueError("invalid learned preference key")
    learned = next(
        (
            preference
            for preference in derive_learned_preferences(connection)
            if preference.preference_key == preference_key
        ),
        None,
    )
    if learned is None:
        raise ValueError("unknown learned preference key")
    return learned


def suppress_learned_preference(
    connection: sqlite3.Connection,
    preference_key: str,
    *,
    now: datetime | None = None,
) -> LearnedPreference:
    """Suppress one currently derived preference while preserving its evidence."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        previous_fingerprint = _learned_authority_fingerprint(connection)
        learned = _learned_preference_for_key(connection, preference_key)
        if not learned.suppressed:
            preference_database.upsert_suppression(
                connection,
                LearnedPreferenceSuppression(
                    preference_key=learned.preference_key,
                    dimension=learned.dimension,
                    normalized_value=learned.normalized_value,
                    suppressed_at=_serialize(
                        now if now is not None else datetime.now(UTC)
                    ),
                ),
            )
        _ranking_authority_changed(
            connection,
            kind="learned",
            fingerprint=_learned_authority_fingerprint(connection),
            previous_fingerprint=previous_fingerprint,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return _learned_preference_for_key(connection, preference_key)


def restore_learned_preference(
    connection: sqlite3.Connection, preference_key: str
) -> LearnedPreference:
    """Restore one currently derived preference; an unsuppressed key is a no-op."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        previous_fingerprint = _learned_authority_fingerprint(connection)
        _learned_preference_for_key(connection, preference_key)
        preference_database.delete_suppression(connection, preference_key)
        _ranking_authority_changed(
            connection,
            kind="learned",
            fingerprint=_learned_authority_fingerprint(connection),
            previous_fingerprint=previous_fingerprint,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return _learned_preference_for_key(connection, preference_key)


def reset_learned_preferences(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> PreferenceState:
    """Atomically clear learned state and advance the review-event boundary."""
    reset_at = _serialize(now if now is not None else datetime.now(UTC))
    connection.execute("BEGIN IMMEDIATE")
    try:
        previous_fingerprint = _learned_authority_fingerprint(connection)
        boundary = preference_database.maximum_review_event_id(connection)
        preference_database.delete_all_preference_evidence(connection)
        preference_database.delete_all_suppressions(connection)
        preference_database.set_learning_reset_boundary(
            connection,
            reset_at=reset_at,
            review_event_id=boundary,
        )
        _ranking_authority_changed(
            connection,
            kind="learned",
            fingerprint=_learned_authority_fingerprint(connection),
            previous_fingerprint=previous_fingerprint,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return preference_database.get_preference_state(connection)
