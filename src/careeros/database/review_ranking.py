"""Durable authority and CAS primitives for materialized Review rankings.

Only reconstructible metadata and scores are stored here. Explicit and learned
preference values remain in their authoritative tables, and raw job text remains
in ``jobs``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from careeros.preferences.models import learned_ranking_authority_fingerprint

REVIEW_RANKING_SCORER_VERSION = "v1"
REVIEW_RANKING_NORMALIZER_VERSION = "v1"
REVIEW_RANKING_GENERATION_FORMAT_VERSION = "v1"

MODE_WITHOUT_LEARNED = 0
MODE_WITH_LEARNED = 1
MODE_WITHOUT_LEARNED_MASK = 1
MODE_WITH_LEARNED_MASK = 2
ALL_MODE_MASK = 3

FailureCode = Literal["database", "validation", "lost_claim", "cancelled", "unknown"]
FAILURE_CODES: frozenset[str] = frozenset(
    {"database", "validation", "lost_claim", "cancelled", "unknown"}
)


class RankingDerivedStateError(ValueError):
    """Persisted ranking-derived metadata is malformed or unsafe to decode."""


def canonical_json(value: object) -> str:
    """Return the one UTF-8 JSON representation used by ranking identities."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(namespace: str, value: object) -> str:
    """Hash a namespaced canonical JSON payload."""
    payload = canonical_json((namespace, value)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def opportunity_fingerprint(values: object) -> str:
    return canonical_sha256("careeros-opportunity-v1", values)


def corpus_fingerprint(values: object) -> str:
    return canonical_sha256("careeros-opportunity-corpus-v1", values)


EMPTY_LEARNED_FINGERPRINT = learned_ranking_authority_fingerprint(())


@dataclass(frozen=True)
class Authority:
    target_revision: int
    explicit_revision: int
    explicit_fingerprint: str
    opportunity_revision: int
    opportunity_fingerprint: str
    opportunity_count: int
    learned_revision: int
    learned_fingerprint: str
    scorer_version: str
    normalizer_version: str
    generation_format_version: str
    required_mode_mask: int
    state: str
    claim_token: str | None
    claim_target_revision: int | None
    claim_mode_mask: int | None
    claimed_at: str | None
    heartbeat_at: str | None
    attempt_count: int
    last_failed_at: str | None
    retry_not_before: str | None
    failure_code: str | None
    updated_at: str


@dataclass(frozen=True)
class Generation:
    id: int
    content_fingerprint: str
    target_revision: int
    learned_mode: int
    explicit_revision: int
    explicit_fingerprint: str
    opportunity_revision: int
    opportunity_fingerprint: str
    opportunity_count: int
    learned_revision: int
    learned_fingerprint: str
    scorer_version: str
    normalizer_version: str
    generation_format_version: str
    item_count: int
    items_fingerprint: str
    completed_at: str


@dataclass(frozen=True)
class RankingItem:
    generation_id: int
    opportunity_key: str
    group_id: str | None
    representative_job_id: int
    opportunity_fingerprint: str
    rank_ordinal: int
    raw_score: int
    public_match_score: int


@dataclass(frozen=True)
class Claim:
    token: str
    target_revision: int
    mode_mask: int
    attempt_count: int
    claimed_at: str


_AUTHORITY_COLUMNS = (
    "target_revision",
    "explicit_revision",
    "explicit_fingerprint",
    "opportunity_revision",
    "opportunity_fingerprint",
    "opportunity_count",
    "learned_revision",
    "learned_fingerprint",
    "scorer_version",
    "normalizer_version",
    "generation_format_version",
    "required_mode_mask",
    "state",
    "claim_token",
    "claim_target_revision",
    "claim_mode_mask",
    "claimed_at",
    "heartbeat_at",
    "attempt_count",
    "last_failed_at",
    "retry_not_before",
    "failure_code",
    "updated_at",
)

_GENERATION_COLUMNS = (
    "id",
    "content_fingerprint",
    "target_revision",
    "learned_mode",
    "explicit_revision",
    "explicit_fingerprint",
    "opportunity_revision",
    "opportunity_fingerprint",
    "opportunity_count",
    "learned_revision",
    "learned_fingerprint",
    "scorer_version",
    "normalizer_version",
    "generation_format_version",
    "item_count",
    "items_fingerprint",
    "completed_at",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    instant = value or _utc_now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC).isoformat()


def _row_values(row: sqlite3.Row, columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row[column] for column in columns)


def _validate_digest(value: str, label: str) -> None:
    if (
        len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _integer(
    value: object,
    label: str,
    *,
    nullable: bool = False,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RankingDerivedStateError(f"ranking {label} must be an integer")
    if minimum is not None and value < minimum:
        raise RankingDerivedStateError(f"ranking {label} is outside its schema domain")
    if maximum is not None and value > maximum:
        raise RankingDerivedStateError(f"ranking {label} is outside its schema domain")
    return value


def decode_ranking_integer(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Decode one non-null integer owned by ranking-derived metadata."""
    decoded = _integer(value, label, minimum=minimum, maximum=maximum)
    assert decoded is not None
    return decoded


def get_authority(connection: sqlite3.Connection) -> Authority:
    row = connection.execute(
        "SELECT * FROM review_ranking_authority WHERE singleton=1"
    ).fetchone()
    if row is None:
        # The authority row is entirely reconstructible ranking state.  Keep
        # its absence on the same privacy-safe recovery path as malformed
        # persisted ranking metadata.
        raise RankingDerivedStateError("ranking authority singleton is missing")
    values = list(_row_values(row, _AUTHORITY_COLUMNS))
    for column in (
        "target_revision",
        "explicit_revision",
        "opportunity_revision",
        "opportunity_count",
        "learned_revision",
        "attempt_count",
    ):
        values[_AUTHORITY_COLUMNS.index(column)] = _integer(
            row[column], column, minimum=0
        )
    values[_AUTHORITY_COLUMNS.index("required_mode_mask")] = _integer(
        row["required_mode_mask"], "required_mode_mask", minimum=0, maximum=3
    )
    values[_AUTHORITY_COLUMNS.index("claim_target_revision")] = _integer(
        row["claim_target_revision"],
        "claim_target_revision",
        nullable=True,
        minimum=0,
    )
    values[_AUTHORITY_COLUMNS.index("claim_mode_mask")] = _integer(
        row["claim_mode_mask"],
        "claim_mode_mask",
        nullable=True,
        minimum=0,
        maximum=3,
    )
    return Authority(*values)


def reset_authority(
    connection: sqlite3.Connection,
    *,
    explicit_revision: int,
    explicit_fingerprint: str,
    opportunity_revision: int,
    opportunity_fingerprint_value: str,
    opportunity_count: int,
    learned_revision: int,
    learned_fingerprint: str,
    now: datetime | None = None,
) -> None:
    """Atomically discard ranking state and seed a fresh rebuild target."""
    if connection.in_transaction:
        raise ValueError("ranking authority reset requires a free connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM review_ranking_current")
        connection.execute("DELETE FROM review_ranking_generations")
        connection.execute("DELETE FROM review_ranking_authority")
        # Keep the insert and all derived-state cleanup in this transaction.
        for digest, label in (
            (explicit_fingerprint, "explicit fingerprint"),
            (opportunity_fingerprint_value, "opportunity fingerprint"),
            (learned_fingerprint, "learned fingerprint"),
        ):
            _validate_digest(digest, label)
        for revision, label in (
            (explicit_revision, "explicit revision"),
            (opportunity_revision, "opportunity revision"),
            (learned_revision, "learned revision"),
        ):
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
            ):
                raise ValueError(f"{label} must be non-negative")
        if (
            isinstance(opportunity_count, bool)
            or not isinstance(opportunity_count, int)
            or opportunity_count < 0
        ):
            raise ValueError("opportunity count must be non-negative")
        connection.execute(
            """INSERT INTO review_ranking_authority(
                singleton,target_revision,explicit_revision,explicit_fingerprint,
                opportunity_revision,opportunity_fingerprint,opportunity_count,
                learned_revision,learned_fingerprint,scorer_version,
                normalizer_version,generation_format_version,required_mode_mask,
                state,updated_at
            ) VALUES (1,0,?,?,?,?,?,?,?,?,?,?,3,'rebuild_required',?)""",
            (
                explicit_revision,
                explicit_fingerprint,
                opportunity_revision,
                opportunity_fingerprint_value,
                opportunity_count,
                learned_revision,
                learned_fingerprint,
                REVIEW_RANKING_SCORER_VERSION,
                REVIEW_RANKING_NORMALIZER_VERSION,
                REVIEW_RANKING_GENERATION_FORMAT_VERSION,
                _stamp(now),
            ),
        )
        cursor = connection.execute(
            """UPDATE preference_state
               SET explicit_ranking_revision=?, explicit_ranking_fingerprint=?
               WHERE singleton=1""",
            (explicit_revision, explicit_fingerprint),
        )
        if cursor.rowcount != 1:
            raise ValueError("preference state singleton is missing")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def get_generation(
    connection: sqlite3.Connection, generation_id: int
) -> Generation | None:
    row = connection.execute(
        "SELECT * FROM review_ranking_generations WHERE id=?", (generation_id,)
    ).fetchone()
    return None if row is None else generation_from_row(row)


def generation_from_row(row: sqlite3.Row) -> Generation:
    """Decode one complete generation row selected by another bounded query."""
    values = list(_row_values(row, _GENERATION_COLUMNS))
    values[_GENERATION_COLUMNS.index("id")] = _integer(row["id"], "id")
    for column in (
        "target_revision",
        "explicit_revision",
        "opportunity_revision",
        "opportunity_count",
        "learned_revision",
        "item_count",
    ):
        values[_GENERATION_COLUMNS.index(column)] = _integer(
            row[column], column, minimum=0
        )
    values[_GENERATION_COLUMNS.index("learned_mode")] = _integer(
        row["learned_mode"], "learned_mode", minimum=0, maximum=1
    )
    return Generation(*values)


def list_generation_items(
    connection: sqlite3.Connection, generation_id: int
) -> tuple[RankingItem, ...]:
    rows = connection.execute(
        """SELECT generation_id, opportunity_key, group_id,
                  representative_job_id, opportunity_fingerprint, rank_ordinal,
                  raw_score, public_match_score
           FROM review_ranking_items
           WHERE generation_id=?
           ORDER BY rank_ordinal""",
        (generation_id,),
    ).fetchall()
    result = []
    for row in rows:
        values = list(tuple(row))
        values[0] = _integer(values[0], "generation_id")
        values[3] = _integer(values[3], "representative_job_id")
        values[5] = _integer(values[5], "rank_ordinal", minimum=1)
        values[6] = _integer(values[6], "raw_score")
        values[7] = _integer(values[7], "public_match_score", minimum=0, maximum=100)
        result.append(RankingItem(*values))
    return tuple(result)


_INCREMENT = object()


def _invalidate(
    connection: sqlite3.Connection,
    *,
    mode_mask: int,
    assignments: tuple[tuple[str, object], ...],
    now: datetime | None = None,
    increment_target: bool = True,
) -> None:
    if mode_mask not in (MODE_WITH_LEARNED_MASK, ALL_MODE_MASK):
        raise ValueError("invalid ranking invalidation mask")
    clauses = [
        "required_mode_mask=required_mode_mask|?",
        "state='rebuild_required'",
        "claim_token=NULL",
        "claim_target_revision=NULL",
        "claim_mode_mask=NULL",
        "claimed_at=NULL",
        "heartbeat_at=NULL",
        "attempt_count=0",
        "last_failed_at=NULL",
        "retry_not_before=NULL",
        "failure_code=NULL",
    ]
    if increment_target:
        clauses.insert(0, "target_revision=target_revision+1")
    parameters: list[object] = [mode_mask]
    for column, value in assignments:
        if value is _INCREMENT:
            clauses.append(f"{column}={column}+1")
        else:
            clauses.append(f"{column}=?")
            parameters.append(value)
    clauses.append("updated_at=?")
    parameters.append(_stamp(now))
    cursor = connection.execute(
        f"UPDATE review_ranking_authority SET {', '.join(clauses)} WHERE singleton=1",
        parameters,
    )
    if cursor.rowcount != 1:
        raise ValueError("ranking authority singleton is missing")
    if mode_mask == ALL_MODE_MASK:
        connection.execute("DELETE FROM review_ranking_current")
    else:
        connection.execute(
            "DELETE FROM review_ranking_current WHERE learned_mode=?",
            (MODE_WITH_LEARNED,),
        )


def update_explicit_authority(
    connection: sqlite3.Connection,
    fingerprint: str,
    *,
    previous_fingerprint: str | None = None,
    effective_learned_fingerprint: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Invalidate both modes for one genuine explicit-authority change."""
    _validate_digest(fingerprint, "explicit fingerprint")
    if effective_learned_fingerprint is not None:
        _validate_digest(effective_learned_fingerprint, "learned fingerprint")
    authority = get_authority(connection)
    if (
        previous_fingerprint is not None
        and authority.explicit_fingerprint != previous_fingerprint
    ):
        raise ValueError("explicit ranking authority changed concurrently")
    explicit_changed = authority.explicit_fingerprint != fingerprint
    learned_changed = (
        effective_learned_fingerprint is not None
        and authority.learned_fingerprint != effective_learned_fingerprint
    )
    if not explicit_changed and not learned_changed:
        return False
    assignments: list[tuple[str, object]] = []
    if explicit_changed:
        assignments.extend(
            (("explicit_revision", _INCREMENT), ("explicit_fingerprint", fingerprint))
        )
        connection.execute(
            """UPDATE preference_state
               SET explicit_ranking_revision=explicit_ranking_revision+1,
                   explicit_ranking_fingerprint=?
               WHERE singleton=1""",
            (fingerprint,),
        )
    if learned_changed:
        assignments.extend(
            (
                ("learned_revision", _INCREMENT),
                ("learned_fingerprint", effective_learned_fingerprint),
            )
        )
    _invalidate(
        connection,
        mode_mask=ALL_MODE_MASK,
        assignments=tuple(assignments),
        now=now,
        increment_target=True,
    )
    return True


def update_learned_authority(
    connection: sqlite3.Connection,
    fingerprint: str,
    *,
    previous_fingerprint: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Invalidate only learned-enabled ranking when its effective tuple changes."""
    _validate_digest(fingerprint, "learned fingerprint")
    authority = get_authority(connection)
    if (
        previous_fingerprint is not None
        and authority.learned_fingerprint != previous_fingerprint
    ):
        raise ValueError("learned ranking authority changed concurrently")
    if authority.learned_fingerprint == fingerprint:
        return False
    _invalidate(
        connection,
        mode_mask=MODE_WITH_LEARNED_MASK,
        assignments=(
            ("learned_revision", _INCREMENT),
            ("learned_fingerprint", fingerprint),
        ),
        now=now,
        increment_target=True,
    )
    return True


def update_opportunity_authority(
    connection: sqlite3.Connection,
    fingerprint: str,
    count: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Invalidate both modes when the scorer-visible opportunity corpus changes."""
    _validate_digest(fingerprint, "opportunity fingerprint")
    if count < 0:
        raise ValueError("opportunity count must be non-negative")
    authority = get_authority(connection)
    if (
        authority.opportunity_fingerprint == fingerprint
        and authority.opportunity_count == count
    ):
        return False
    _invalidate(
        connection,
        mode_mask=ALL_MODE_MASK,
        assignments=(
            ("opportunity_revision", _INCREMENT),
            ("opportunity_fingerprint", fingerprint),
            ("opportunity_count", count),
        ),
        now=now,
    )
    return True


def invalidate_all(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> bool:
    """Mark both ranking modes for rebuild after an integrity audit failure."""
    authority = get_authority(connection)
    if authority.state != "ready" or authority.required_mode_mask != 0:
        return False
    connection.execute("BEGIN IMMEDIATE")
    try:
        _invalidate(
            connection,
            mode_mask=ALL_MODE_MASK,
            assignments=(),
            now=now,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return True


def reconcile_authority(
    connection: sqlite3.Connection,
    *,
    opportunity_fingerprint_value: str,
    opportunity_count: int,
    explicit_fingerprint: str,
    learned_fingerprint: str,
    expected_authority: Authority | None = None,
    now: datetime | None = None,
) -> bool | None:
    """Reconcile durable ranking metadata with current scorer-visible state.

    This is the recovery boundary used by startup and administrative rebuilds.
    It repairs derived authority metadata, invalidating only the modes affected
    by the observed identity changes.  The caller supplies bounded snapshots;
    no scoring or corpus work is performed while the write transaction is
    held.  When ``expected_authority`` is supplied, ``None`` means that a
    ranking-affecting writer changed the freshness marker after the caller's
    read snapshot; no reconciliation writes are applied in that case.
    """
    _validate_digest(opportunity_fingerprint_value, "opportunity fingerprint")
    _validate_digest(explicit_fingerprint, "explicit fingerprint")
    _validate_digest(learned_fingerprint, "learned fingerprint")
    if opportunity_count < 0:
        raise ValueError("opportunity count must be non-negative")
    if connection.in_transaction:
        raise ValueError("ranking authority reconciliation requires a free connection")
    connection.execute("BEGIN IMMEDIATE")
    try:
        authority = get_authority(connection)
        if expected_authority is not None and authority != expected_authority:
            connection.commit()
            return None
        state = connection.execute(
            "SELECT explicit_ranking_revision, explicit_ranking_fingerprint "
            "FROM preference_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise ValueError("preference state singleton is missing")
        explicit_metadata_changed = (
            str(state["explicit_ranking_fingerprint"]) != explicit_fingerprint
            or decode_ranking_integer(
                state["explicit_ranking_revision"], "explicit_ranking_revision"
            )
            != authority.explicit_revision
        )
        explicit_changed = authority.explicit_fingerprint != explicit_fingerprint
        learned_changed = authority.learned_fingerprint != learned_fingerprint
        opportunity_changed = (
            authority.opportunity_fingerprint != opportunity_fingerprint_value
            or authority.opportunity_count != opportunity_count
        )
        runtime_changed = (
            authority.scorer_version != REVIEW_RANKING_SCORER_VERSION
            or authority.normalizer_version != REVIEW_RANKING_NORMALIZER_VERSION
            or authority.generation_format_version
            != REVIEW_RANKING_GENERATION_FORMAT_VERSION
        )
        if explicit_metadata_changed or explicit_changed:
            explicit_revision = authority.explicit_revision + (
                1 if explicit_changed else 0
            )
            connection.execute(
                "UPDATE preference_state SET explicit_ranking_revision=?, "
                "explicit_ranking_fingerprint=? WHERE singleton=1",
                (explicit_revision, explicit_fingerprint),
            )
        assignments: list[tuple[str, object]] = []
        mode_mask = 0
        if explicit_changed:
            assignments.extend(
                (
                    ("explicit_revision", _INCREMENT),
                    ("explicit_fingerprint", explicit_fingerprint),
                )
            )
            mode_mask |= ALL_MODE_MASK
        if learned_changed:
            assignments.extend(
                (
                    ("learned_revision", _INCREMENT),
                    ("learned_fingerprint", learned_fingerprint),
                )
            )
            mode_mask |= MODE_WITH_LEARNED_MASK
        if opportunity_changed:
            assignments.extend(
                (
                    ("opportunity_revision", _INCREMENT),
                    ("opportunity_fingerprint", opportunity_fingerprint_value),
                    ("opportunity_count", opportunity_count),
                )
            )
            mode_mask |= ALL_MODE_MASK
        if runtime_changed:
            assignments.extend(
                (
                    ("scorer_version", REVIEW_RANKING_SCORER_VERSION),
                    ("normalizer_version", REVIEW_RANKING_NORMALIZER_VERSION),
                    (
                        "generation_format_version",
                        REVIEW_RANKING_GENERATION_FORMAT_VERSION,
                    ),
                )
            )
            mode_mask |= ALL_MODE_MASK
        if mode_mask:
            _invalidate(
                connection,
                mode_mask=mode_mask,
                assignments=tuple(assignments),
                now=now,
            )
        elif explicit_metadata_changed:
            connection.execute(
                "UPDATE preference_state SET explicit_ranking_revision=? "
                "WHERE singleton=1",
                (authority.explicit_revision,),
            )
        connection.commit()
        return bool(mode_mask or explicit_metadata_changed)
    except Exception:
        connection.rollback()
        raise


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def claim(
    connection: sqlite3.Connection,
    *,
    mode_mask: int = ALL_MODE_MASK,
    stale_seconds: int = 300,
    now: datetime | None = None,
) -> Claim | None:
    """Claim required modes under ``BEGIN IMMEDIATE``, recovering stale leases."""
    if connection.in_transaction:
        raise ValueError("ranking claim requires a transaction-free connection")
    if mode_mask < 1 or mode_mask > ALL_MODE_MASK:
        raise ValueError("claim mode mask must be between 1 and 3")
    if stale_seconds < 0:
        raise ValueError("stale_seconds must be non-negative")
    instant = now or _utc_now()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    instant = instant.astimezone(UTC)
    connection.execute("BEGIN IMMEDIATE")
    try:
        authority = get_authority(connection)
        if authority.required_mode_mask == 0:
            connection.commit()
            return None
        if authority.required_mode_mask == ALL_MODE_MASK and mode_mask != ALL_MODE_MASK:
            connection.commit()
            return None
        retry_at = _parse_timestamp(authority.retry_not_before)
        if authority.state == "failed" and retry_at is not None and retry_at > instant:
            connection.commit()
            return None
        if authority.state == "building":
            heartbeat_at = _parse_timestamp(authority.heartbeat_at)
            if (
                heartbeat_at is not None
                and (instant - heartbeat_at).total_seconds() <= stale_seconds
            ):
                connection.commit()
                return None
        selected_mask = authority.required_mode_mask & mode_mask
        if selected_mask == 0:
            connection.commit()
            return None
        token = secrets.token_hex(16)
        timestamp = _stamp(instant)
        attempt_count = authority.attempt_count + 1
        cursor = connection.execute(
            """UPDATE review_ranking_authority
               SET state='building', claim_token=?,
                   claim_target_revision=target_revision, claim_mode_mask=?,
                   claimed_at=?, heartbeat_at=?, attempt_count=?,
                   last_failed_at=NULL, retry_not_before=NULL, failure_code=NULL,
                   updated_at=?
               WHERE singleton=1 AND target_revision=?""",
            (
                token,
                selected_mask,
                timestamp,
                timestamp,
                attempt_count,
                timestamp,
                authority.target_revision,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return None
        # The claimed modes now own their replacement lifecycle.  Drop only
        # those current bindings before removing newly unbound reconstructible
        # generations, so a same-target retry cannot collide with an old row.
        # Keeping this cleanup in the claim transaction prevents observers from
        # seeing building authority with protected generations for claimed
        # modes, while preserving an unaffected mode zero on learned-only work.
        for mode in (MODE_WITHOUT_LEARNED, MODE_WITH_LEARNED):
            if selected_mask & (1 << mode):
                connection.execute(
                    "DELETE FROM review_ranking_current WHERE learned_mode=?",
                    (mode,),
                )
        # This also clears generations abandoned by an interrupted worker,
        # retaining the existing stale-claim takeover behavior.
        delete_unbound_generations(connection)
        connection.commit()
        return Claim(
            token=token,
            target_revision=authority.target_revision,
            mode_mask=selected_mask,
            attempt_count=attempt_count,
            claimed_at=timestamp,
        )
    except Exception:
        connection.rollback()
        raise


def _claim_parts(
    claim_or_token: Claim | str, target_revision: int | None
) -> tuple[str, int]:
    if isinstance(claim_or_token, Claim):
        if (
            target_revision is not None
            and target_revision != claim_or_token.target_revision
        ):
            raise ValueError("claim target revision conflicts with supplied target")
        return claim_or_token.token, claim_or_token.target_revision
    if target_revision is None:
        raise ValueError("target_revision is required when passing a claim token")
    return claim_or_token, target_revision


def heartbeat(
    connection: sqlite3.Connection,
    claim_or_token: Claim | str,
    target_revision: int | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    token, target = _claim_parts(claim_or_token, target_revision)
    timestamp = _stamp(now)
    cursor = connection.execute(
        """UPDATE review_ranking_authority
           SET heartbeat_at=?, updated_at=?
           WHERE singleton=1 AND state='building' AND claim_token=?
             AND claim_target_revision=? AND target_revision=?""",
        (timestamp, timestamp, token, target, target),
    )
    connection.commit()
    return cursor.rowcount == 1


def fail_claim(
    connection: sqlite3.Connection,
    claim_or_token: Claim | str,
    code: FailureCode | str,
    target_revision: int | None = None,
    *,
    now: datetime | None = None,
    base_delay_seconds: int = 5,
    maximum_delay_seconds: int = 300,
) -> bool:
    """Fail the current claim with bounded exponential retry backoff."""
    if code not in FAILURE_CODES:
        raise ValueError("invalid ranking failure code")
    if base_delay_seconds < 1 or maximum_delay_seconds < base_delay_seconds:
        raise ValueError("invalid retry delay bounds")
    token, target = _claim_parts(claim_or_token, target_revision)
    authority = get_authority(connection)
    if (
        authority.state != "building"
        or authority.claim_token != token
        or authority.claim_target_revision != target
        or authority.target_revision != target
    ):
        return False
    instant = now or _utc_now()
    delay = min(
        maximum_delay_seconds,
        base_delay_seconds * (2 ** max(0, authority.attempt_count - 1)),
    )
    failed_at = _stamp(instant)
    retry_at = _stamp(instant + timedelta(seconds=delay))
    cursor = connection.execute(
        """UPDATE review_ranking_authority
           SET state='failed', failure_code=?, last_failed_at=?, retry_not_before=?,
               claim_token=NULL, claim_target_revision=NULL, claim_mode_mask=NULL,
               claimed_at=NULL, heartbeat_at=NULL, updated_at=?
           WHERE singleton=1 AND state='building' AND claim_token=?
             AND claim_target_revision=? AND target_revision=?""",
        (code, failed_at, retry_at, failed_at, token, target, target),
    )
    connection.commit()
    return cursor.rowcount == 1


def _expected_learned_identity(authority: Authority, mode: int) -> tuple[int, str]:
    if mode == MODE_WITHOUT_LEARNED:
        return 0, EMPTY_LEARNED_FINGERPRINT
    return authority.learned_revision, authority.learned_fingerprint


def fully_prepared(connection: sqlite3.Connection) -> bool:
    """Check bounded generation headers only; never scan corpus or item rows."""
    authority = get_authority(connection)
    if (
        authority.state != "ready"
        or authority.required_mode_mask != 0
        or authority.claim_token is not None
        or authority.scorer_version != REVIEW_RANKING_SCORER_VERSION
        or authority.normalizer_version != REVIEW_RANKING_NORMALIZER_VERSION
        or authority.generation_format_version
        != REVIEW_RANKING_GENERATION_FORMAT_VERSION
    ):
        return False
    rows = connection.execute(
        """SELECT c.learned_mode, c.bound_target_revision,
                  g.target_revision, g.learned_mode AS generation_mode,
                  g.explicit_revision, g.explicit_fingerprint,
                  g.opportunity_revision, g.opportunity_fingerprint,
                  g.opportunity_count, g.item_count,
                  g.learned_revision, g.learned_fingerprint,
                  g.scorer_version, g.normalizer_version,
                  g.generation_format_version
           FROM review_ranking_current AS c
           JOIN review_ranking_generations AS g ON g.id=c.generation_id
           ORDER BY c.learned_mode"""
    ).fetchall()
    if len(rows) != 2 or [
        _integer(row["learned_mode"], "learned_mode", minimum=0, maximum=1)
        for row in rows
    ] != [0, 1]:
        return False
    for row in rows:
        mode = _integer(row["learned_mode"], "learned_mode", minimum=0, maximum=1)
        assert mode is not None
        learned_revision, learned_identity = _expected_learned_identity(authority, mode)
        if (
            _integer(
                row["bound_target_revision"],
                "bound_target_revision",
                minimum=0,
            )
            != authority.target_revision
            or (
                _integer(row["target_revision"], "target_revision", minimum=0)
                != authority.target_revision
                and not (
                    _integer(
                        row["learned_mode"],
                        "learned_mode",
                        minimum=0,
                        maximum=1,
                    )
                    == MODE_WITHOUT_LEARNED
                    and (
                        _integer(
                            row["target_revision"],
                            "target_revision",
                            minimum=0,
                        )
                        or 0
                    )
                    < authority.target_revision
                )
            )
            or _integer(
                row["generation_mode"],
                "generation_mode",
                minimum=0,
                maximum=1,
            )
            != mode
            or _integer(row["explicit_revision"], "explicit_revision", minimum=0)
            != authority.explicit_revision
            or str(row["explicit_fingerprint"]) != authority.explicit_fingerprint
            or _integer(row["opportunity_revision"], "opportunity_revision", minimum=0)
            != authority.opportunity_revision
            or str(row["opportunity_fingerprint"]) != authority.opportunity_fingerprint
            or _integer(row["opportunity_count"], "opportunity_count", minimum=0)
            != authority.opportunity_count
            or _integer(row["item_count"], "item_count", minimum=0)
            != authority.opportunity_count
            or _integer(row["learned_revision"], "learned_revision", minimum=0)
            != learned_revision
            or str(row["learned_fingerprint"]) != learned_identity
            or str(row["scorer_version"]) != REVIEW_RANKING_SCORER_VERSION
            or str(row["normalizer_version"]) != REVIEW_RANKING_NORMALIZER_VERSION
            or str(row["generation_format_version"])
            != REVIEW_RANKING_GENERATION_FORMAT_VERSION
        ):
            return False
    return True


def _validate_generation_for_activation(
    connection: sqlite3.Connection,
    authority: Authority,
    mode: int,
    generation_id: int,
    allow_older_target: bool = False,
) -> bool:
    generation = get_generation(connection, generation_id)
    if generation is None:
        return False
    learned_revision, learned_identity = _expected_learned_identity(authority, mode)
    if (
        (
            generation.target_revision != authority.target_revision
            and not (
                allow_older_target
                and mode == MODE_WITHOUT_LEARNED
                and generation.target_revision < authority.target_revision
            )
        )
        or generation.learned_mode != mode
        or generation.explicit_revision != authority.explicit_revision
        or generation.explicit_fingerprint != authority.explicit_fingerprint
        or generation.opportunity_revision != authority.opportunity_revision
        or generation.opportunity_fingerprint != authority.opportunity_fingerprint
        or generation.opportunity_count != authority.opportunity_count
        or generation.learned_revision != learned_revision
        or generation.learned_fingerprint != learned_identity
        or generation.scorer_version != authority.scorer_version
        or generation.normalizer_version != authority.normalizer_version
        or generation.generation_format_version != authority.generation_format_version
        or generation.content_fingerprint
        != canonical_sha256(
            "careeros-ranking-generation-v1",
            (
                generation.target_revision,
                generation.learned_mode,
                generation.explicit_fingerprint,
                generation.opportunity_fingerprint,
                generation.learned_fingerprint,
                generation.items_fingerprint,
            ),
        )
    ):
        return False

    from careeros.ranking.materialization import (
        items_fingerprint,
        snapshot_opportunities,
    )

    opportunities = snapshot_opportunities(connection)
    expected = {item.key: item for item in opportunities}
    if (
        len(opportunities) != authority.opportunity_count
        or corpus_fingerprint(
            tuple((item.key, item.fingerprint) for item in opportunities)
        )
        != authority.opportunity_fingerprint
    ):
        return False
    items = list_generation_items(connection, generation_id)
    if (
        len(items) != generation.item_count
        or len(items) != len(expected)
        or len({item.opportunity_key for item in items}) != len(items)
        or len({item.rank_ordinal for item in items}) != len(items)
    ):
        return False
    if tuple(item.rank_ordinal for item in items) != tuple(range(1, len(items) + 1)):
        return False
    for item in items:
        opportunity = expected.get(item.opportunity_key)
        if (
            opportunity is None
            or item.group_id != opportunity.group_id
            or item.representative_job_id != opportunity.representative_job_id
            or item.opportunity_fingerprint != opportunity.fingerprint
        ):
            return False
    return items_fingerprint(items) == generation.items_fingerprint


def validate_generation(
    connection: sqlite3.Connection, generation_id: int, *, mode: int | None = None
) -> bool:
    """Run the complete expensive integrity check for one generation."""
    authority = get_authority(connection)
    generation = get_generation(connection, generation_id)
    if generation is None:
        return False
    selected_mode = generation.learned_mode if mode is None else mode
    return _validate_generation_for_activation(
        connection, authority, selected_mode, generation_id
    )


def validate_current_generations(connection: sqlite3.Connection) -> bool:
    """Run the complete expensive validation for both ready current bindings."""
    if not fully_prepared(connection):
        return False
    authority = get_authority(connection)
    rows = connection.execute(
        """SELECT learned_mode,generation_id
           FROM review_ranking_current ORDER BY learned_mode"""
    ).fetchall()
    if len(rows) != 2:
        return False
    return all(
        _validate_generation_for_activation(
            connection,
            authority,
            decode_ranking_integer(
                row["learned_mode"], "learned_mode", minimum=0, maximum=1
            ),
            decode_ranking_integer(row["generation_id"], "generation_id"),
            allow_older_target=decode_ranking_integer(
                row["learned_mode"], "learned_mode", minimum=0, maximum=1
            )
            == MODE_WITHOUT_LEARNED,
        )
        for row in rows
    )


def activate(
    connection: sqlite3.Connection,
    claim_or_token: Claim | str,
    target_revision: int | None,
    generations: dict[int, int],
    *,
    manage_transaction: bool = True,
    now: datetime | None = None,
) -> bool:
    """Atomically bind exactly the modes owned by a still-current claim."""
    token, target = _claim_parts(claim_or_token, target_revision)
    if manage_transaction:
        if connection.in_transaction:
            raise ValueError(
                "managed activation requires a transaction-free connection"
            )
        connection.execute("BEGIN IMMEDIATE")
    try:
        authority = get_authority(connection)
        expected_modes = {
            mode
            for mode in (0, 1)
            if authority.claim_mode_mask and authority.claim_mode_mask & (1 << mode)
        }
        rebinding_mode_zero = expected_modes == {MODE_WITH_LEARNED}
        activation_generations = dict(generations)
        if rebinding_mode_zero:
            current_zero = connection.execute(
                "SELECT generation_id FROM review_ranking_current WHERE learned_mode=0"
            ).fetchone()
            if current_zero is None:
                if manage_transaction:
                    connection.rollback()
                return False
            activation_generations[MODE_WITHOUT_LEARNED] = decode_ranking_integer(
                current_zero[0], "generation_id"
            )
        if (
            authority.state != "building"
            or authority.claim_token != token
            or authority.claim_target_revision != target
            or authority.target_revision != target
            or set(generations) != expected_modes
            or not expected_modes
        ):
            if manage_transaction:
                connection.rollback()
            return False
        if any(
            not _validate_generation_for_activation(
                connection,
                authority,
                mode,
                generation_id,
                allow_older_target=rebinding_mode_zero,
            )
            for mode, generation_id in activation_generations.items()
        ):
            if manage_transaction:
                connection.rollback()
            return False
        timestamp = _stamp(now)
        for mode, generation_id in sorted(activation_generations.items()):
            connection.execute(
                """INSERT INTO review_ranking_current(
                       learned_mode, generation_id, bound_target_revision, bound_at
                   ) VALUES (?,?,?,?)
                   ON CONFLICT(learned_mode) DO UPDATE SET
                       generation_id=excluded.generation_id,
                       bound_target_revision=excluded.bound_target_revision,
                       bound_at=excluded.bound_at""",
                (mode, generation_id, target, timestamp),
            )
        activated_mask = sum(1 << mode for mode in activation_generations)
        remaining = authority.required_mode_mask & ~activated_mask
        cursor = connection.execute(
            """UPDATE review_ranking_authority
               SET required_mode_mask=?, state=?, claim_token=NULL,
                   claim_target_revision=NULL, claim_mode_mask=NULL,
                   claimed_at=NULL, heartbeat_at=NULL, last_failed_at=NULL,
                   retry_not_before=NULL, failure_code=NULL, updated_at=?
               WHERE singleton=1 AND state='building' AND claim_token=?
                 AND claim_target_revision=? AND target_revision=?""",
            (
                remaining,
                "ready" if remaining == 0 else "rebuild_required",
                timestamp,
                token,
                target,
                target,
            ),
        )
        if cursor.rowcount != 1:
            if manage_transaction:
                connection.rollback()
            return False
        # Keep cleanup inside the activation transaction.  Unbound generations
        # are reconstructible; current bindings are explicitly preserved.
        delete_unbound_generations(connection)
        if manage_transaction:
            connection.commit()
        return True
    except Exception:
        if manage_transaction:
            connection.rollback()
        raise


def delete_unbound_generations(connection: sqlite3.Connection) -> int:
    """Delete reconstructible generations, explicitly preserving both currents."""
    cursor = connection.execute(
        """DELETE FROM review_ranking_generations
           WHERE id NOT IN (SELECT generation_id FROM review_ranking_current)"""
    )
    return cursor.rowcount
