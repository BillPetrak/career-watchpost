"""Export domain models: formats, the record cap, and validated options.

The export package is a read-only projection of stored CareerOS state. It never
writes to the database, never acquires the collection lock, never fetches jobs,
and never contacts any external service. Records are produced as deterministic,
ordered dictionaries so every format (JSON, JSONL, CSV) renders the same data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# Hard upper bound on exported records. ``--limit 0`` ("no cap") still stops at
# this value so a single export can never exhaust memory on a huge database.
MAX_EXPORT_RECORDS = 1_000_000


class ExportFormat(Enum):
    """Supported export serialization formats."""

    JSON = "json"
    JSONL = "jsonl"
    CSV = "csv"


FORMAT_EXTENSIONS: dict[ExportFormat, str] = {
    ExportFormat.JSON: ".json",
    ExportFormat.JSONL: ".jsonl",
    ExportFormat.CSV: ".csv",
}


class ExportError(Exception):
    """A user-facing export failure (invalid output path, write error, ...)."""


@dataclass(frozen=True)
class ExportRequest:
    """Validated options shared by every export subcommand.

    Only the fields relevant to the selected subcommand are populated; the
    per-command builders ignore the rest. ``limit`` is the raw CLI value: 0
    means "no cap" and any positive value is bounded by
    :data:`MAX_EXPORT_RECORDS`. ``output`` is None when writing to stdout.
    """

    format: ExportFormat = ExportFormat.JSON
    output: Path | None = None
    limit: int = 20
    overwrite: bool = False
    grouped: bool = False
    clean_descriptions: bool = False
    query: str | None = None
    profile: Path | None = None
    remote: bool | None = None
    location: str | None = None
    source: str | None = None
    employment_type: str | None = None
    untracked: bool = False
    include_statuses: tuple[str, ...] = ()
    exclude_statuses: tuple[str, ...] = ()
    min_score: int = 0
    state: str = "unreviewed"
    include_tracked: bool = False
    use_feedback: bool = True
    failed_only: bool = False


@dataclass(frozen=True)
class ExportResult:
    """The built records plus truncation metadata.

    ``total`` is the number of records the underlying pipeline produced before
    the export cap was applied (so callers can report what was omitted).
    ``truncated`` is True exactly when ``len(records) < total`` because of the
    cap. ``schema`` is a stable machine name for the record shape, e.g.
    ``careeros.jobs.v1``.
    """

    schema: str
    records: tuple[dict[str, Any], ...]
    total: int = field(default=0)

    @property
    def truncated(self) -> bool:
        """Return True when the record cap dropped trailing records."""
        return self.total > len(self.records)
