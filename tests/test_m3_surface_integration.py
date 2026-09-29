"""M3 catalog and CLI surface invariants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from careeros.cli.common import _SOURCE_LABELS
from careeros.cli.parser import build_parser
from careeros.collectors import COLLECTORS
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import initialize_schema
from careeros.export.models import ExportRequest
from careeros.export.service import build_source_stats_records
from careeros.reporting.service import build_report
from careeros.source_stats.service import compute_source_stats
from careeros.sources import BUILT_IN_SOURCES, KNOWN_SOURCES, resolve_source_catalog
from careeros.status import build_statuses


def _config(*, present: bool, enabled: bool):
    return SimpleNamespace(
        lever=SimpleNamespace(present=False, enabled=False),
        greenhouse=SimpleNamespace(present=False, enabled=False),
        workable=SimpleNamespace(present=present, enabled=enabled),
        smartrecruiters=SimpleNamespace(present=False, enabled=False),
    )


def _database(path: Path) -> Path:
    with connect(path) as connection:
        initialize_schema(connection)
    return path


def _workable_config(path: Path, *, enabled: bool) -> Path:
    path.write_text(
        "version: 1\n"
        "sources:\n"
        "  workable:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    boards:\n"
        "      - subdomain: example-account\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _job() -> CollectedJob:
    return CollectedJob(
        source="workable",
        source_job_id="account:1",
        title="Workable role",
        company="Example",
        location=None,
        description=None,
        url=None,
        employment_type=None,
        remote_status=None,
        published_at=None,
        collected_at="2026-09-29T00:00:00+00:00",
    )


def test_workable_is_known_but_not_a_builtin() -> None:
    assert len(COLLECTORS) == 7
    assert tuple(KNOWN_SOURCES) == (
        *BUILT_IN_SOURCES,
        "lever",
        "greenhouse",
        "workable",
        "smartrecruiters",
    )
    assert _SOURCE_LABELS["workable"] == "Workable"


def test_zero_config_catalog_remains_exactly_seven() -> None:
    catalog = resolve_source_catalog(config=_config(present=False, enabled=False))
    assert catalog.active == BUILT_IN_SOURCES
    assert catalog.status == BUILT_IN_SOURCES
    assert catalog.statistics == BUILT_IN_SOURCES


def test_enabled_workable_is_appended_after_existing_configurable_sources() -> None:
    catalog = resolve_source_catalog(config=_config(present=True, enabled=True))
    assert catalog.active == (*BUILT_IN_SOURCES, "workable")
    assert catalog.status == (*BUILT_IN_SOURCES, "workable")
    assert catalog.statistics == (*BUILT_IN_SOURCES, "workable")


def test_disabled_workable_is_visible_but_not_active() -> None:
    catalog = resolve_source_catalog(config=_config(present=True, enabled=False))
    assert catalog.active == BUILT_IN_SOURCES
    assert catalog.status == (*BUILT_IN_SOURCES, "workable")
    assert catalog.statistics == (*BUILT_IN_SOURCES, "workable")


def test_workable_enabled_flows_through_stats_report_export_and_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "enabled.sqlite3")
    config = _workable_config(tmp_path / "sources.yaml", enabled=True)
    expected = (*BUILT_IN_SOURCES, "workable")
    with connect(database) as connection:
        catalog = resolve_source_catalog(connection, config_path=str(config))
        stats = compute_source_stats(connection, catalog=catalog)
        exported = build_source_stats_records(
            connection, ExportRequest(format="json", limit=20), config_path=str(config)
        )
        report = build_report(
            connection,
            now=datetime(2026, 9, 29, tzinfo=UTC),
            sources_config=str(config),
        )
    assert catalog.active == expected
    assert tuple(item.source for item in stats) == expected
    assert tuple(item["source"] for item in exported.records) == expected
    assert tuple(item.source for item in report.overview.source_stats) == expected

    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    monkeypatch.setenv("CAREEROS_SOURCES_CONFIG", str(config))
    from careeros.api import app
    from tests.support import TestClient

    with TestClient(app) as client:
        response = client.get("/sources/stats")
    assert response.status_code == 200
    assert tuple(item["source"] for item in response.json()["items"]) == expected


def test_workable_historical_jobs_remain_visible_after_config_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "historical.sqlite3")
    monkeypatch.delenv("CAREEROS_SOURCES_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config"))
    with connect(database) as connection:
        save_jobs(connection, [_job()])
        catalog = resolve_source_catalog(connection)
        statuses = build_statuses(
            connection, now=datetime(2026, 9, 29, tzinfo=UTC), catalog=catalog
        )
    assert catalog.active == BUILT_IN_SOURCES
    assert catalog.status[-1] == catalog.statistics[-1] == "workable"
    assert statuses[-1].configuration == "historical only"


def test_workable_run_history_only_has_selector_but_no_statistics_row(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "history-only.sqlite3")
    with connect(database) as connection:
        insert_run(
            connection,
            source="workable",
            started_at=datetime(2026, 9, 29, tzinfo=UTC),
        )
        catalog = resolve_source_catalog(connection)
        stats = compute_source_stats(connection, catalog=catalog)
    assert catalog.status[-1] == "workable"
    assert "workable" not in catalog.statistics
    assert all(item.source != "workable" for item in stats)
    assert (
        build_parser().parse_args(["collect", "history", "--source", "workable"]).source
        == "workable"
    )


def test_workable_reenabled_retains_recent_scheduling_state(tmp_path: Path) -> None:
    database = _database(tmp_path / "reenabled.sqlite3")
    config = _workable_config(tmp_path / "sources.yaml", enabled=True)
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    with connect(database) as connection:
        run_id = insert_run(
            connection,
            source="workable",
            started_at=now - timedelta(hours=1),
        )
        complete_run(
            connection,
            run_id=run_id,
            finished_at=now - timedelta(hours=1),
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
        catalog = resolve_source_catalog(connection, config_path=str(config))
        statuses = build_statuses(connection, now=now, catalog=catalog)
    assert statuses[-1].configuration == "enabled"
    assert statuses[-1].due is False
    assert statuses[-1].next_due is not None


def test_all_configurable_sources_keep_fixed_order() -> None:
    config = SimpleNamespace(
        lever=SimpleNamespace(present=True, enabled=False),
        greenhouse=SimpleNamespace(present=True, enabled=False),
        workable=SimpleNamespace(present=True, enabled=False),
        smartrecruiters=SimpleNamespace(present=False, enabled=False),
    )
    catalog = resolve_source_catalog(config=config)
    expected = (*BUILT_IN_SOURCES, "lever", "greenhouse", "workable")
    assert catalog.status == catalog.statistics == expected
