"""The private, human-authoritative ``career-profile`` command family."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from careeros.career_profile.service import (
    assemble_profile,
    review_candidate,
)
from careeros.career_profile.workspace import (
    WorkspaceRequestError,
    analyze,
    candidate_detail,
    candidates,
    current_projection,
    history_projection,
    parse_manifest,
    profile_projection,
    sources,
    validate_review_candidate_authority,
)
from careeros.cli.common import EXIT_TEMP_FAILURE, _lock_conflict_message
from careeros.database.connection import connect_current_rw, connect_read_only
from careeros.locking import CollectionLockConflict, collection_lock
from careeros.settings import materials_root


def _emit(value: Any, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    if isinstance(value, list):
        for item in value:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            else:
                print(f"{key}: {item}")
    else:
        print(value)


def _safe_file_json(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise WorkspaceRequestError("value file is unavailable") from exc
    except (TypeError, ValueError) as exc:
        raise WorkspaceRequestError("value file is unavailable") from exc
    if not isinstance(value, dict):
        raise WorkspaceRequestError("value file is unavailable")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read(args: argparse.Namespace, value: Any) -> int:
    try:
        _emit(value, args.format)
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Career Profile read failed: {type(exc).__name__}", file=sys.stderr)
        return 1


def _write_error(exc: Exception) -> int:
    if isinstance(exc, CollectionLockConflict):
        print(_lock_conflict_message(exc), file=sys.stderr)
        return EXIT_TEMP_FAILURE
    if isinstance(exc, WorkspaceRequestError):
        print("Career Profile request is invalid.", file=sys.stderr)
        return 2
    print("Career Profile operation failed.", file=sys.stderr)
    return 1


def run_career_profile_command(args: argparse.Namespace) -> int:
    command = args.career_profile_command
    if command == "sources":
        try:
            with connect_read_only(args.database) as connection:
                return _read(
                    args,
                    sources(
                        connection,
                        include_retired=args.include_retired,
                        offset=args.offset,
                        limit=args.limit,
                    ),
                )
        except Exception as exc:
            return _write_error(exc)
    if command == "analyze":
        root = (
            args.materials_root if args.materials_root is not None else materials_root()
        )
        try:
            with collection_lock(args.database, command="career-profile analyze"):
                with connect_current_rw(args.database) as connection:
                    result = analyze(
                        connection, root, args.material_id, args.version_id
                    )
            _emit(result, args.format)
            return 0
        except Exception as exc:
            return _write_error(exc)
    if command == "candidates":
        try:
            with connect_read_only(args.database) as connection:
                result = candidates(
                    connection,
                    args.fact_run_id,
                    offset=args.offset,
                    limit=args.limit,
                    states=tuple(args.state or ()),
                )
            return _read(args, result)
        except Exception as exc:
            return _write_error(exc)
    if command == "candidate":
        try:
            with connect_read_only(args.database) as connection:
                result = candidate_detail(
                    connection,
                    args.candidate_id,
                    evidence_offset=args.evidence_offset,
                    evidence_limit=args.evidence_limit,
                    history_offset=args.history_offset,
                    history_limit=args.history_limit,
                )
            return _read(args, result)
        except Exception as exc:
            return _write_error(exc)
    if command == "review":
        try:
            if args.decision == "approved_edit" and args.value_file is None:
                raise WorkspaceRequestError("approved_edit requires a value file")
            if args.decision != "approved_edit" and args.value_file is not None:
                raise WorkspaceRequestError(
                    "value file is only valid for approved_edit"
                )
            edited = (
                _safe_file_json(args.value_file)
                if args.value_file is not None
                else None
            )
            with collection_lock(args.database, command="career-profile review"):
                with connect_current_rw(args.database) as connection:
                    validate_review_candidate_authority(connection, args.candidate_id)
                    review_result = review_candidate(
                        connection,
                        args.candidate_id,
                        args.decision,
                        edited_value_json=edited,
                    )
            _emit(
                {
                    "candidate_id": review_result.candidate_id,
                    "status": review_result.status,
                },
                args.format,
            )
            return 0
        except Exception as exc:
            return _write_error(exc)
    if command == "show":
        try:
            with connect_read_only(args.database) as connection:
                if args.version is None:
                    result = current_projection(connection)
                else:
                    from careeros.career_profile.service import profile_snapshot

                    result = {
                        "profile": profile_projection(
                            profile_snapshot(connection, args.version), connection
                        )
                    }
            return _read(args, result)
        except Exception as exc:
            return _write_error(exc)
    if command == "history":
        try:
            with connect_read_only(args.database) as connection:
                return _read(
                    args,
                    history_projection(
                        connection, offset=args.offset, limit=args.limit
                    ),
                )
        except Exception as exc:
            return _write_error(exc)
    if command == "save":
        try:
            entries = parse_manifest(_safe_file_json(args.manifest))
            with collection_lock(args.database, command="career-profile save"):
                with connect_current_rw(args.database) as connection:
                    save_result = assemble_profile(
                        connection, args.expected_revision, entries
                    )
            _emit(
                {
                    "version": {
                        "id": save_result.version.id,
                        "version_number": save_result.version.version_number,
                        "created_at": save_result.version.created_at,
                    },
                    "revision": save_result.state.revision,
                    "fact_count": len(save_result.facts),
                },
                args.format,
            )
            return 0
        except Exception as exc:
            return _write_error(exc)
    print("Unsupported Career Profile command.", file=sys.stderr)
    return 2
