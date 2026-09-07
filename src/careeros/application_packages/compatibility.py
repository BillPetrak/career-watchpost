"""Frozen fork/rebase requirement compatibility matrix."""

from __future__ import annotations

from typing import Any


def requirement_compatibility(
    old: dict[str, Any], current: dict[str, Any]
) -> tuple[bool, bool, bool]:
    """Return ``(copy_selection, copy_answer, copy_confirmation)``.

    Compatibility intentionally distinguishes Material metadata from
    answer-capable requirements; required/fulfillment/deadline/ordering and
    M1 confirmation changes do not invalidate package-owned user work.
    """
    if old.get("kind") != current.get("kind"):
        return False, False, False
    if current.get("fulfillment_state") == "not_applicable":
        return False, False, False
    kind = current.get("kind")
    if kind == "material":
        compatible = old.get("label") == current.get("label") and old.get(
            "detail"
        ) == current.get("detail")
        return compatible, False, False
    answer_capable = kind in {"question", "link", "other"}
    if not answer_capable:
        return False, False, False
    definition_changed = old.get("label") != current.get("label") or old.get(
        "detail"
    ) != current.get("detail")
    return False, True, not definition_changed
