"""Deterministic read-only exports of stored CareerOS state.

The ``export`` package projects jobs, rankings, the review queue, source
statistics, and collection history into deterministic JSON/JSONL/CSV files or
stdout. It is strictly read-only: it never writes to the database, never
acquires the collection lock, never fetches jobs, and never contacts any
external service. Each subcommand is wired through ``careeros.cli`` into the
shared search/group/rank/review/source-stat/history pipelines.
"""

from careeros.export.models import (
    MAX_EXPORT_RECORDS,
    ExportError,
    ExportFormat,
    ExportRequest,
    ExportResult,
)
from careeros.export.serializers import render_export
from careeros.export.service import (
    build_records,
    effective_limit,
)
from careeros.export.writer import write_export, write_file_export

__all__: tuple[str, ...] = (
    "MAX_EXPORT_RECORDS",
    "ExportError",
    "ExportFormat",
    "ExportRequest",
    "ExportResult",
    "build_records",
    "effective_limit",
    "render_export",
    "write_export",
    "write_file_export",
)
