"""Deterministic export serializers.

Each renderer consumes the same :class:`ExportResult` so every format carries
exactly the same data: JSON wraps the records in a stable envelope, JSONL
writes one compact record per line, and CSV writes a header row plus one row
per record. Key order is preserved verbatim from the record builders, so
columns never reorder between runs or formats.

Envelope: ``{"records": [...], "total": N, "truncated": bool}``. ``total`` is
the number of records the pipeline produced before the export cap and
``truncated`` reports whether trailing records were dropped. JSONL and CSV
carry no envelope: each line/row is a record, and truncation is observable as
``len(records) < total`` when re-imported by the JSON reader.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from careeros.export.models import ExportFormat, ExportResult


def render_export(result: ExportResult, format: ExportFormat) -> str:
    """Render an :class:`ExportResult` in the requested format."""
    if format is ExportFormat.JSON:
        return _render_json(result)
    if format is ExportFormat.JSONL:
        return _render_jsonl(result)
    return _render_csv(result)


def _render_json(result: ExportResult) -> str:
    """Render records as an indented JSON document with envelope metadata."""
    envelope = {
        "records": [record for record in result.records],
        "total": result.total,
        "truncated": result.truncated,
    }
    return json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"


def _render_jsonl(result: ExportResult) -> str:
    """Render one compact JSON object per line, in record order."""
    lines = [json.dumps(record, ensure_ascii=False) for record in result.records]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _csv_cell(value: Any) -> Any:
    """Normalize a record value for CSV output.

    Nested structures (lists/dicts) become stable JSON strings with sorted keys
    so identical data always renders identically; booleans render as
    ``true``/``false`` and None as an empty cell.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


def _render_csv(result: ExportResult) -> str:
    """Render records as CSV with a header row from the canonical column order."""
    if not result.records:
        return ""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(result.records[0].keys()),
        lineterminator="\n",
    )
    writer.writeheader()
    for record in result.records:
        writer.writerow({key: _csv_cell(value) for key, value in record.items()})
    return buffer.getvalue()


__all__: tuple[str, ...] = ("render_export",)
