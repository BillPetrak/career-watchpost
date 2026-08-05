"""Read-only service that computes unified per-source statistics.

Only SELECTs against the ``jobs`` table and the pure grouping normalizers are
used; nothing writes or mutates stored state. The service reuses the existing
grouping identity logic rather than reimplementing normalization, so the
"never groupable" signal always agrees with the matcher. A single ``order``
(defaulting to the collector registry order) keeps the per-source summary
aligned with ``collect status`` and the report's collection health.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from careeros.collectors import COLLECTORS, CollectorName
from careeros.database.jobs import StoredJob, list_all_jobs
from careeros.grouping.matcher import identity_for
from careeros.source_stats.models import (
    MISSING_FIELD_COLUMNS,
    FieldCoverage,
    SourceStats,
)


def _field_is_missing(job: StoredJob, column: str) -> bool:
    """Return True when a stored job does not provide a nullable field.

    Both NULL and an empty string count as "not provided by source"; the
    collectors normalize absent values to NULL, and empty text is never a
    meaningful value for any of the tracked fields.
    """
    value = getattr(job, column)
    return value is None or value == ""


def _coverage_pct(provided_count: int, total_jobs: int) -> int | None:
    """Derive the field-coverage percentage from the existing counts.

    Returns ``None`` when the source has no stored jobs: an empty set has no
    meaningful coverage percentage.
    """
    if total_jobs == 0:
        return None
    return round(provided_count / total_jobs * 100)


def _fields_for(jobs: Sequence[StoredJob]) -> tuple[FieldCoverage, ...]:
    """Compute one :class:`FieldCoverage` per tracked field, in column order."""
    total_jobs = len(jobs)
    fields: list[FieldCoverage] = []
    for column in MISSING_FIELD_COLUMNS:
        missing = sum(1 for job in jobs if _field_is_missing(job, column))
        provided = total_jobs - missing
        fields.append(
            FieldCoverage(
                field=column,
                missing_count=missing,
                provided_count=provided,
                coverage_pct=_coverage_pct(provided, total_jobs),
            )
        )
    return tuple(fields)


def _never_groupable_count(jobs: Sequence[StoredJob]) -> int:
    """Count stored jobs whose grouping identity is unidentifiable.

    Reuses the grouping matcher's ``identity_for``, so a job is never groupable
    exactly when the matcher would skip it (empty normalized company or title
    signature).
    """
    never_groupable = 0
    for job in jobs:
        identity = identity_for(job)
        if not identity.company_sig or not identity.title_sig:
            never_groupable += 1
    return never_groupable


def compute_source_stats(
    connection: sqlite3.Connection,
    *,
    order: Sequence[CollectorName] | None = None,
) -> tuple[SourceStats, ...]:
    """Return one :class:`SourceStats` per source, in ``order``.

    ``order`` defaults to the collector registry order so the result aligns
    with ``collect status`` and the report's collection-health section. Sources
    with no stored jobs are included with zero counts so the unified summary is
    complete and deterministic.
    """
    if order is None:
        order = tuple(COLLECTORS)

    by_source: dict[str, list[StoredJob]] = {source: [] for source in order}
    for job in list_all_jobs(connection):
        by_source.setdefault(job.source, []).append(job)

    stats: list[SourceStats] = []
    for source in order:
        jobs = by_source[source]
        stats.append(
            SourceStats(
                source=source,
                total_jobs=len(jobs),
                fields=_fields_for(jobs),
                never_groupable_jobs=_never_groupable_count(jobs),
            )
        )
    return tuple(stats)
