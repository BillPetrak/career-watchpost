"""Persistence-neutral Career Profile contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ProfileCreationOrigin = Literal["manual", "approved_facts", "imported"]
ProfileFactOrigin = Literal["manual", "approved_candidate", "imported"]


@dataclass(frozen=True)
class CareerProfileVersion:
    id: str
    version_number: int
    parent_version_id: str | None
    content_fingerprint: str
    origin: ProfileCreationOrigin
    created_at: str

    def __post_init__(self) -> None:
        if self.version_number < 1:
            raise ValueError("profile version number must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_fingerprint):
            raise ValueError("profile content fingerprint must be lowercase SHA-256")
        if self.origin not in ("manual", "approved_facts", "imported"):
            raise ValueError("invalid profile creation origin")


@dataclass(frozen=True)
class CareerProfileFact:
    id: str
    profile_version_id: str
    logical_fact_id: str
    fact_type: str
    normalized_key: str
    canonical_value_json: str
    value_sha256: str
    origin: ProfileFactOrigin
    privacy_classification: str
    sort_order: int

    def __post_init__(self) -> None:
        if self.sort_order < 0:
            raise ValueError("profile fact sort order must be non-negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.value_sha256):
            raise ValueError("profile fact hash must be lowercase SHA-256")
        if self.origin not in ("manual", "approved_candidate", "imported"):
            raise ValueError("invalid profile fact origin")
        if self.privacy_classification not in ("public", "private", "sensitive"):
            raise ValueError("invalid profile fact privacy classification")


@dataclass(frozen=True)
class CareerProfileFactSource:
    profile_fact_id: str
    candidate_id: str


@dataclass(frozen=True)
class CareerProfileState:
    current_profile_version_id: str | None
    revision: int
    next_version_number: int

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("profile revision must be non-negative")
        if self.next_version_number < 1:
            raise ValueError("next profile version number must be positive")
