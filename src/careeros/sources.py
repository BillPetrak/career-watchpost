"""Central source catalog and configuration-aware source-set resolution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from careeros.collectors import COLLECTORS
from careeros.source_config import SourceConfig, load_source_config

BUILT_IN_SOURCES = tuple(COLLECTORS)
CONFIGURABLE_SOURCES = ("lever",)
KNOWN_SOURCES = (*BUILT_IN_SOURCES, *CONFIGURABLE_SOURCES)


@dataclass(frozen=True)
class SourceCatalog:
    active: tuple[str, ...]
    status: tuple[str, ...]
    statistics: tuple[str, ...]
    known: tuple[str, ...] = KNOWN_SOURCES
    config: SourceConfig | None = None


def _historical(connection: sqlite3.Connection | None) -> set[str]:
    if connection is None:
        return set()
    found: set[str] = set()
    for table in ("jobs", "collection_runs"):
        try:
            rows = connection.execute(f"SELECT DISTINCT source FROM {table}").fetchall()
        except sqlite3.Error:
            continue
        found.update(row[0] for row in rows if row[0] in CONFIGURABLE_SOURCES)
    return found


def resolve_source_catalog(
    connection: sqlite3.Connection | None = None,
    *,
    config: SourceConfig | None = None,
    config_path: str | None = None,
) -> SourceCatalog:
    """Resolve active, observability, statistics, and known sources.

    This function performs no schema initialization and no writes. Callers that
    only need historical source names may omit ``config`` and avoid loading the
    private configuration entirely by passing ``config=SourceConfig(...)``.
    """
    if config is None:
        config = load_source_config(config_path)
    historical = _historical(connection)
    lever_present = config.lever.present
    lever_enabled = lever_present and config.lever.enabled
    active = (*BUILT_IN_SOURCES, "lever") if lever_enabled else BUILT_IN_SOURCES
    visible = lever_present or historical
    status = (*BUILT_IN_SOURCES, "lever") if visible else BUILT_IN_SOURCES
    stats = (
        (*BUILT_IN_SOURCES, "lever")
        if lever_present or "lever" in _historical_jobs(connection)
        else BUILT_IN_SOURCES
    )
    return SourceCatalog(active, status, stats, KNOWN_SOURCES, config)


def _historical_jobs(connection: sqlite3.Connection | None) -> set[str]:
    if connection is None:
        return set()
    try:
        rows = connection.execute("SELECT DISTINCT source FROM jobs").fetchall()
    except sqlite3.Error:
        return set()
    return {row[0] for row in rows if row[0] in CONFIGURABLE_SOURCES}


__all__ = [
    "BUILT_IN_SOURCES",
    "CONFIGURABLE_SOURCES",
    "KNOWN_SOURCES",
    "SourceCatalog",
    "resolve_source_catalog",
]
