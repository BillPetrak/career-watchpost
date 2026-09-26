"""Immutable Career Profile persistence and authority operations.

The caller supplies already validated facts.  This module deliberately does
not perform extraction, model work, or material access; it only validates and
atomically persists the authority snapshot represented by schema v13.
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn, cast

from careeros.career_profile.models import (
    CareerProfileFact,
    CareerProfileFactSource,
    CareerProfileState,
    CareerProfileVersion,
    ProfileCreationOrigin,
    ProfileFactOrigin,
)


class CareerProfileError(ValueError):
    """Base class for privacy-safe profile repository failures."""


class CareerProfileNotFound(CareerProfileError):
    pass


class CareerProfileValidationError(CareerProfileError):
    pass


class CareerProfileConflict(CareerProfileError):
    """The caller's observed profile revision is stale."""

    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class ProfileFactInput:
    """One desired fact in a new complete profile snapshot."""

    logical_fact_id: str
    fact_type: str
    normalized_key: str
    canonical_value_json: str
    origin: ProfileFactOrigin
    privacy_classification: Literal["public", "private", "sensitive"]
    sort_order: int
    source_candidate_ids: tuple[str, ...] = ()
    value_sha256: str | None = None


@dataclass(frozen=True)
class ProfileSaveResult:
    version: CareerProfileVersion
    facts: tuple[CareerProfileFact, ...]
    state: CareerProfileState


def _row_state(row: sqlite3.Row) -> CareerProfileState:
    return CareerProfileState(
        current_profile_version_id=row["current_profile_version_id"],
        revision=int(row["revision"]),
        next_version_number=int(row["next_version_number"]),
    )


def get_profile_state(connection: sqlite3.Connection) -> CareerProfileState:
    row = connection.execute(
        "SELECT current_profile_version_id, revision, next_version_number "
        "FROM career_profile_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise CareerProfileNotFound("career profile state not found")
    return _row_state(row)


def _version(row: sqlite3.Row) -> CareerProfileVersion:
    return CareerProfileVersion(
        id=str(row["id"]),
        version_number=int(row["version_number"]),
        parent_version_id=row["parent_version_id"],
        content_fingerprint=str(row["content_fingerprint"]),
        origin=str(row["origin"]),  # type: ignore[arg-type]
        created_at=str(row["created_at"]),
    )


def get_profile_version(
    connection: sqlite3.Connection, version_id: str
) -> CareerProfileVersion:
    row = connection.execute(
        "SELECT * FROM career_profile_versions WHERE id=?", (version_id,)
    ).fetchone()
    if row is None:
        raise CareerProfileNotFound("career profile version not found")
    return _version(row)


def get_current_profile_version(
    connection: sqlite3.Connection,
) -> CareerProfileVersion | None:
    state = get_profile_state(connection)
    if state.current_profile_version_id is None:
        return None
    return get_profile_version(connection, state.current_profile_version_id)


def get_profile_version_by_number(
    connection: sqlite3.Connection, version_number: int
) -> CareerProfileVersion:
    row = connection.execute(
        "SELECT * FROM career_profile_versions WHERE version_number=?",
        (version_number,),
    ).fetchone()
    if row is None:
        raise CareerProfileNotFound("career profile version not found")
    return _version(row)


def list_profile_versions(
    connection: sqlite3.Connection, *, limit: int = 100
) -> list[CareerProfileVersion]:
    sql = "SELECT * FROM career_profile_versions ORDER BY version_number DESC"
    args: tuple[int, ...] = ()
    if limit <= 0 or limit > 1000:
        raise CareerProfileValidationError(
            "profile history limit must be between 1 and 1000"
        )
    sql += " LIMIT ?"
    args = (limit,)
    return [_version(row) for row in connection.execute(sql, args)]


def _fact(row: sqlite3.Row) -> CareerProfileFact:
    return CareerProfileFact(
        id=str(row["id"]),
        profile_version_id=str(row["profile_version_id"]),
        logical_fact_id=str(row["logical_fact_id"]),
        fact_type=str(row["fact_type"]),
        normalized_key=str(row["normalized_key"]),
        canonical_value_json=str(row["canonical_value_json"]),
        value_sha256=str(row["value_sha256"]),
        origin=str(row["origin"]),  # type: ignore[arg-type]
        privacy_classification=str(row["privacy_classification"]),
        sort_order=int(row["sort_order"]),
    )


def list_profile_facts(
    connection: sqlite3.Connection, version_id: str
) -> list[CareerProfileFact]:
    if (
        connection.execute(
            "SELECT 1 FROM career_profile_versions WHERE id=?", (version_id,)
        ).fetchone()
        is None
    ):
        raise CareerProfileNotFound("career profile version not found")
    return [
        _fact(row)
        for row in connection.execute(
            "SELECT * FROM career_profile_facts "
            "WHERE profile_version_id=? ORDER BY sort_order, id",
            (version_id,),
        )
    ]


def list_fact_sources(
    connection: sqlite3.Connection, profile_fact_id: str
) -> list[CareerProfileFactSource]:
    if (
        connection.execute(
            "SELECT 1 FROM career_profile_facts WHERE id=?", (profile_fact_id,)
        ).fetchone()
        is None
    ):
        raise CareerProfileNotFound("career profile fact not found")
    return [
        CareerProfileFactSource(str(row["profile_fact_id"]), str(row["candidate_id"]))
        for row in connection.execute(
            "SELECT profile_fact_id, candidate_id FROM career_profile_fact_sources "
            "WHERE profile_fact_id=? ORDER BY candidate_id",
            (profile_fact_id,),
        )
    ]


def validate_profile_snapshot(
    connection: sqlite3.Connection, version_id: str
) -> tuple[
    CareerProfileVersion, tuple[CareerProfileFact, ...], tuple[tuple[str, ...], ...]
]:
    """Validate one immutable profile snapshot without repairing persisted rows."""
    version = get_profile_version(connection, version_id)
    if version.version_number == 1:
        if version.parent_version_id is not None:
            raise CareerProfileValidationError("version one must not have a parent")
    elif version.parent_version_id is None:
        raise CareerProfileValidationError("profile version parent is missing")
    elif (
        connection.execute(
            "SELECT 1 FROM career_profile_versions WHERE id=?",
            (version.parent_version_id,),
        ).fetchone()
        is None
    ):
        raise CareerProfileValidationError("profile version parent does not exist")
    rows = connection.execute(
        "SELECT * FROM career_profile_facts WHERE profile_version_id=? ORDER BY sort_order, id",
        (version_id,),
    ).fetchall()
    # Ordering in the database is only for inspection; reject any malformed persisted order.
    if [int(row["sort_order"]) for row in rows] != list(range(len(rows))):
        raise CareerProfileValidationError(
            "profile fact sort orders are not contiguous"
        )
    inputs: list[ProfileFactInput] = []
    source_sets: list[tuple[str, ...]] = []
    for row in rows:
        fact = _fact(row)
        sources = tuple(
            str(source["candidate_id"])
            for source in connection.execute(
                "SELECT candidate_id FROM career_profile_fact_sources WHERE profile_fact_id=? ORDER BY candidate_id",
                (fact.id,),
            )
        )
        persisted = tuple(
            str(source["candidate_id"])
            for source in connection.execute(
                "SELECT candidate_id FROM career_profile_fact_sources WHERE profile_fact_id=?",
                (fact.id,),
            )
        )
        if persisted != sources or len(set(sources)) != len(sources):
            raise CareerProfileValidationError(
                "profile sources are not sorted and unique"
            )
        if fact.origin in ("manual", "imported") and sources:
            raise CareerProfileValidationError(
                "manual/imported facts cannot have sources"
            )
        if fact.origin == "approved_candidate" and not sources:
            raise CareerProfileValidationError("approved candidate fact has no sources")
        if sources:
            from careeros.resume_intelligence.career_facts import (
                validate_candidate_value,
            )

            for candidate_id in sources:
                candidate = connection.execute(
                    "SELECT fact_type,normalized_key,canonical_value_json,value_sha256 "
                    "FROM career_fact_candidates WHERE id=?",
                    (candidate_id,),
                ).fetchone()
                if candidate is None:
                    raise CareerProfileValidationError(
                        "profile source candidate does not exist"
                    )
                if str(candidate["fact_type"]) != fact.fact_type:
                    raise CareerProfileValidationError(
                        "profile source fact type is incompatible"
                    )
                try:
                    _, digest, key = validate_candidate_value(
                        fact.fact_type, fact.canonical_value_json
                    )
                except (TypeError, ValueError) as exc:
                    raise CareerProfileValidationError(
                        "candidate-backed profile value is invalid"
                    ) from exc
                if digest != fact.value_sha256 or key != fact.normalized_key:
                    raise CareerProfileValidationError(
                        "candidate-backed profile value/key is invalid"
                    )
                # The immutable snapshot stores the effective review value.
                # An approved_edit may intentionally have a different key
                # from the immutable candidate row; historical validation
                # must not reinterpret that snapshot through today's source
                # candidate authority.
        inputs.append(
            ProfileFactInput(
                fact.logical_fact_id,
                fact.fact_type,
                fact.normalized_key,
                fact.canonical_value_json,
                fact.origin,
                cast(
                    Literal["public", "private", "sensitive"],
                    fact.privacy_classification,
                ),
                fact.sort_order,
                sources,
                fact.value_sha256,
            )
        )
        source_sets.append(sources)
    _normalise_inputs(inputs)
    if _fingerprint(inputs) != version.content_fingerprint:
        raise CareerProfileValidationError("profile content fingerprint mismatch")
    return version, tuple(_fact(row) for row in rows), tuple(source_sets)


def validate_profile_aggregate(connection: sqlite3.Connection) -> CareerProfileState:
    """Validate singleton state, version chain, and all historical snapshots."""
    state = get_profile_state(connection)
    versions = [
        _version(row)
        for row in connection.execute(
            "SELECT * FROM career_profile_versions ORDER BY version_number"
        )
    ]
    if state.current_profile_version_id is None:
        if state.revision != 0 or state.next_version_number != 1 or versions:
            raise CareerProfileValidationError("invalid initial profile state")
        return state
    if state.revision != state.next_version_number - 1:
        raise CareerProfileValidationError("profile revision/version invariant failed")
    if not versions or versions[-1].version_number != state.revision:
        raise CareerProfileValidationError("profile version chain is incomplete")
    expected_parent: str | None = None
    for expected_number, version in enumerate(versions, 1):
        if (
            version.version_number != expected_number
            or version.parent_version_id != expected_parent
        ):
            raise CareerProfileValidationError(
                "profile version parent chain is invalid"
            )
        validate_profile_snapshot(connection, version.id)
        expected_parent = version.id
    if state.current_profile_version_id != versions[-1].id:
        raise CareerProfileValidationError("current profile version is not latest")
    return state


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant: {value}")


def _canonical_json(value: str) -> str:
    try:
        parsed = json.loads(value, parse_constant=_reject_json_constant)
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CareerProfileValidationError(
            "canonical fact value is invalid JSON"
        ) from exc
    if canonical != value:
        raise CareerProfileValidationError("canonical fact value is not canonical JSON")
    return canonical


def _normalise_inputs(facts: Iterable[ProfileFactInput]) -> list[ProfileFactInput]:
    result: list[ProfileFactInput] = []
    for item in facts:
        result.append(item)
    result.sort(key=lambda fact: fact.sort_order)
    if [fact.sort_order for fact in result] != list(range(len(result))):
        raise CareerProfileValidationError(
            "profile fact sort orders must be contiguous"
        )
    seen: set[str] = set()
    seen_sources: set[str] = set()
    for fact in result:
        if fact.logical_fact_id in seen:
            raise CareerProfileValidationError("profile logical facts must be unique")
        seen.add(fact.logical_fact_id)
        _canonical_json(fact.canonical_value_json)
        expected_hash = hashlib.sha256(
            fact.canonical_value_json.encode("utf-8")
        ).hexdigest()
        if fact.value_sha256 is not None and fact.value_sha256 != expected_hash:
            raise CareerProfileValidationError("profile fact hash does not match value")
        if fact.origin == "approved_candidate" and not fact.source_candidate_ids:
            raise CareerProfileValidationError(
                "approved candidate facts require sources"
            )
        if fact.origin in ("manual", "imported") and fact.source_candidate_ids:
            raise CareerProfileValidationError(
                "manual/imported facts cannot have sources"
            )
        if fact.origin not in ("manual", "approved_candidate", "imported"):
            raise CareerProfileValidationError("invalid profile fact origin")
        if fact.privacy_classification not in ("public", "private", "sensitive"):
            raise CareerProfileValidationError("invalid profile privacy classification")
        source_ids = tuple(fact.source_candidate_ids)
        if tuple(sorted(set(source_ids))) != source_ids:
            raise CareerProfileValidationError(
                "profile source candidates must be sorted and unique"
            )
        overlap = seen_sources.intersection(source_ids)
        if overlap:
            raise CareerProfileValidationError(
                "a candidate cannot source two profile facts"
            )
        seen_sources.update(source_ids)
    return result


def _fingerprint(facts: list[ProfileFactInput]) -> str:
    semantic = [
        {
            "logical_fact_id": f.logical_fact_id,
            "fact_type": f.fact_type,
            "normalized_key": f.normalized_key,
            "canonical_value_json": f.canonical_value_json,
            "origin": f.origin,
            "privacy_classification": f.privacy_classification,
            "sort_order": f.sort_order,
            "source_candidate_ids": sorted(f.source_candidate_ids),
        }
        for f in facts
    ]
    encoded = json.dumps(
        semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def save_profile(
    connection: sqlite3.Connection,
    expected_revision: int,
    facts: Iterable[ProfileFactInput],
    *,
    origin: str = "manual",
    created_at: str | None = None,
    before_write: Callable[[sqlite3.Connection], None] | None = None,
    before_commit: Callable[[sqlite3.Connection], None] | None = None,
) -> ProfileSaveResult:
    """Atomically create one immutable snapshot and advance profile authority."""
    desired = _normalise_inputs(facts)
    if origin not in ("manual", "approved_facts", "imported"):
        raise CareerProfileValidationError("invalid profile creation origin")
    if expected_revision < 0:
        raise CareerProfileValidationError("expected revision must be non-negative")
    timestamp = created_at or datetime.now(UTC).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT current_profile_version_id, revision, next_version_number "
            "FROM career_profile_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise CareerProfileNotFound("career profile state not found")
        state = _row_state(row)
        if state.revision != expected_revision:
            raise CareerProfileConflict(
                "career profile revision is stale", current_revision=state.revision
            )
        if before_write is not None:
            before_write(connection)
        candidate_ids = sorted(
            {cid for fact in desired for cid in fact.source_candidate_ids}
        )
        for candidate_id in candidate_ids:
            if (
                connection.execute(
                    "SELECT 1 FROM career_fact_candidates WHERE id=?", (candidate_id,)
                ).fetchone()
                is None
            ):
                raise CareerProfileValidationError("profile source candidate not found")
        version_id = secrets.token_hex(16)
        connection.execute(
            "INSERT INTO career_profile_versions("
            "id,version_number,parent_version_id,content_fingerprint,origin,"
            "created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                version_id,
                state.next_version_number,
                state.current_profile_version_id,
                _fingerprint(desired),
                origin,
                timestamp,
            ),
        )
        saved: list[CareerProfileFact] = []
        for item in desired:
            fact_id = secrets.token_hex(16)
            value_hash = hashlib.sha256(
                item.canonical_value_json.encode("utf-8")
            ).hexdigest()
            connection.execute(
                "INSERT INTO career_profile_facts("
                "id,profile_version_id,logical_fact_id,fact_type,normalized_key,"
                "canonical_value_json,value_sha256,origin,privacy_classification,"
                "sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    fact_id,
                    version_id,
                    item.logical_fact_id,
                    item.fact_type,
                    item.normalized_key,
                    item.canonical_value_json,
                    value_hash,
                    item.origin,
                    item.privacy_classification,
                    item.sort_order,
                ),
            )
            for candidate_id in item.source_candidate_ids:
                connection.execute(
                    "INSERT INTO career_profile_fact_sources("
                    "profile_fact_id,candidate_id) VALUES (?,?)",
                    (fact_id, candidate_id),
                )
            saved.append(
                CareerProfileFact(
                    fact_id,
                    version_id,
                    item.logical_fact_id,
                    item.fact_type,
                    item.normalized_key,
                    item.canonical_value_json,
                    value_hash,
                    item.origin,
                    item.privacy_classification,
                    item.sort_order,
                )
            )
        updated = connection.execute(
            "UPDATE career_profile_state SET current_profile_version_id=?, "
            "revision=?, next_version_number=? "
            "WHERE singleton=1 AND revision=? AND next_version_number=?",
            (
                version_id,
                state.revision + 1,
                state.next_version_number + 1,
                expected_revision,
                state.next_version_number,
            ),
        )
        if updated.rowcount != 1:
            current = connection.execute(
                "SELECT revision FROM career_profile_state WHERE singleton=1"
            ).fetchone()
            raise CareerProfileConflict(
                "career profile revision is stale",
                current_revision=None if current is None else int(current[0]),
            )
        if before_commit is not None:
            before_commit(connection)
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise CareerProfileValidationError(
            "profile save integrity validation failed"
        ) from exc
    except Exception:
        connection.rollback()
        raise
    version = CareerProfileVersion(
        version_id,
        state.next_version_number,
        state.current_profile_version_id,
        _fingerprint(desired),
        cast(ProfileCreationOrigin, origin),
        timestamp,
    )
    return ProfileSaveResult(
        version,
        tuple(saved),
        CareerProfileState(
            version_id, state.revision + 1, state.next_version_number + 1
        ),
    )
