"""Candidate profile loader from JSON or YAML."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from careeros.ranking.scorer import CandidateProfile

_YAML_SUFFIXES = {".yaml", ".yml"}
_JSON_SUFFIXES = {".json"}
_SUPPORTED_SUFFIXES = _YAML_SUFFIXES | _JSON_SUFFIXES


def _load_data(path: Path) -> dict[str, Any]:
    """Read and parse a profile file, returning the root mapping.

    The format is chosen by file extension: ``.yaml``/``.yml`` are parsed
    as YAML and ``.json`` as JSON. Any other extension is rejected.
    """
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ValueError(
            f"Unsupported profile file extension {path.suffix!r}; "
            f"expected one of {', '.join(sorted(_SUPPORTED_SUFFIXES))}."
        )

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as e:
        raise ValueError(f"Failed to read profile file: {e}") from e

    if suffix in _YAML_SUFFIXES:
        try:
            data: object = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in profile file: {e}") from e

        if not isinstance(data, dict):
            raise ValueError(
                f"Profile must be a YAML mapping, got {type(data).__name__}."
            )
        return data

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in profile file: {e.msg}",
            e.doc,
            e.pos,
        ) from e

    if not isinstance(data, dict):
        raise ValueError(f"Profile must be a JSON object, got {type(data).__name__}.")

    return data


def _load_string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    """Extract and validate a list-of-strings field."""
    if key not in data:
        return ()

    value = data[key]

    if not isinstance(value, list):
        raise ValueError(
            f"Field '{key}' must be a list of strings, got {type(value).__name__}."
        )

    result: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"Field '{key}' must contain only strings, "
                f"got {type(item).__name__} at index {i}."
            )
        stripped = item.strip()
        if stripped == "":
            raise ValueError(f"Field '{key}' contains an empty string at index {i}.")
        result.append(stripped)

    return tuple(result)


def _load_remote_preference(data: dict[str, Any]) -> str | None:
    """Extract and validate the optional remote_preference field."""
    if "remote_preference" not in data:
        return None

    value = data["remote_preference"]

    if value is None:
        return None

    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None

    raise ValueError(
        f"Field 'remote_preference' must be a string or null, "
        f"got {type(value).__name__}."
    )


def load_profile(path: Path | str) -> CandidateProfile:
    """Load a candidate profile from a JSON or YAML file.

    The format is chosen by file extension; unsupported extensions are
    rejected. Unsupported top-level fields are silently ignored.
    """
    path_obj = path if isinstance(path, Path) else Path(path)

    data = _load_data(path_obj)

    return CandidateProfile(
        desired_titles=_load_string_list(data, "desired_titles"),
        skills=_load_string_list(data, "skills"),
        preferred_locations=_load_string_list(data, "preferred_locations"),
        remote_preference=_load_remote_preference(data),
        excluded_terms=_load_string_list(data, "excluded_terms"),
        required_skills=_load_string_list(data, "required_skills"),
        hard_excluded_terms=_load_string_list(data, "hard_excluded_terms"),
        preferred_employment_types=_load_string_list(
            data, "preferred_employment_types"
        ),
        preferred_seniority=_load_string_list(data, "preferred_seniority"),
    )
