# ruff: noqa: E501
"""Presentation-safe orchestration for the private Career Profile workspace.

This module intentionally contains no Career Fact or Career Profile authority.
It validates through M5/M6 first, then builds bounded projections for CLI/API
callers.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from careeros.career_profile.models import (
    CandidateProfileFactRequest,
    ManualProfileFactRequest,
)
from careeros.career_profile.service import (
    candidate_review_history_page,
    current_profile,
    effective_candidate_review,
    effective_candidate_review_at,
    profile_snapshot,
)
from careeros.database.materials import material_row, version_rows
from careeros.database.resume_intelligence import (
    fact_evidence,
    validate_recognized_fact_authority,
)
from careeros.resume_intelligence.career_facts import run_deterministic_facts
from careeros.resume_intelligence.coordinates import validate_slice_hash
from careeros.resume_intelligence.extraction import extract_material
from careeros.resume_intelligence.models import CareerFactCandidate, ResumeExtractionRun

PAGE_MAX = 100
MERGE_HINT_MAX = 100
_DOCX_PART = re.compile(r"word/(?:document|header[1-9][0-9]*|footer[1-9][0-9]*)[.]xml")


class WorkspaceError(ValueError):
    """Safe presentation-layer failure."""


class WorkspaceRequestError(WorkspaceError):
    """The caller supplied an invalid bounded presentation request."""


class WorkspaceNotFoundError(WorkspaceError):
    """The requested presentation resource does not exist."""


class WorkspaceAuthorityError(WorkspaceError):
    """Persisted closed authority could not be projected safely."""


def page_args(offset: int, limit: int) -> tuple[int, int]:
    if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0:
        raise WorkspaceRequestError("offset must be non-negative")
    if limit <= 0 or limit > PAGE_MAX:
        raise WorkspaceRequestError("limit must be between 1 and 100")
    return offset, limit


def _page(items: list[dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "offset": offset,
        "limit": limit,
    }


def _safe_locator(locator: str) -> str:
    try:
        parsed = json.loads(locator)
    except (TypeError, ValueError) as exc:
        raise WorkspaceAuthorityError("source locator is unavailable") from exc
    if not isinstance(parsed, dict):
        raise WorkspaceAuthorityError("source locator is unavailable")
    if parsed.get("type") == "pdf_page":
        if set(parsed) != {"type", "page"}:
            raise WorkspaceAuthorityError("source locator is unavailable")
        page = parsed.get("page")
        if isinstance(page, int) and not isinstance(page, bool) and page > 0:
            return f"PDF page {page}"
        raise WorkspaceAuthorityError("source locator is unavailable")
    if parsed.get("type") == "docx_block":
        kind = parsed.get("kind")
        index = parsed.get("index")
        part = parsed.get("part")
        if (
            set(parsed) == {"type", "part", "kind", "index"}
            and isinstance(part, str)
            and _DOCX_PART.fullmatch(part) is not None
            and isinstance(kind, str)
            and kind in {"paragraph", "table"}
            and isinstance(index, int)
            and not isinstance(index, bool)
            and index >= 0
        ):
            return f"DOCX {kind} {index + 1}"
    raise WorkspaceAuthorityError("source locator is unavailable")


def _candidate_value(candidate: CareerFactCandidate) -> Any:
    try:
        return json.loads(candidate.canonical_value_json)
    except (TypeError, ValueError) as exc:
        raise WorkspaceAuthorityError("candidate authority is unavailable") from exc


def _candidate_projection(
    connection: sqlite3.Connection,
    candidate: CareerFactCandidate,
    all_candidates: tuple[CareerFactCandidate, ...],
) -> dict[str, Any]:
    review = effective_candidate_review(connection, candidate.id)
    merge_ids: list[str] = []
    if review.status in ("approved", "approved_edit") and review.normalized_key:
        for other in all_candidates:
            if other.id == candidate.id or other.fact_type != candidate.fact_type:
                continue
            other_review = effective_candidate_review(connection, other.id)
            if (
                other_review.status in ("approved", "approved_edit")
                and other_review.normalized_key == review.normalized_key
            ):
                merge_ids.append(other.id)
    return {
        "id": candidate.id,
        "fact_run_id": candidate.fact_run_id,
        "fact_type": candidate.fact_type,
        "value": _candidate_value(candidate),
        "original_value": _candidate_value(candidate),
        "confidence": candidate.confidence,
        "origin": candidate.origin,
        "review": {
            "status": review.status,
            "value": json.loads(review.canonical_value_json)
            if review.canonical_value_json
            else None,
            "event_id": review.event_id,
            "created_at": review.created_at,
        },
        "merge_eligible_candidate_ids": sorted(merge_ids),
        "merge_eligible_count": len(merge_ids),
    }


def _approved_recognized_candidates(
    connection: sqlite3.Connection, fact_types: tuple[str, ...]
) -> tuple[CareerFactCandidate, ...]:
    """Validate only relevant reviewed runs before computing cross-run hints."""
    if not fact_types:
        return ()
    placeholders = ",".join("?" for _ in fact_types)
    rows = connection.execute(
        "SELECT c.id,c.fact_run_id FROM career_fact_candidates c "
        "JOIN career_fact_review_events e ON e.id=("
        "SELECT e2.id FROM career_fact_review_events e2 "
        "WHERE e2.candidate_id=c.id ORDER BY e2.created_at DESC,e2.id DESC LIMIT 1"
        ") WHERE c.fact_type IN ("
        + placeholders
        + ") AND e.event_kind IN ('approved','approved_edit') "
        "ORDER BY c.fact_run_id,c.id LIMIT ?",
        fact_types + (MERGE_HINT_MAX,),
    ).fetchall()
    candidates_by_run: dict[str, set[str]] = {}
    for row in rows:
        candidates_by_run.setdefault(str(row["fact_run_id"]), set()).add(str(row["id"]))
    result: list[CareerFactCandidate] = []
    for run_id, candidate_ids in candidates_by_run.items():
        try:
            authority = validate_recognized_fact_authority(connection, run_id)
        except Exception:
            continue
        result.extend(
            candidate
            for candidate in authority.candidates
            if candidate.id in candidate_ids
        )
    return tuple(result)


def validate_review_candidate_authority(
    connection: sqlite3.Connection, candidate_id: str
) -> None:
    """Require one review target to belong to complete recognized M5 authority."""
    row = connection.execute(
        "SELECT fact_run_id FROM career_fact_candidates WHERE id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceNotFoundError("fact candidate not found")
    authority = validate_recognized_fact_authority(connection, str(row["fact_run_id"]))
    if not any(candidate.id == candidate_id for candidate in authority.candidates):
        raise WorkspaceNotFoundError("fact candidate not found")


def sources(
    connection: sqlite3.Connection,
    *,
    include_retired: bool = True,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    page_args(offset, limit)
    where = "" if include_retired else "WHERE m.status='active'"
    total = int(
        connection.execute(
            f"SELECT count(*) FROM materials m {where}"
            f"{' AND' if where else 'WHERE'} m.category='resume'"
        ).fetchone()[0]
    )
    rows = connection.execute(
        "SELECT id FROM materials m "
        f"{where}{' AND' if where else 'WHERE'} m.category='resume' "
        "ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for material_id_row in rows:
        row = connection.execute(
            "SELECT id,name,status FROM materials WHERE id=?",
            (material_id_row["id"],),
        ).fetchone()
        if row is None:
            raise WorkspaceAuthorityError("material authority is unavailable")
        versions: list[dict[str, Any]] = []
        for version in version_rows(connection, str(row["id"])):
            extraction_rows = connection.execute(
                "SELECT * FROM resume_extraction_runs "
                "WHERE material_version_id=? ORDER BY created_at DESC,id DESC",
                (version["id"],),
            ).fetchall()
            extractions = [
                ResumeExtractionRun(**dict(item)) for item in extraction_rows
            ]
            versions.append(
                {
                    "id": str(version["id"]),
                    "version_number": int(version["version_number"]),
                    "display_filename": str(version["display_filename"]),
                    "media_type": str(version["declared_media_type"]),
                    "created_at": str(version["created_at"]),
                    "extractions": [
                        {
                            "id": extraction.id,
                            "outcome": extraction.outcome,
                            "source_format": extraction.source_format,
                            "created_at": extraction.created_at,
                            "fact_runs": [],
                        }
                        for extraction in extractions
                    ],
                }
            )
            for item, extraction in zip(
                versions[-1]["extractions"], extractions, strict=True
            ):
                fact_runs: list[dict[str, Any]] = []
                for run in connection.execute(
                    "SELECT * FROM career_fact_runs WHERE extraction_run_id=? "
                    "ORDER BY created_at DESC,id DESC",
                    (extraction.id,),
                ).fetchall():
                    recognized = False
                    candidate_count: int | None = None
                    if (
                        run["outcome"] == "succeeded"
                        and run["mode"] == "deterministic"
                        and run["input_kind"] == "extracted"
                    ):
                        try:
                            authority = validate_recognized_fact_authority(
                                connection, str(run["id"])
                            )
                        except Exception:
                            pass
                        else:
                            recognized = True
                            candidate_count = len(authority.candidates)
                    fact_runs.append(
                        {
                            "id": str(run["id"]),
                            "outcome": str(run["outcome"]),
                            "mode": str(run["mode"]),
                            "input_kind": str(run["input_kind"]),
                            "created_at": str(run["created_at"]),
                            "recognized": recognized,
                            "candidate_count": candidate_count,
                        }
                    )
                item["fact_runs"] = fact_runs
        result.append(
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "status": str(row["status"]),
                "versions": versions,
            }
        )
    return {
        "items": result,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


def analyze(
    connection: sqlite3.Connection,
    materials_root: Path,
    material_id: str,
    version_id: str,
) -> dict[str, Any]:
    material = material_row(connection, material_id)
    if material is None or material["category"] != "resume":
        raise WorkspaceNotFoundError("resume material not found")
    version = connection.execute(
        "SELECT id FROM material_versions WHERE id=? AND material_id=?",
        (version_id, material_id),
    ).fetchone()
    if version is None:
        raise WorkspaceNotFoundError("resume version not found")
    extraction = extract_material(connection, materials_root, material_id, version_id)
    fact_run, candidates, _evidence = run_deterministic_facts(connection, extraction.id)
    validate_recognized_fact_authority(connection, fact_run.id)
    return {
        "material_id": material_id,
        "version_id": version_id,
        "extraction_run_id": extraction.id,
        "fact_run_id": fact_run.id,
        "candidate_count": len(candidates),
        "outcome": fact_run.outcome,
    }


def candidates(
    connection: sqlite3.Connection,
    fact_run_id: str,
    *,
    offset: int = 0,
    limit: int = 20,
    states: tuple[str, ...] = (),
) -> dict[str, Any]:
    page_args(offset, limit)
    authority = validate_recognized_fact_authority(connection, fact_run_id)
    all_candidates = _approved_recognized_candidates(
        connection,
        tuple(sorted({candidate.fact_type for candidate in authority.candidates})),
    )
    items = [
        _candidate_projection(connection, candidate, all_candidates)
        for candidate in authority.candidates
    ]
    if states:
        items = [item for item in items if item["review"]["status"] in states]
    return _page(items, offset, limit)


def candidate_detail(
    connection: sqlite3.Connection,
    candidate_id: str,
    *,
    evidence_offset: int = 0,
    evidence_limit: int = 20,
    history_offset: int = 0,
    history_limit: int = 20,
) -> dict[str, Any]:
    page_args(evidence_offset, evidence_limit)
    page_args(history_offset, history_limit)
    row = connection.execute(
        "SELECT * FROM career_fact_candidates WHERE id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceNotFoundError("fact candidate not found")
    authority = validate_recognized_fact_authority(connection, str(row["fact_run_id"]))
    candidate = next(
        (item for item in authority.candidates if item.id == candidate_id), None
    )
    if candidate is None:
        raise WorkspaceNotFoundError("fact candidate not found")
    extraction = connection.execute(
        "SELECT * FROM resume_extraction_runs WHERE id=?",
        (authority.run.extraction_run_id,),
    ).fetchone()
    if extraction is None or extraction["raw_text"] is None:
        raise WorkspaceAuthorityError("evidence authority is unavailable")
    artifact = str(extraction["raw_text"]).encode("utf-8")
    evidence_items: list[dict[str, Any]] = []
    for evidence in fact_evidence(connection, candidate_id, limit=1000):
        if evidence.coordinate_space != "extracted":
            raise WorkspaceAuthorityError("evidence authority is unavailable")
        try:
            validate_slice_hash(
                artifact, evidence.start_byte, evidence.end_byte, evidence.slice_sha256
            )
            text = artifact[evidence.start_byte : evidence.end_byte].decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise WorkspaceAuthorityError("evidence authority is unavailable") from exc
        matching = connection.execute(
            "SELECT raw_start_byte,raw_end_byte,source_locator,slice_sha256 "
            "FROM resume_text_segments WHERE extraction_run_id=? "
            "AND raw_start_byte<=? AND raw_end_byte>=? ORDER BY ordinal",
            (
                authority.run.extraction_run_id,
                evidence.start_byte,
                evidence.end_byte,
            ),
        ).fetchall()
        if len(matching) != 1:
            raise WorkspaceAuthorityError("source locator is unavailable")
        segment = matching[0]
        try:
            validate_slice_hash(
                artifact,
                int(segment["raw_start_byte"]),
                int(segment["raw_end_byte"]),
                str(segment["slice_sha256"]),
            )
        except (TypeError, ValueError) as exc:
            raise WorkspaceAuthorityError("source locator is unavailable") from exc
        evidence_items.append(
            {
                "ordinal": evidence.ordinal,
                "text": text,
                "source_label": _safe_locator(str(segment["source_locator"])),
                "start_byte": evidence.start_byte,
                "end_byte": evidence.end_byte,
            }
        )
    history_page = candidate_review_history_page(
        connection, candidate_id, offset=history_offset, limit=history_limit
    )
    history_window = history_page["items"]
    history = [
        {
            "id": item["id"],
            "candidate_id": item["candidate_id"],
            "decision": item["event_kind"],
            "created_at": item["created_at"],
        }
        for item in history_window
    ]
    item = _candidate_projection(
        connection,
        candidate,
        _approved_recognized_candidates(connection, (candidate.fact_type,)),
    )
    item["provenance"] = {
        "extraction_run_id": authority.run.extraction_run_id,
        "fact_run_id": authority.run.id,
        "source_name": str(extraction["source_name"]),
        "source_format": str(extraction["source_format"]),
    }
    item["evidence"] = _page(evidence_items, evidence_offset, evidence_limit)
    item["review_history"] = {
        "items": history,
        "total": history_page["total"],
        "offset": history_page["offset"],
        "limit": history_page["limit"],
    }
    return item


def profile_projection(
    snapshot: Any, connection: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    version = snapshot.version
    safe_facts: list[dict[str, Any]] = []
    for fact, source_ids in zip(
        snapshot.facts, snapshot.source_candidate_ids, strict=True
    ):
        try:
            value = json.loads(fact.canonical_value_json)
        except (TypeError, ValueError) as exc:
            raise WorkspaceAuthorityError(
                "Career Profile authority is unavailable"
            ) from exc
        primary_candidate_id: str | None = None
        if connection is not None and fact.origin == "approved_candidate":
            matching: list[str] = []
            for candidate_id in source_ids:
                review = effective_candidate_review_at(
                    connection, candidate_id, version.created_at
                )
                if (
                    review.status in ("approved", "approved_edit")
                    and review.canonical_value_json == fact.canonical_value_json
                ):
                    matching.append(candidate_id)
            if matching:
                primary_candidate_id = min(matching)
        safe_facts.append(
            {
                "logical_fact_id": fact.logical_fact_id,
                "fact_type": fact.fact_type,
                "value": value,
                "origin": fact.origin,
                "privacy_classification": fact.privacy_classification,
                "sort_order": fact.sort_order,
                "source_candidate_ids": list(source_ids),
                "assembly_primary_candidate_id": primary_candidate_id,
            }
        )
    return {
        "version": {
            "id": version.id,
            "version_number": version.version_number,
            "parent_version_id": version.parent_version_id,
            "origin": version.origin,
            "created_at": version.created_at,
        },
        "facts": safe_facts,
    }


def current_projection(connection: sqlite3.Connection) -> dict[str, Any]:
    state = connection.execute(
        "SELECT revision FROM career_profile_state WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise WorkspaceAuthorityError("Career Profile authority is unavailable")
    return {
        "revision": int(state["revision"]),
        "profile": profile_projection(current_profile(connection), connection),
    }


def history_projection(
    connection: sqlite3.Connection, *, offset: int = 0, limit: int = 20
) -> dict[str, Any]:
    page_args(offset, limit)
    total = int(
        connection.execute("SELECT count(*) FROM career_profile_versions").fetchone()[0]
    )
    rows = connection.execute(
        "SELECT id FROM career_profile_versions ORDER BY version_number DESC "
        "LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        snapshot = profile_snapshot(connection, str(row["id"]))
        projected = profile_projection(snapshot, connection)
        if projected is not None:
            result.append(projected)
    return {"items": result, "total": total, "offset": offset, "limit": limit}


def parse_manifest(
    payload: str | bytes | dict[str, Any],
) -> tuple[CandidateProfileFactRequest | ManualProfileFactRequest, ...]:
    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    except (TypeError, ValueError) as exc:
        raise WorkspaceRequestError("invalid profile manifest") from exc
    if (
        not isinstance(data, dict)
        or set(data) != {"entries"}
        or not isinstance(data["entries"], list)
    ):
        raise WorkspaceRequestError("invalid profile manifest")
    entries: list[CandidateProfileFactRequest | ManualProfileFactRequest] = []
    for raw in data["entries"]:
        if not isinstance(raw, dict) or raw.get("kind") not in {
            "candidate",
            "existing",
        }:
            raise WorkspaceRequestError("invalid profile manifest")
        allowed = (
            {
                "kind",
                "primary_candidate_id",
                "supporting_candidate_ids",
                "logical_fact_id",
                "distinct_identity",
                "privacy_override",
            }
            if raw["kind"] == "candidate"
            else {"kind", "logical_fact_id", "privacy_override"}
        )
        if set(raw) - allowed:
            raise WorkspaceRequestError("invalid profile manifest")
        privacy = raw.get("privacy_override")
        if privacy is not None and privacy not in {"public", "private", "sensitive"}:
            raise WorkspaceRequestError("invalid profile manifest")
        if raw["kind"] == "existing":
            if not isinstance(raw.get("logical_fact_id"), str):
                raise WorkspaceRequestError("invalid profile manifest")
            entries.append(ManualProfileFactRequest(raw["logical_fact_id"], privacy))
            continue
        primary = raw.get("primary_candidate_id")
        supporting = raw.get("supporting_candidate_ids", [])
        if (
            not isinstance(primary, str)
            or not isinstance(supporting, list)
            or not all(isinstance(value, str) for value in supporting)
        ):
            raise WorkspaceRequestError("invalid profile manifest")
        logical_fact_id = raw.get("logical_fact_id")
        distinct_identity = raw.get("distinct_identity", False)
        if logical_fact_id is not None and not isinstance(logical_fact_id, str):
            raise WorkspaceRequestError("invalid profile manifest")
        if not isinstance(distinct_identity, bool):
            raise WorkspaceRequestError("invalid profile manifest")
        entries.append(
            CandidateProfileFactRequest(
                primary,
                tuple(supporting),
                logical_fact_id,
                distinct_identity,
                privacy,
            )
        )
    return tuple(entries)
