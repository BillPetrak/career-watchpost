"""Row-level persistence helpers for the frozen schema-v12 preference tables.

These helpers never commit or roll back. Service callers own transaction
boundaries so explicit replacement and later review/evidence coordination can
remain atomic.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from careeros.preferences.models import (
    ExplicitPreferenceValue,
    LearnedPreferenceSuppression,
    PreferenceEvidence,
    PreferenceState,
)


def get_preference_state(connection: sqlite3.Connection) -> PreferenceState:
    row = connection.execute(
        """
        SELECT explicit_configured, explicit_updated_at, learning_reset_at,
               learning_reset_review_event_id, created_at
        FROM preference_state
        WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        raise ValueError("preference state singleton is missing")
    return PreferenceState(
        explicit_configured=bool(row["explicit_configured"]),
        explicit_updated_at=row["explicit_updated_at"],
        learning_reset_at=row["learning_reset_at"],
        learning_reset_review_event_id=int(row["learning_reset_review_event_id"]),
        created_at=str(row["created_at"]),
    )


def list_explicit_preferences(
    connection: sqlite3.Connection,
) -> tuple[ExplicitPreferenceValue, ...]:
    rows = connection.execute(
        """
        SELECT dimension, position, value
        FROM explicit_preferences
        ORDER BY CASE dimension
            WHEN 'desired_titles' THEN 0
            WHEN 'skills' THEN 1
            WHEN 'required_skills' THEN 2
            WHEN 'preferred_locations' THEN 3
            WHEN 'remote_preference' THEN 4
            WHEN 'excluded_terms' THEN 5
            WHEN 'hard_excluded_terms' THEN 6
            WHEN 'preferred_employment_types' THEN 7
            WHEN 'preferred_seniority' THEN 8
        END, position
        """
    ).fetchall()
    return tuple(
        ExplicitPreferenceValue(
            dimension=str(row["dimension"]),
            position=int(row["position"]),
            value=str(row["value"]),
        )
        for row in rows
    )


def delete_explicit_preferences(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM explicit_preferences")


def insert_explicit_preference(
    connection: sqlite3.Connection,
    *,
    dimension: str,
    position: int,
    value: str,
) -> None:
    connection.execute(
        """
        INSERT INTO explicit_preferences(dimension, position, value)
        VALUES (?, ?, ?)
        """,
        (dimension, position, value),
    )


def set_explicit_configuration(
    connection: sqlite3.Connection,
    *,
    configured: bool,
    updated_at: str | None,
) -> None:
    cursor = connection.execute(
        """
        UPDATE preference_state
        SET explicit_configured = ?, explicit_updated_at = ?
        WHERE singleton = 1
        """,
        (int(configured), updated_at),
    )
    if cursor.rowcount != 1:
        raise ValueError("preference state singleton is missing")


def list_preference_evidence(
    connection: sqlite3.Connection,
) -> tuple[PreferenceEvidence, ...]:
    rows = connection.execute(
        """
        SELECT job_id, review_event_id, dimension, value, normalized_value,
               signal, observed_at
        FROM preference_evidence
        ORDER BY job_id, dimension COLLATE BINARY, normalized_value COLLATE BINARY
        """
    ).fetchall()
    return tuple(
        PreferenceEvidence(
            job_id=int(row["job_id"]),
            review_event_id=int(row["review_event_id"]),
            dimension=str(row["dimension"]),
            value=str(row["value"]),
            normalized_value=str(row["normalized_value"]),
            signal=int(row["signal"]),
            observed_at=str(row["observed_at"]),
        )
        for row in rows
    )


def replace_job_evidence(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    evidence: Iterable[PreferenceEvidence],
) -> None:
    """Replace one job's already-derived rows without owning a transaction."""
    rows = tuple(evidence)
    if any(row.job_id != job_id for row in rows):
        raise ValueError("preference evidence job does not match replacement owner")
    connection.execute("DELETE FROM preference_evidence WHERE job_id = ?", (job_id,))
    connection.executemany(
        """
        INSERT INTO preference_evidence(
            job_id, review_event_id, dimension, value, normalized_value,
            signal, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                row.job_id,
                row.review_event_id,
                row.dimension,
                row.value,
                row.normalized_value,
                row.signal,
                row.observed_at,
            )
            for row in rows
        ),
    )


def list_suppressions(
    connection: sqlite3.Connection,
) -> tuple[LearnedPreferenceSuppression, ...]:
    rows = connection.execute(
        """
        SELECT preference_key, dimension, normalized_value, suppressed_at
        FROM learned_preference_suppressions
        ORDER BY dimension COLLATE BINARY, normalized_value COLLATE BINARY
        """
    ).fetchall()
    return tuple(
        LearnedPreferenceSuppression(
            preference_key=str(row["preference_key"]),
            dimension=str(row["dimension"]),
            normalized_value=str(row["normalized_value"]),
            suppressed_at=str(row["suppressed_at"]),
        )
        for row in rows
    )


def upsert_suppression(
    connection: sqlite3.Connection,
    suppression: LearnedPreferenceSuppression,
) -> None:
    connection.execute(
        """
        INSERT INTO learned_preference_suppressions(
            preference_key, dimension, normalized_value, suppressed_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(dimension, normalized_value) DO UPDATE SET
            preference_key = excluded.preference_key,
            suppressed_at = excluded.suppressed_at
        """,
        (
            suppression.preference_key,
            suppression.dimension,
            suppression.normalized_value,
            suppression.suppressed_at,
        ),
    )


def delete_suppression(connection: sqlite3.Connection, preference_key: str) -> None:
    connection.execute(
        "DELETE FROM learned_preference_suppressions WHERE preference_key = ?",
        (preference_key,),
    )


def delete_all_preference_evidence(connection: sqlite3.Connection) -> None:
    """Delete the current learned-evidence epoch without committing."""
    connection.execute("DELETE FROM preference_evidence")


def delete_all_suppressions(connection: sqlite3.Connection) -> None:
    """Delete every learned-preference suppression without committing."""
    connection.execute("DELETE FROM learned_preference_suppressions")


def maximum_review_event_id(connection: sqlite3.Connection) -> int:
    """Return the current append-only review-event boundary, or zero."""
    row = connection.execute(
        "SELECT COALESCE(MAX(id), 0) FROM review_events"
    ).fetchone()
    return int(row[0])


def set_learning_reset_boundary(
    connection: sqlite3.Connection,
    *,
    reset_at: str,
    review_event_id: int,
) -> None:
    """Advance the singleton learned-state reset boundary without committing."""
    cursor = connection.execute(
        """
        UPDATE preference_state
        SET learning_reset_at = ?, learning_reset_review_event_id = ?
        WHERE singleton = 1
        """,
        (reset_at, review_event_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("preference state singleton is missing")
