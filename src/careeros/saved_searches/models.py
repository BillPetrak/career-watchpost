"""Saved-search domain model, validation, and name policy.

A saved search captures query and filter *intent*, never cached results:
running it always queries the current database through the shared execution
pipeline. Voice of the allowed application statuses is shared centrally with
:mod:`careeros.applications.models`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from careeros.applications.models import validate_status

SEARCH_MODES: tuple[str, ...] = ("search", "rank")


class SavedSearchError(Exception):
    """Base class for saved-search domain errors."""


class ValidationError(SavedSearchError):
    """Raised when a saved-search definition is invalid."""


class DuplicateNameError(SavedSearchError):
    """Raised when a saved-search name already exists (case-insensitive)."""


class UnknownSearchError(SavedSearchError):
    """Raised when a named saved search does not exist."""


@dataclass(frozen=True)
class SavedSearchFields:
    """The user-authored definition of a saved search (no id/timestamps).

    ``description`` is stored for future extensibility and does not yet have a
    CLI flag; it is ``None`` unless created through the service layer.
    ``remote`` is ``None`` (no filter) or ``True`` (remote only), matching the
    existing search semantics where ``False`` is indistinguishable from absent.
    """

    name: str
    query: str
    mode: str
    description: str | None = None
    profile_path: str | None = None
    grouped: bool = False
    source: str | None = None
    remote: bool | None = None
    location: str | None = None
    untracked: bool = False
    include_statuses: tuple[str, ...] = ()
    exclude_statuses: tuple[str, ...] = ()
    min_score: int | None = None
    result_limit: int = 20


@dataclass(frozen=True)
class SavedSearch(SavedSearchFields):
    """A saved search as stored, with identity and timestamps."""

    id: int = field(kw_only=True)
    created_at: str = field(kw_only=True)
    updated_at: str = field(kw_only=True)


@dataclass(frozen=True)
class SavedSearchPatch:
    """Partial update input; ``None`` fields mean "not provided" (retain).

    ``profile_path`` may be a value (set), ``None`` (not provided), or cleared
    through the service layer's ``clears`` set.
    """

    description: str | None = None
    query: str | None = None
    profile_path: str | None = None
    grouped: bool | None = None
    source: str | None = None
    remote: bool | None = None
    location: str | None = None
    untracked: bool | None = None
    include_statuses: tuple[str, ...] | None = None
    exclude_statuses: tuple[str, ...] | None = None
    min_score: int | None = None
    result_limit: int | None = None


def normalize_name(name: str) -> str:
    """Trim and validate a saved-search name.

    Names are case-insensitive for uniqueness and lookup but stored as given.
    """
    stripped = name.strip()
    if not stripped:
        raise ValidationError("Saved-search name must not be empty or whitespace.")
    return stripped


def validate_fields(fields: SavedSearchFields) -> None:
    """Validate a complete saved-search definition.

    Raises :class:`ValidationError` for invalid values or combinations. This is
    the single enforcement point for the service layer; the CLI additionally
    rejects contradictory flag combinations with argparse errors.
    """
    normalize_name(fields.name)

    query = fields.query.strip()
    if not query:
        raise ValidationError("Saved-search query must not be empty or whitespace.")

    if fields.mode not in SEARCH_MODES:
        raise ValidationError(
            f"Invalid mode {fields.mode!r}; expected one of: {', '.join(SEARCH_MODES)}."
        )

    if fields.mode == "rank":
        if fields.profile_path is None or not fields.profile_path.strip():
            raise ValidationError("Rank saved searches require a profile path.")
    else:
        if fields.profile_path is not None:
            raise ValidationError(
                "Search saved searches cannot have a profile path (rank-only)."
            )
        if fields.min_score is not None:
            raise ValidationError(
                "Search saved searches cannot have a minimum score (rank-only)."
            )

    if fields.source is not None and not fields.source.strip():
        raise ValidationError("Source filter must not be empty or whitespace.")
    if fields.location is not None and not fields.location.strip():
        raise ValidationError("Location filter must not be empty or whitespace.")

    if fields.include_statuses and fields.exclude_statuses:
        raise ValidationError(
            "include_statuses and exclude_statuses cannot be combined."
        )
    if fields.untracked and (fields.include_statuses or fields.exclude_statuses):
        raise ValidationError(
            "untracked cannot be combined with include_statuses or exclude_statuses."
        )

    for status in (*fields.include_statuses, *fields.exclude_statuses):
        try:
            validate_status(status)
        except ValueError as error:
            raise ValidationError(str(error)) from error

    if fields.min_score is not None and not (0 <= fields.min_score <= 100):
        raise ValidationError("min_score must be between 0 and 100.")

    if fields.result_limit < 1:
        raise ValidationError("result_limit must be at least 1.")
