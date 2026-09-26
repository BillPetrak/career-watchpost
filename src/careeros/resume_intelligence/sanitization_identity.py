"""Pure identity and override-manifest contracts for M4 sanitization."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypedDict, cast

from .pii import MAX_FINDINGS, MAX_TEXT_BYTES

OverrideAction = Literal["pending", "replace", "keep", "review", "delete"]


class OverrideManifestEntry(TypedDict, total=False):
    finding_ordinal: int
    action: OverrideAction
    replacement: str


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


SANITIZER_FINGERPRINT = fingerprint(
    {
        "implementation": "careeros-m4-local-pii-v2",
        "detector_rules": (
            "email-v1",
            "email-unicode-v2",
            "email-label-ambiguous-v2",
            "phone-v1",
            "name-label-v1",
            "name-label-unicode-v2",
            "name-label-ambiguous-v2",
        ),
        "resolver": "strict-overlap-components-v1",
    }
)
DEFAULT_POLICY_FINGERPRINT = fingerprint(
    {
        "categories": ("email", "name", "phone"),
        "detected_disposition": "replace",
        "ambiguous_label_disposition": "review",
        "max_findings": MAX_FINDINGS,
        "max_input_bytes": MAX_TEXT_BYTES,
        "max_sanitized_bytes": MAX_TEXT_BYTES,
        "placeholder": "[PII:{category}]",
        "unresolved_approval": "deny",
    }
)


def override_manifest(
    actions: dict[int, OverrideAction], replacements: dict[int, str]
) -> str:
    entries: list[OverrideManifestEntry] = []
    for ordinal in sorted(actions):
        entry: OverrideManifestEntry = {
            "finding_ordinal": ordinal,
            "action": actions[ordinal],
        }
        if ordinal in replacements:
            entry["replacement"] = replacements[ordinal]
        entries.append(entry)
    return canonical_json({"version": 1, "actions": entries})


def parse_override_manifest(value: str) -> tuple[OverrideManifestEntry, ...]:
    """Parse the one canonical M4 override-manifest shape."""

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("override manifest is not valid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"version", "actions"}:
        raise ValueError("override manifest has an invalid shape")
    if parsed["version"] != 1 or not isinstance(parsed["actions"], list):
        raise ValueError("override manifest has an invalid version")
    if not parsed["actions"]:
        raise ValueError("override manifest requires at least one action")
    allowed = {"pending", "replace", "keep", "review", "delete"}
    result: list[OverrideManifestEntry] = []
    seen: set[int] = set()
    for raw_entry in parsed["actions"]:
        if not isinstance(raw_entry, dict) or not set(raw_entry).issubset(
            {"finding_ordinal", "action", "replacement"}
        ):
            raise ValueError("override manifest action has an invalid shape")
        if set(raw_entry) < {"finding_ordinal", "action"}:
            raise ValueError("override manifest action is incomplete")
        ordinal = raw_entry["finding_ordinal"]
        action = raw_entry["action"]
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
            or ordinal in seen
            or action not in allowed
        ):
            raise ValueError("override manifest action is invalid")
        seen.add(ordinal)
        replacement = raw_entry.get("replacement")
        if replacement is not None and (
            action != "replace" or not isinstance(replacement, str) or replacement == ""
        ):
            raise ValueError("override manifest replacement is invalid")
        result.append(cast(OverrideManifestEntry, dict(raw_entry)))
    if tuple(entry["finding_ordinal"] for entry in result) != tuple(sorted(seen)):
        raise ValueError("override manifest actions are not ordered")
    if canonical_json(parsed) != value:
        raise ValueError("override manifest is not canonical JSON")
    return tuple(result)


def override_policy_fingerprint(override_manifest_json: str) -> str:
    return fingerprint(
        {
            "base_policy_fingerprint": DEFAULT_POLICY_FINGERPRINT,
            "override_manifest_json": override_manifest_json,
        }
    )


def sanitization_cache_fingerprint(
    *,
    extraction_run_id: str,
    raw_text_sha256: str,
    raw_text_byte_size: int,
    policy_fingerprint: str,
    parent_sanitization_run_id: str | None,
    override_manifest_json: str,
) -> str:
    return fingerprint(
        {
            "extraction_run_id": extraction_run_id,
            "raw_text_sha256": raw_text_sha256,
            "raw_text_byte_size": raw_text_byte_size,
            "sanitizer_fingerprint": SANITIZER_FINGERPRINT,
            "policy_fingerprint": policy_fingerprint,
            "parent_sanitization_run_id": parent_sanitization_run_id,
            "override_manifest_json": override_manifest_json,
        }
    )
