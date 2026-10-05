"""Blocking durable rebuild of materialized Review ranking generations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from careeros.database.connection import connect_current_rw
from careeros.database.groups import list_all_groups
from careeros.database.review_ranking import (
    ALL_MODE_MASK,
    MODE_WITH_LEARNED,
    MODE_WITH_LEARNED_MASK,
    MODE_WITHOUT_LEARNED,
    MODE_WITHOUT_LEARNED_MASK,
    RankingDerivedStateError,
    activate,
    claim,
    decode_ranking_integer,
    fail_claim,
    get_authority,
    heartbeat,
    invalidate_all,
    reconcile_authority,
    reset_authority,
    validate_current_generations,
)
from careeros.preferences.models import explicit_ranking_authority_fingerprint
from careeros.preferences.service import (
    build_ranking_adaptive_preference_set,
    effective_learned_authority_fingerprint,
    resolve_explicit_profile,
)
from careeros.ranking.materialization import (
    MaterializedScore,
    Opportunity,
    canonical_opportunity_key,
    materialize_generation,
    opportunity_corpus_fingerprint,
    snapshot_opportunities,
)
from careeros.ranking.scorer import score_groups


@dataclass(frozen=True)
class RebuildResult:
    target_revision: int
    mode_mask: int
    generation_ids: tuple[int, ...]


_MAX_RECONCILIATION_ATTEMPTS = 3


def recover_and_rebuild(
    connection: sqlite3.Connection, *, stale_seconds: int = 300
) -> RebuildResult | None:
    """Reconcile, audit ready state, and rebuild any invalid current ranking.

    This is the privileged recovery seam used by startup, explicit audit wake,
    and the administrative rebuild command.  Ordinary durable polling calls
    :func:`rebuild_once` and deliberately skips the complete item audit.
    """
    try:
        reconciled = _reconcile_current_authority(connection)
    except RankingDerivedStateError:
        if _preference_ranking_revision_is_malformed(connection):
            _recover_preference_ranking_metadata(connection)
            reconciled = _reconcile_current_authority(connection)
        else:
            # A present malformed authority is disposable too.  Do not catch
            # unrelated ValueError failures (for example malformed preferences).
            try:
                get_authority(connection)
            except RankingDerivedStateError:
                _reset_from_live_identities(connection)
                reconciled = True
            else:
                raise
    if not reconciled:
        return None
    try:
        authority = get_authority(connection)
        ready = authority.state == "ready" and authority.required_mode_mask == 0
        valid = validate_current_generations(connection) if ready else False
    except RankingDerivedStateError:
        # A malformed persisted ranking-derived value is itself audit evidence.
        # Keep the public failure bounded while making the state claimable.
        valid = False
    if not valid:
        # reconcile_authority may already have invalidated this target.  The
        # idempotent audit invalidation only advances a still-fully-ready
        # target, preventing double target advancement on one divergence.
        try:
            invalidate_all(connection)
        except RankingDerivedStateError:
            # A malformed authority cannot be repaired by this layer; the
            # schema/database primitive owns reconstruction of that singleton.
            raise
    return rebuild_once(connection, stale_seconds=stale_seconds)


def _preference_ranking_revision_is_malformed(
    connection: sqlite3.Connection,
) -> bool:
    """Identify only the reconstructible schema-14 preference revision field."""
    row = connection.execute(
        "SELECT explicit_ranking_revision FROM preference_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        return False
    try:
        decode_ranking_integer(
            row["explicit_ranking_revision"],
            "explicit_ranking_revision",
            minimum=0,
        )
    except RankingDerivedStateError:
        return True
    return False


def _recover_preference_ranking_metadata(connection: sqlite3.Connection) -> None:
    """Reconstruct only preference metadata owned by durable ranking state."""
    try:
        authority = get_authority(connection)
    except RankingDerivedStateError:
        authority = None
    seed_revision = authority.explicit_revision if authority is not None else 0
    opportunities, explicit_fingerprint, learned_fingerprint = (
        _live_identities_with_temporary_preference_metadata(
            connection,
            explicit_revision=seed_revision,
        )
    )
    if authority is not None and authority.explicit_fingerprint == explicit_fingerprint:
        if connection.in_transaction:
            raise ValueError("ranking metadata repair requires a free connection")
        connection.execute("BEGIN IMMEDIATE")
        try:
            current = get_authority(connection)
            if current != authority:
                raise ValueError("ranking authority changed concurrently")
            cursor = connection.execute(
                """UPDATE preference_state
                   SET explicit_ranking_revision=?,
                       explicit_ranking_fingerprint=?
                   WHERE singleton=1""",
                (authority.explicit_revision, explicit_fingerprint),
            )
            if cursor.rowcount != 1:
                raise ValueError("preference state singleton is missing")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return

    explicit_revision = 0 if authority is None else authority.explicit_revision + 1
    reset_authority(
        connection,
        explicit_revision=explicit_revision,
        explicit_fingerprint=explicit_fingerprint,
        opportunity_revision=0,
        opportunity_fingerprint_value=opportunity_corpus_fingerprint(opportunities),
        opportunity_count=len(opportunities),
        learned_revision=0,
        learned_fingerprint=learned_fingerprint,
    )


def _live_identities_with_temporary_preference_metadata(
    connection: sqlite3.Connection,
    *,
    explicit_revision: int,
) -> tuple[tuple[Opportunity, ...], str, str]:
    """Read live Product-1 identities without committing temporary metadata."""
    if connection.in_transaction:
        raise ValueError("ranking identity recovery requires a free connection")
    connection.execute("SAVEPOINT ranking_preference_metadata_recovery")
    try:
        cursor = connection.execute(
            """UPDATE preference_state
               SET explicit_ranking_revision=?, explicit_ranking_fingerprint=?
               WHERE singleton=1""",
            (explicit_revision, "0" * 64),
        )
        if cursor.rowcount != 1:
            raise ValueError("preference state singleton is missing")
        opportunities = snapshot_opportunities(connection)
        explicit = resolve_explicit_profile(connection).explicit_profile
        explicit_fingerprint = explicit_ranking_authority_fingerprint(explicit)
        learned_fingerprint = effective_learned_authority_fingerprint(connection)
    finally:
        connection.execute("ROLLBACK TO ranking_preference_metadata_recovery")
        connection.execute("RELEASE ranking_preference_metadata_recovery")
    return opportunities, explicit_fingerprint, learned_fingerprint


def _reconcile_current_authority(connection: sqlite3.Connection) -> bool:
    """Reconcile one coherent live snapshot against its authority CAS marker."""
    if connection.in_transaction:
        raise ValueError("ranking identity snapshot requires a free connection")
    for _attempt in range(_MAX_RECONCILIATION_ATTEMPTS):
        connection.execute("BEGIN")
        try:
            opportunities = snapshot_opportunities(connection)
            explicit = resolve_explicit_profile(connection).explicit_profile
            explicit_fingerprint = explicit_ranking_authority_fingerprint(explicit)
            learned_fingerprint = effective_learned_authority_fingerprint(connection)
            authority_present = (
                connection.execute(
                    "SELECT 1 FROM review_ranking_authority WHERE singleton=1"
                ).fetchone()
                is not None
            )
            if authority_present:
                expected_authority = get_authority(connection)
                explicit_revision = None
            else:
                expected_authority = None
                preference_state = connection.execute(
                    "SELECT explicit_ranking_revision FROM preference_state "
                    "WHERE singleton=1"
                ).fetchone()
                if preference_state is None:
                    raise RankingDerivedStateError(
                        "preference state singleton is missing"
                    )
                explicit_revision = decode_ranking_integer(
                    preference_state["explicit_ranking_revision"],
                    "explicit_ranking_revision",
                    minimum=0,
                )
        finally:
            connection.rollback()

        if expected_authority is None:
            assert explicit_revision is not None
            _reset_from_live_identities(
                connection,
                opportunities=opportunities,
                explicit_fingerprint=explicit_fingerprint,
                learned_fingerprint=learned_fingerprint,
                explicit_revision=explicit_revision,
            )
            return True
        result = reconcile_authority(
            connection,
            opportunity_fingerprint_value=opportunity_corpus_fingerprint(opportunities),
            opportunity_count=len(opportunities),
            explicit_fingerprint=explicit_fingerprint,
            learned_fingerprint=learned_fingerprint,
            expected_authority=expected_authority,
        )
        if result is not None:
            return True
    return False


def _reset_from_live_identities(
    connection: sqlite3.Connection,
    *,
    opportunities: tuple[Opportunity, ...] | None = None,
    explicit_fingerprint: str | None = None,
    learned_fingerprint: str | None = None,
    explicit_revision: int | None = None,
) -> None:
    if opportunities is None:
        opportunities = snapshot_opportunities(connection)
    if explicit_fingerprint is None:
        explicit = resolve_explicit_profile(connection).explicit_profile
        explicit_fingerprint = explicit_ranking_authority_fingerprint(explicit)
    if learned_fingerprint is None:
        learned_fingerprint = effective_learned_authority_fingerprint(connection)
    if explicit_revision is None:
        state = connection.execute(
            "SELECT explicit_ranking_revision FROM preference_state WHERE singleton=1"
        ).fetchone()
        if state is None:
            raise RankingDerivedStateError("preference state singleton is missing")
        explicit_revision = decode_ranking_integer(
            state["explicit_ranking_revision"],
            "explicit_ranking_revision",
            minimum=0,
        )
    reset_authority(
        connection,
        explicit_revision=explicit_revision,
        explicit_fingerprint=explicit_fingerprint,
        opportunity_revision=0,
        opportunity_fingerprint_value=opportunity_corpus_fingerprint(opportunities),
        opportunity_count=len(opportunities),
        learned_revision=0,
        learned_fingerprint=learned_fingerprint,
    )


def rebuild_once(
    connection: sqlite3.Connection, *, stale_seconds: int = 300
) -> RebuildResult | None:
    """Claim, score, persist, and atomically activate the current target."""
    # Collection/regroup callers normally update this in their write transaction;
    # admin and startup recovery also need to repair a manually changed corpus.
    if not _reconcile_current_authority(connection):
        return None
    explicit = resolve_explicit_profile(connection).explicit_profile
    assertion = claim(connection, mode_mask=ALL_MODE_MASK, stale_seconds=stale_seconds)
    if assertion is None:
        return None
    try:
        connection.execute("BEGIN")
        try:
            claimed_authority = get_authority(connection)
            if (
                claimed_authority.claim_token != assertion.token
                or claimed_authority.claim_target_revision != assertion.target_revision
                or claimed_authority.target_revision != assertion.target_revision
            ):
                connection.rollback()
                return None
            explicit = resolve_explicit_profile(connection).explicit_profile
            groups = list_all_groups(connection)
            by_key = {item.key: item for item in snapshot_opportunities(connection)}
            adaptive_by_mode = {
                mode: build_ranking_adaptive_preference_set(
                    connection,
                    supplied_profile=explicit,
                    enabled=mode == MODE_WITH_LEARNED,
                )
                for mode in (MODE_WITHOUT_LEARNED, MODE_WITH_LEARNED)
                if assertion.mode_mask & (1 << mode)
            }
        finally:
            if connection.in_transaction:
                connection.rollback()

        def score_mode(mode: int) -> list[MaterializedScore]:
            ranked = score_groups(
                groups,
                explicit.to_candidate_profile(),
                adaptive_preferences=adaptive_by_mode[mode],
            )
            result: list[MaterializedScore] = []
            for item in ranked:
                key = canonical_opportunity_key(
                    item.group.group_id, item.group.representative.id
                )
                opportunity = by_key[key]
                result.append(
                    MaterializedScore(
                        opportunity=opportunity,
                        raw_score=item.raw_score,
                        public_match_score=item.match_score,
                    )
                )
            return result

        if not heartbeat(connection, assertion):
            return None
        generations: dict[int, int] = {}
        if assertion.mode_mask & MODE_WITHOUT_LEARNED_MASK:
            scores = score_mode(MODE_WITHOUT_LEARNED)
            if not heartbeat(connection, assertion):
                return None
            zero = materialize_generation(
                connection,
                learned_mode=MODE_WITHOUT_LEARNED,
                scores=scores,
                claim=assertion,
                authority_snapshot=claimed_authority,
            )
            generations[MODE_WITHOUT_LEARNED] = zero.id
        if not heartbeat(connection, assertion):
            return None
        if assertion.mode_mask & MODE_WITH_LEARNED_MASK:
            scores = score_mode(MODE_WITH_LEARNED)
            if not heartbeat(connection, assertion):
                return None
            one = materialize_generation(
                connection,
                learned_mode=MODE_WITH_LEARNED,
                scores=scores,
                claim=assertion,
                authority_snapshot=claimed_authority,
            )
            generations[MODE_WITH_LEARNED] = one.id
        if not activate(connection, assertion, None, generations):
            return None
        connection.commit()
        return RebuildResult(
            assertion.target_revision,
            assertion.mode_mask,
            tuple(generations[mode] for mode in sorted(generations)),
        )
    except Exception as error:
        failure_code = (
            "database"
            if isinstance(error, sqlite3.Error)
            else "validation"
            if isinstance(error, ValueError)
            else "unknown"
        )
        fail_claim(connection, assertion, failure_code)
        raise


def rebuild_database(path: Path) -> RebuildResult | None:
    """Run an audited local rebuild against a current database."""
    with connect_current_rw(path) as connection:
        return recover_and_rebuild(connection)
