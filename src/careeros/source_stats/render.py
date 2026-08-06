"""Deterministic renderers for the ``careeros source stats`` command.

Both the default text renderer and the JSON renderer consume the same immutable
:class:`SourceStats` tuples produced by :mod:`careeros.source_stats.service`;
no business logic is reimplemented here. Text output is readable in a normal
terminal and JSON is sorted-key and independently deterministic.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from careeros.presenters import source_stats_dict
from careeros.source_stats.models import MISSING_FIELD_WORDING, SourceStats


def render_source_stats_text(stats: Sequence[SourceStats]) -> str:
    """Render per-source statistics as deterministic plain text."""
    lines = [
        "Source statistics",
        "-----------------",
    ]
    for entry in stats:
        lines.extend(_source_stats_block(entry))
    return "\n".join(lines) + "\n"


def render_source_stats_json(stats: Sequence[SourceStats]) -> str:
    """Render per-source statistics as deterministic JSON (same data)."""
    payload = {"sources": [source_stats_dict(entry) for entry in stats]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _source_stats_block(entry: SourceStats) -> list[str]:
    lines = [
        "",
        f"{entry.source}: {entry.total_jobs} jobs, "
        f"never groupable: {entry.never_groupable_jobs}",
    ]
    if entry.total_jobs == 0:
        lines.append(f"  Fields {MISSING_FIELD_WORDING}: none")
        lines.append("  Coverage: none")
        return lines

    missing = [field for field in entry.fields if field.missing_count > 0]
    if missing:
        lines.append(f"  Fields {MISSING_FIELD_WORDING}:")
        lines.extend(f"    {field.field} {field.missing_count}" for field in missing)
    else:
        lines.append(f"  Fields {MISSING_FIELD_WORDING}: none")

    lines.append("  Coverage:")
    for field in entry.fields:
        pct = f"{field.coverage_pct}%" if field.coverage_pct is not None else "-"
        lines.append(f"    {field.field} {pct}")
    return lines
