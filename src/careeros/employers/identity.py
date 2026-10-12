"""Stable employer and exact ATS feed identities.

These identities deliberately exclude mutable names, domains, and vacancy text.
They are values only: constructing them performs validation but no I/O.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum

_TARGET_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9-]{0,99}\Z")


class ATSProvider(StrEnum):
    """ATS providers authorized for the implementation boundary."""

    LEVER = "lever"
    GREENHOUSE = "greenhouse"
    WORKABLE = "workable"
    SMARTRECRUITERS = "smartrecruiters"


def _canonical_uuid(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{label} must be a canonical UUID")


@dataclass(frozen=True)
class EmployerId:
    """Opaque durable employer identity, unrelated to employer metadata."""

    value: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.value, "employer identity")


@dataclass(frozen=True)
class FeedLineageId:
    """Logical feed lineage identity, separate from any exact target."""

    value: str

    def __post_init__(self) -> None:
        _canonical_uuid(self.value, "feed lineage identity")


@dataclass(frozen=True)
class ExactFeedTarget:
    """Canonical immutable ``(provider, instance, target_identifier)`` key.

    The target grammar intentionally matches the released ``sources.yaml``
    parser. Lever retains its distinct global and EU instances. The other
    released ATS integrations have one public/global instance.
    """

    provider: ATSProvider
    instance: str
    target_identifier: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, ATSProvider):
            raise TypeError("provider must be an ATSProvider")
        if not isinstance(self.instance, str):
            raise TypeError("instance must be a string")
        allowed_instances = (
            frozenset({"global", "eu"})
            if self.provider is ATSProvider.LEVER
            else frozenset({"global"})
        )
        if self.instance not in allowed_instances:
            raise ValueError(
                f"unsupported {self.provider.value} instance: {self.instance!r}"
            )
        if not isinstance(
            self.target_identifier, str
        ) or not _TARGET_IDENTIFIER.fullmatch(self.target_identifier):
            raise ValueError("target identifier is not canonical")

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the serialization-neutral canonical key."""
        return (self.provider.value, self.instance, self.target_identifier)

    @property
    def source_name(self) -> str:
        """Preserve the released collector source identity."""
        return self.provider.value


@dataclass(frozen=True)
class FeedIdentity:
    """An exact target associated with an independently durable lineage."""

    target: ExactFeedTarget
    lineage_id: FeedLineageId

    def __post_init__(self) -> None:
        if not isinstance(self.target, ExactFeedTarget):
            raise TypeError("target must be an ExactFeedTarget")
        if not isinstance(self.lineage_id, FeedLineageId):
            raise TypeError("lineage_id must be a FeedLineageId")


__all__ = [
    "ATSProvider",
    "EmployerId",
    "ExactFeedTarget",
    "FeedIdentity",
    "FeedLineageId",
]
