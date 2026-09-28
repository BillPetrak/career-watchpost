"""M6 human-authority and immutable Career Profile service."""

# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal, cast

from careeros.career_profile.models import (
    CandidateProfileFactRequest,
    CareerProfileSnapshot,
    EffectiveCandidateReview,
    ManualProfileFactRequest,
)
from careeros.database.career_profile import (
    CareerProfileConflict,
    CareerProfileValidationError,
    ProfileFactInput,
    ProfileSaveResult,
    get_profile_state,
    list_profile_facts,
    save_profile,
    validate_profile_aggregate,
    validate_profile_snapshot,
)
from careeros.database.resume_intelligence import (
    append_fact_review_in_transaction,
    fact_review_history_records,
    fact_review_history_records_page,
    latest_fact_review_record,
    load_fact_authority,
    validate_recognized_fact_authority,
    validate_review_timestamp,
)
from careeros.resume_intelligence.career_facts import validate_candidate_value
from careeros.resume_intelligence.models import CareerFactReviewEvent

_PRIVACY: dict[str, Literal["public", "private", "sensitive"]] = {
    "identity.name": "sensitive",
    "contact.email": "sensitive",
    "contact.phone": "sensitive",
    "portfolio_reference": "private",
    "skill": "public",
    "employment": "private",
    "education": "private",
    "certification": "public",
    "language": "public",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _candidate(connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM career_fact_candidates WHERE id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise CareerProfileValidationError("fact candidate not found")
    return cast(sqlite3.Row, row)


def effective_candidate_review(
    connection: sqlite3.Connection, candidate_id: str
) -> EffectiveCandidateReview:
    candidate = _candidate(connection, candidate_id)
    event = latest_fact_review_record(connection, candidate_id)
    if event is None:
        return EffectiveCandidateReview(candidate_id, "pending")
    status = event.event_kind
    if status == "rejected":
        return EffectiveCandidateReview(
            candidate_id,
            "rejected",
            event_id=event.id,
            created_at=event.created_at,
        )
    if status == "approved":
        try:
            canonical, digest, key = validate_candidate_value(
                str(candidate["fact_type"]), str(candidate["canonical_value_json"])
            )
        except (TypeError, ValueError) as exc:
            raise CareerProfileValidationError(
                "approved candidate authority is corrupt"
            ) from exc
        if (
            canonical != str(candidate["canonical_value_json"])
            or digest != str(candidate["value_sha256"])
            or key != str(candidate["normalized_key"])
        ):
            raise CareerProfileValidationError(
                "approved candidate authority is corrupt"
            )
        return EffectiveCandidateReview(
            candidate_id,
            "approved",
            str(candidate["canonical_value_json"]),
            str(candidate["value_sha256"]),
            str(candidate["normalized_key"]),
            event.id,
            event.created_at,
        )
    value = str(event.edited_value_json)
    digest = str(event.edited_value_sha256)
    canonical, actual_digest, key = validate_candidate_value(
        str(candidate["fact_type"]), value
    )
    if canonical != value or digest != actual_digest:
        raise CareerProfileValidationError("approved edit authority is corrupt")
    return EffectiveCandidateReview(
        candidate_id,
        "approved_edit",
        value,
        digest,
        key,
        event.id,
        event.created_at,
    )


def candidate_review_history(
    connection: sqlite3.Connection, candidate_id: str, *, limit: int | None = None
) -> list[dict[str, Any]]:
    candidate = _candidate(connection, candidate_id)
    records = fact_review_history_records(connection, candidate_id, limit=limit)
    for row in records:
        if row.event_kind == "approved_edit":
            if row.edited_value_json is None or row.edited_value_sha256 is None:
                raise CareerProfileValidationError("approved edit authority is corrupt")
            try:
                canonical, digest, _ = validate_candidate_value(
                    str(candidate["fact_type"]), row.edited_value_json
                )
            except (TypeError, ValueError) as exc:
                raise CareerProfileValidationError(
                    "approved edit authority is corrupt"
                ) from exc
            if canonical != row.edited_value_json or digest != row.edited_value_sha256:
                raise CareerProfileValidationError("approved edit authority is corrupt")
    return [asdict(row) for row in records]


def candidate_review_history_page(
    connection: sqlite3.Connection,
    candidate_id: str,
    *,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Return one M6-validated review-history page.

    Integrity validation is deliberately limited to rows returned by this page;
    callers requiring complete-history certification should use
    :func:`candidate_review_history`.
    """
    if isinstance(offset, bool) or offset < 0:
        raise CareerProfileValidationError("offset must be non-negative")
    if isinstance(limit, bool) or limit < 1 or limit > 100:
        raise CareerProfileValidationError("limit must be between 1 and 100")
    candidate = _candidate(connection, candidate_id)
    records, total = fact_review_history_records_page(
        connection, candidate_id, offset=offset, limit=limit
    )
    for row in records:
        if row.event_kind not in ("approved", "rejected", "approved_edit"):
            raise CareerProfileValidationError("unrecognized review event")
        if row.event_kind == "approved_edit":
            if row.edited_value_json is None or row.edited_value_sha256 is None:
                raise CareerProfileValidationError("approved edit authority is corrupt")
            try:
                canonical, digest, _ = validate_candidate_value(
                    str(candidate["fact_type"]), row.edited_value_json
                )
            except (TypeError, ValueError) as exc:
                raise CareerProfileValidationError(
                    "approved edit authority is corrupt"
                ) from exc
            if canonical != row.edited_value_json or digest != row.edited_value_sha256:
                raise CareerProfileValidationError("approved edit authority is corrupt")
    return {
        "items": [asdict(row) for row in records],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def _edited_key(fact_type: str, value_json: str) -> str:
    return validate_candidate_value(fact_type, value_json)[2]


def _review_candidate(
    connection: sqlite3.Connection,
    candidate_id: str,
    decision: str,
    *,
    edited_value_json: str | None = None,
    note: str | None = None,
    timestamp: str,
) -> EffectiveCandidateReview:
    """Atomically apply a review transition or retry."""
    if decision not in ("approved", "rejected", "approved_edit"):
        raise CareerProfileValidationError("invalid review decision")
    candidate = _candidate(connection, candidate_id)
    # Every M6 decision is authority over an existing recognized M5 aggregate,
    # never a way to turn an arbitrary row into a candidate.  This expensive
    # validation deliberately occurs before the short review write lock.
    try:
        authority_digest = validate_recognized_fact_authority(
            connection, str(candidate["fact_run_id"])
        ).digest
    except Exception as exc:
        if isinstance(exc, CareerProfileValidationError):
            raise
        raise CareerProfileValidationError(
            "review requires a valid recognized M5 aggregate"
        ) from exc
    if decision == "approved_edit":
        if edited_value_json is None:
            raise CareerProfileValidationError("approved_edit requires a value")
        canonical, edited_hash, _ = validate_candidate_value(
            str(candidate["fact_type"]), edited_value_json
        )
        if canonical != edited_value_json:
            raise CareerProfileValidationError("edited value is not canonical")
    else:
        if edited_value_json is not None:
            raise CareerProfileValidationError(
                "edited value only valid for approved_edit"
            )
        edited_hash = None
    created_at = timestamp or _now()
    validate_review_timestamp(created_at)
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = effective_candidate_review(connection, candidate_id)
        if (
            load_fact_authority(connection, str(candidate["fact_run_id"])).digest
            != authority_digest
        ):
            raise CareerProfileConflict("M5 aggregate authority changed")
        semantic_same = current.status == decision and (
            decision != "approved_edit"
            or current.canonical_value_json == edited_value_json
        )
        if semantic_same and note is None:
            connection.commit()
            return current
        append_fact_review_in_transaction(
            connection,
            CareerFactReviewEvent(
                candidate_id,
                cast(Literal["approved", "rejected", "approved_edit"], decision),
                created_at,
                edited_value_json,
                edited_hash,
                note,
            ),
            validate_timestamp=True,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return effective_candidate_review(connection, candidate_id)


def review_candidate(
    connection: sqlite3.Connection,
    candidate_id: str,
    decision: str,
    *,
    edited_value_json: str | None = None,
    note: str | None = None,
) -> EffectiveCandidateReview:
    """Apply a review using the service-owned canonical UTC clock."""
    return _review_candidate(
        connection,
        candidate_id,
        decision,
        edited_value_json=edited_value_json,
        note=note,
        timestamp=_now(),
    )


def _preflight_digest(connection: sqlite3.Connection, fact_run_id: str) -> str:
    return validate_recognized_fact_authority(connection, fact_run_id).digest


def _locked_digest(connection: sqlite3.Connection, fact_run_id: str) -> str:
    return load_fact_authority(connection, fact_run_id).digest


def _validate_profile_source_authority(
    connection: sqlite3.Connection, source_candidate_ids: Sequence[str]
) -> None:
    """Require every candidate-backed snapshot source to remain M5-authoritative."""
    runs: dict[str, set[str]] = {}
    for candidate_id in source_candidate_ids:
        row = connection.execute(
            "SELECT fact_run_id FROM career_fact_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise CareerProfileValidationError(
                "profile source candidate does not exist"
            )
        runs.setdefault(str(row["fact_run_id"]), set()).add(candidate_id)
    for run_id, candidate_ids in runs.items():
        try:
            authority = validate_recognized_fact_authority(connection, run_id)
        except Exception as exc:
            if isinstance(exc, CareerProfileValidationError):
                raise
            raise CareerProfileValidationError(
                "profile source requires a valid recognized M5 aggregate"
            ) from exc
        if candidate_ids.difference(candidate.id for candidate in authority.candidates):
            raise CareerProfileValidationError(
                "profile source candidate is outside recognized M5 authority"
            )


def _resolve_identity(
    connection: sqlite3.Connection,
    request: CandidateProfileFactRequest,
    sources: tuple[str, ...],
    fact_type: str,
    normalized_key: str,
) -> str:
    mappings: dict[str, set[str]] = {}
    for candidate_id in sources:
        rows = connection.execute(
            "SELECT DISTINCT f.logical_fact_id FROM career_profile_facts f "
            "JOIN career_profile_fact_sources s ON s.profile_fact_id=f.id "
            "WHERE s.candidate_id=?",
            (candidate_id,),
        ).fetchall()
        mappings[candidate_id] = {str(row[0]) for row in rows}
    found = set().union(*mappings.values()) if mappings else set()
    if request.logical_fact_id is not None:
        if request.distinct_identity:
            raise CareerProfileValidationError(
                "distinct identity cannot target an existing logical fact"
            )
        if not connection.execute(
            "SELECT 1 FROM career_profile_facts "
            "WHERE logical_fact_id=? AND fact_type=? LIMIT 1",
            (request.logical_fact_id, fact_type),
        ).fetchone():
            raise CareerProfileValidationError(
                "explicit logical identity does not exist for fact type"
            )
        if any(ids and ids != {request.logical_fact_id} for ids in mappings.values()):
            raise CareerProfileValidationError(
                "candidate has conflicting historical identity"
            )
        return request.logical_fact_id
    if len(found) > 1:
        raise CareerProfileValidationError(
            "candidate has ambiguous historical logical identity"
        )
    if found and request.distinct_identity:
        raise CareerProfileValidationError(
            "distinct identity cannot reuse a historical candidate association"
        )
    if found:
        return next(iter(found))
    historical_equal = connection.execute(
        "SELECT 1 FROM career_profile_facts WHERE fact_type=? AND normalized_key=? LIMIT 1",
        (fact_type, normalized_key),
    ).fetchone()
    if historical_equal is not None and not request.distinct_identity:
        raise CareerProfileValidationError(
            "equal-key historical identity requires explicit target or distinct identity"
        )
    return "candidate:" + min(sources)


def assemble_profile(
    connection: sqlite3.Connection,
    expected_revision: int,
    entries: Sequence[CandidateProfileFactRequest | ManualProfileFactRequest],
    *,
    created_at: str | None = None,
) -> ProfileSaveResult:
    """Prepare and save a complete approved-facts snapshot with authority rechecks."""
    prepared_state = validate_profile_aggregate(connection)
    if expected_revision != prepared_state.revision:
        raise CareerProfileConflict(
            "career profile revision is stale",
            current_revision=prepared_state.revision,
        )
    prepared: list[ProfileFactInput] = []
    digests: dict[str, str] = {}
    authority_candidate_ids: dict[str, set[str]] = {}
    used: set[str] = set()
    for entry in entries:
        if isinstance(entry, ManualProfileFactRequest):
            state = get_profile_state(connection)
            current = (
                next(
                    (
                        f
                        for f in list_profile_facts(
                            connection, state.current_profile_version_id
                        )
                        if f.logical_fact_id == entry.logical_fact_id
                    ),
                    None,
                )
                if state.current_profile_version_id
                else None
            )
            if current is None or current.origin not in ("manual", "imported"):
                raise CareerProfileValidationError(
                    "manual/imported facts must be carried from current profile"
                )
            if entry.privacy_override not in (None, "public", "private", "sensitive"):
                raise CareerProfileValidationError("invalid privacy override")
            privacy = entry.privacy_override or cast(
                Literal["public", "private", "sensitive"],
                current.privacy_classification,
            )
            prepared.append(
                ProfileFactInput(
                    current.logical_fact_id,
                    current.fact_type,
                    current.normalized_key,
                    current.canonical_value_json,
                    current.origin,
                    privacy,
                    len(prepared),
                    (),
                    current.value_sha256,
                )
            )
            continue
        if entry.primary_candidate_id in used or any(
            s in used for s in entry.supporting_candidate_ids
        ):
            raise CareerProfileValidationError(
                "candidate cannot source two profile facts"
            )
        if (
            len(set(entry.supporting_candidate_ids))
            != len(entry.supporting_candidate_ids)
            or entry.primary_candidate_id in entry.supporting_candidate_ids
        ):
            raise CareerProfileValidationError("duplicate profile source candidate")
        if entry.privacy_override not in (None, "public", "private", "sensitive"):
            raise CareerProfileValidationError("invalid privacy override")
        sources = tuple(
            sorted((entry.primary_candidate_id, *entry.supporting_candidate_ids))
        )
        reviews = [
            effective_candidate_review(connection, candidate_id)
            for candidate_id in sources
        ]
        if any(
            review.status not in ("approved", "approved_edit") for review in reviews
        ):
            raise CareerProfileValidationError("all profile sources must be approved")
        rows = [_candidate(connection, candidate_id) for candidate_id in sources]
        if (
            len({str(row["fact_type"]) for row in rows}) != 1
            or len({review.normalized_key for review in reviews}) != 1
        ):
            raise CareerProfileValidationError(
                "profile sources are not merge-compatible"
            )
        sources_by_run: dict[str, set[str]] = {}
        for candidate_id, row in zip(sources, rows, strict=True):
            run_id = str(row["fact_run_id"])
            sources_by_run.setdefault(run_id, set()).add(candidate_id)
        for run_id, run_source_ids in sources_by_run.items():
            if run_id not in digests:
                try:
                    digests[run_id] = _preflight_digest(connection, run_id)
                    authority = load_fact_authority(connection, run_id)
                except Exception as exc:
                    if isinstance(exc, CareerProfileValidationError):
                        raise
                    raise CareerProfileValidationError(
                        "profile source requires a valid recognized M5 aggregate"
                    ) from exc
                authority_candidate_ids[run_id] = {
                    candidate.id for candidate in authority.candidates
                }
            if run_source_ids.difference(authority_candidate_ids[run_id]):
                raise CareerProfileValidationError(
                    "profile source candidate is outside recognized M5 authority"
                )
        primary_index = sources.index(entry.primary_candidate_id)
        primary = reviews[primary_index]
        primary_row = rows[primary_index]
        fact_type = str(primary_row["fact_type"])
        if fact_type not in _PRIVACY:
            raise CareerProfileValidationError("unknown profile fact type")
        logical_id = _resolve_identity(
            connection, entry, sources, fact_type, str(primary.normalized_key)
        )
        privacy = entry.privacy_override or _PRIVACY[fact_type]
        prepared.append(
            ProfileFactInput(
                logical_id,
                fact_type,
                str(primary.normalized_key),
                str(primary.canonical_value_json),
                "approved_candidate",
                privacy,
                len(prepared),
                sources,
                primary.value_sha256,
            )
        )
        used.update(sources)
    # Save via the generic atomic writer, but recheck authority immediately before it starts.
    for run_id, digest in digests.items():
        if _locked_digest(connection, run_id) != digest:
            raise CareerProfileConflict("M5 aggregate authority changed")
    review_tokens = {
        candidate_id: effective_candidate_review(connection, candidate_id)
        for candidate_id in used
    }

    def recheck(c: sqlite3.Connection) -> None:
        state_row = c.execute(
            "SELECT current_profile_version_id, revision, next_version_number "
            "FROM career_profile_state WHERE singleton=1"
        ).fetchone()
        if state_row is None:
            raise CareerProfileValidationError("career profile state not found")
        observed_state = (
            state_row["current_profile_version_id"],
            int(state_row["revision"]),
            int(state_row["next_version_number"]),
        )
        prepared_tuple = (
            prepared_state.current_profile_version_id,
            prepared_state.revision,
            prepared_state.next_version_number,
        )
        if observed_state != prepared_tuple:
            raise CareerProfileConflict("prepared profile state authority changed")
        for run_id, digest in digests.items():
            if _locked_digest(c, run_id) != digest:
                raise CareerProfileConflict("M5 aggregate authority changed")
        for candidate_id, token in review_tokens.items():
            current = effective_candidate_review(c, candidate_id)
            if (
                current.status,
                current.event_id,
                current.created_at,
                current.canonical_value_json,
                current.value_sha256,
                current.normalized_key,
            ) != (
                token.status,
                token.event_id,
                token.created_at,
                token.canonical_value_json,
                token.value_sha256,
                token.normalized_key,
            ):
                raise CareerProfileConflict("candidate review authority changed")

    def validate_before_commit(c: sqlite3.Connection) -> None:
        validate_profile_aggregate(c)

    result = save_profile(
        connection,
        expected_revision,
        prepared,
        origin="approved_facts",
        created_at=created_at,
        before_write=recheck,
        before_commit=validate_before_commit,
    )
    return result


def current_profile(connection: sqlite3.Connection) -> CareerProfileSnapshot | None:
    validate_profile_aggregate(connection)
    state = get_profile_state(connection)
    if state.current_profile_version_id is None:
        return None
    version, facts, sources = validate_profile_snapshot(
        connection, state.current_profile_version_id
    )
    for source_ids in sources:
        _validate_profile_source_authority(connection, source_ids)
    return CareerProfileSnapshot(version, facts, sources)


def profile_snapshot(
    connection: sqlite3.Connection, version_id: str
) -> CareerProfileSnapshot:
    """Return one fully validated immutable profile version."""
    validate_profile_aggregate(connection)
    version, facts, sources = validate_profile_snapshot(connection, version_id)
    for source_ids in sources:
        _validate_profile_source_authority(connection, source_ids)
    return CareerProfileSnapshot(version, facts, sources)


def profile_history(
    connection: sqlite3.Connection, *, limit: int | None = None
) -> list[CareerProfileSnapshot]:
    validate_profile_aggregate(connection)
    if limit is None:
        rows = connection.execute(
            "SELECT id FROM career_profile_versions ORDER BY version_number DESC"
        ).fetchall()
    else:
        if limit <= 0:
            raise CareerProfileValidationError("history limit must be positive")
        rows = connection.execute(
            "SELECT id FROM career_profile_versions ORDER BY version_number DESC LIMIT ?",
            (limit,),
        ).fetchall()
    snapshots: list[CareerProfileSnapshot] = []
    for row in rows:
        version, facts, sources = validate_profile_snapshot(connection, str(row[0]))
        for source_ids in sources:
            _validate_profile_source_authority(connection, source_ids)
        snapshots.append(CareerProfileSnapshot(version, facts, sources))
    return snapshots
