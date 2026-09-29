"""Central source catalog and configuration-aware source-set resolution."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from careeros.collectors import COLLECTORS
from careeros.source_config import SourceConfig, load_source_config

BUILT_IN_SOURCES = tuple(COLLECTORS)
CONFIGURABLE_SOURCES = ("lever", "greenhouse", "workable")
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
    configured_present = {
        "lever": config.lever.present,
        "greenhouse": config.greenhouse.present,
        "workable": config.workable.present,
    }
    configured_enabled = {
        "lever": config.lever.enabled,
        "greenhouse": config.greenhouse.enabled,
        "workable": config.workable.enabled,
    }
    active_configurable = tuple(
        source
        for source in CONFIGURABLE_SOURCES
        if configured_present[source] and configured_enabled[source]
    )
    visible_configurable = tuple(
        source
        for source in CONFIGURABLE_SOURCES
        if configured_present[source] or source in historical
    )
    historical_jobs = _historical_jobs(connection)
    statistics_configurable = tuple(
        source
        for source in CONFIGURABLE_SOURCES
        if configured_present[source] or source in historical_jobs
    )
    active = (*BUILT_IN_SOURCES, *active_configurable)
    status = (*BUILT_IN_SOURCES, *visible_configurable)
    stats = (*BUILT_IN_SOURCES, *statistics_configurable)
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
