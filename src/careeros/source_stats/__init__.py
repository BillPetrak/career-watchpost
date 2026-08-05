"""Unified source statistics and source-quality diagnostics.

Exposes the read-only :func:`compute_source_stats` service and the immutable
statistics models. The reporting layer consumes these to render a per-source
summary in the Career Snapshot Overview; nothing here ever writes to the
database or acquires the collection lock.
"""

from careeros.source_stats.models import (
    MISSING_FIELD_COLUMNS,
    MISSING_FIELD_WORDING,
    FieldCoverage,
    SourceStats,
)
from careeros.source_stats.service import compute_source_stats

__all__ = [
    "MISSING_FIELD_COLUMNS",
    "MISSING_FIELD_WORDING",
    "FieldCoverage",
    "SourceStats",
    "compute_source_stats",
]
