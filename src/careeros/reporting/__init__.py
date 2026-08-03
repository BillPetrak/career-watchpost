"""Career Snapshot reporting: a read-only local action dashboard.

Exposes the report builder (:func:`build_report`) and the two renderers
(:func:`render_report` for text, :func:`render_report_json` for JSON). The
report never writes to the database and never acquires the collection lock.
"""

from careeros.reporting.models import (
    ActionItem,
    ActionItems,
    ApplicationPipeline,
    ApplicationSnapshot,
    CollectionHealth,
    Overview,
    Recommendations,
    Report,
)
from careeros.reporting.render import render_report, render_report_json
from careeros.reporting.service import build_report

__all__ = [
    "ActionItem",
    "ActionItems",
    "ApplicationPipeline",
    "ApplicationSnapshot",
    "CollectionHealth",
    "Overview",
    "Recommendations",
    "Report",
    "build_report",
    "render_report",
    "render_report_json",
]
