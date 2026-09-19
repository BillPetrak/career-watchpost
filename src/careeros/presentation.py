"""Small presentation-only helpers shared by human-readable renderers."""

from __future__ import annotations

_ENUM_LABELS = {
    "remote": "Remote",
    "onsite_or_unspecified": "On-site or unspecified",
    "interested": "Interested",
    "applied": "Applied",
    "interview": "Interview",
    "offer": "Offer",
    "rejected": "Rejected",
    "withdrawn": "Withdrawn",
    "shortlist": "Shortlist",
    "dismiss": "Dismiss",
    "review_later": "Review later",
    "unreviewed": "Unreviewed",
    "follow_up": "Follow up",
    "review": "Review",
    "custom": "Custom",
    "attempted": "Attempted",
    "delivered": "Delivered",
    "failed": "Failed",
    "full_time": "Full time",
    "part_time": "Part time",
}


def enum_label(value: str | None, *, empty: str = "N/A") -> str:
    """Return a stable human label without changing the underlying value."""
    if value is None or not value.strip():
        return empty
    if value in _ENUM_LABELS:
        return _ENUM_LABELS[value]
    if "_" in value or "-" in value:
        return value.replace("_", " ").replace("-", " ").capitalize()
    return value.capitalize()


__all__ = ["enum_label"]
