"""Immutable data model for unified source statistics.

These dataclasses are presentation-agnostic: they hold values only, never
connections or printers. The service layer (:mod:`careeros.source_stats.service`)
computes :class:`SourceStats` from the database, and the reporting layer shapes
them into the Career Snapshot Overview. Nothing here ever writes to the
database.
"""

from __future__ import annotations

from dataclasses import dataclass

# The eight job fields a source may leave empty. Each maps to a nullable column
# in the ``jobs`` table; a stored row "does not provide" a field when the
# column is NULL or an empty string. ``title`` and ``collected_at`` are always
# required and therefore never measured here. Field order here is the stable
# order used by the service, the report Overview, and both renderers.
MISSING_FIELD_COLUMNS: tuple[str, ...] = (
    "source_job_id",
    "company",
    "location",
    "description",
    "url",
    "employment_type",
    "remote_status",
    "published_at",
)

# Human-facing wording for the missing-field vocabulary, shared by the
# reporting renderers so the report never re-invents the phrase.
MISSING_FIELD_WORDING = "not provided by source"


@dataclass(frozen=True)
class FieldCoverage:
    """Coverage of one stored job field for one source.

    ``missing_count`` counts the source's stored rows that do not provide the
    field; ``provided_count`` is ``total_jobs - missing_count``. ``coverage_pct``
    is the percentage derived from those counts
    (``round(provided_count / total_jobs * 100)``), or ``None`` when the source
    has no stored jobs because an empty set has no meaningful coverage.
    """

    field: str
    missing_count: int
    provided_count: int
    coverage_pct: int | None


@dataclass(frozen=True)
class SourceStats:
    """Unified statistics for one job source.

    ``total_jobs`` counts the source's stored rows. ``fields`` holds one
    :class:`FieldCoverage` per tracked field in :data:`MISSING_FIELD_COLUMNS`
    order. ``never_groupable_jobs`` counts stored rows whose normalized company
    or title signature is empty, so they can never participate in duplicate
    grouping -- a source-quality signal that a source's listings are poorly
    structured.
    """

    source: str
    total_jobs: int
    fields: tuple[FieldCoverage, ...]
    never_groupable_jobs: int
