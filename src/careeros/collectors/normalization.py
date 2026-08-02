"""Shared normalization helpers for job collectors."""

from __future__ import annotations


def optional_string(value: object) -> str | None:
    """Return a stripped string or None."""
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    return stripped or None


def employment_type(value: object) -> str | None:
    """Join a list of employment-type strings into a single value."""
    if not isinstance(value, list):
        return None

    types = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return ", ".join(types) or None
