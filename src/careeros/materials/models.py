from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = (
    "resume",
    "cover_letter_base",
    "portfolio",
    "supporting_document",
    "other",
)
MAX_BYTES = 25 * 1024 * 1024
MAX_VERSIONS = 100


class MaterialError(Exception):
    """Privacy-safe material domain failure."""


class ConflictError(MaterialError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "material_conflict",
        current_revision: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_revision = current_revision


class NotFoundError(MaterialError):
    pass


class ValidationError(MaterialError):
    pass


class StorageError(MaterialError):
    pass


@dataclass(frozen=True)
class Version:
    id: str
    material_id: str
    version_number: int
    display_filename: str
    declared_media_type: str
    byte_size: int
    created_at: str


@dataclass(frozen=True)
class Material:
    id: str
    name: str
    category: str
    revision: int
    status: str
    version_count: int
    current_version: Version | None
    retired_at: str | None
    created_at: str
    updated_at: str
