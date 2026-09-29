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
from careeros.source_config import SmartRecruitersConfig, SourceConfig
from careeros.source_stats.service import compute_source_stats
from careeros.sources import (
    BUILT_IN_SOURCES,
    CONFIGURABLE_SOURCES,
    resolve_source_catalog,
)
from careeros.status import build_statuses


def _config(*, present: bool, enabled: bool) -> SourceConfig:
    return SourceConfig(
        1,
        SimpleNamespace(present=False, enabled=False),
        None,
        False,
        SimpleNamespace(present=False, enabled=False),
        SimpleNamespace(present=False, enabled=False),
        SmartRecruitersConfig(present, enabled, ()),
    )


def _database(path: Path) -> Path:
    with connect(path) as connection:
        initialize_schema(connection)
    return path


def _smart_config(path: Path, *, enabled: bool) -> Path:
    path.write_text(
        "version: 1\nsources:\n"
        "  smartrecruiters:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    companies:\n"
        "      - identifier: example-company\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _job() -> CollectedJob:
    return CollectedJob(
        source="smartrecruiters",
        source_job_id="example-company:posting-id",
        title="SmartRecruiters role",
        company="Example",
        location=None,
        description=None,
        url=None,
        employment_type=None,
        remote_status=None,
        published_at=None,
        collected_at="2026-09-29T00:00:00+00:00",
    )


def test_smartrecruiters_is_known_after_existing_configurable_sources() -> None:
    assert CONFIGURABLE_SOURCES == (
        "lever",
        "greenhouse",
        "workable",
        "smartrecruiters",
    )
    assert "smartrecruiters" not in BUILT_IN_SOURCES
    assert _SOURCE_LABELS["smartrecruiters"] == "SmartRecruiters"


def test_smartrecruiters_enabled_is_active_and_visible() -> None:
    catalog = resolve_source_catalog(config=_config(present=True, enabled=True))
    assert catalog.active == (*BUILT_IN_SOURCES, "smartrecruiters")
    assert catalog.status == (*BUILT_IN_SOURCES, "smartrecruiters")
    assert catalog.statistics == (*BUILT_IN_SOURCES, "smartrecruiters")


def test_smartrecruiters_disabled_is_visible_but_inactive() -> None:
    catalog = resolve_source_catalog(config=_config(present=True, enabled=False))
    assert catalog.active == BUILT_IN_SOURCES
    assert catalog.status == (*BUILT_IN_SOURCES, "smartrecruiters")
    assert catalog.statistics == (*BUILT_IN_SOURCES, "smartrecruiters")


def test_zero_config_keeps_exact_seven_and_collectors_registry_is_frozen() -> None:
    assert tuple(COLLECTORS) == (
        "arbeitnow",
        "remoteok",
        "weworkremotely",
        "jobicy",
        "remotive",
        "workingnomads",
        "himalayas",
    )
    catalog = resolve_source_catalog(config=_config(present=False, enabled=False))
    assert catalog.active == catalog.status == catalog.statistics == BUILT_IN_SOURCES


def test_fixed_configurable_order_is_independent_of_yaml_order(tmp_path: Path) -> None:
    config = tmp_path / "all.yaml"
    config.write_text(
        "version: 1\nsources:\n"
        "  smartrecruiters: {enabled: false, companies: [{identifier: smart}]}\n"
        "  workable: {enabled: false, boards: [{subdomain: work}]}\n"
        "  greenhouse: {enabled: false, boards: [{token: green}]}\n"
        "  lever: {enabled: false, boards: [{id: lev, instance: global, "
        "company: L}]}\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    catalog = resolve_source_catalog(config_path=str(config))
    assert catalog.status == (
        *BUILT_IN_SOURCES,
        "lever",
        "greenhouse",
        "workable",
        "smartrecruiters",
    )


def test_smartrecruiters_historical_jobs_remain_visible_after_config_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert catalog.status[-1] == catalog.statistics[-1] == "smartrecruiters"
    assert statuses[-1].configuration == "historical only"


def test_smartrecruiters_run_history_only_has_selector_but_no_stats(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path / "history.sqlite3")
    with connect(database) as connection:
        insert_run(
            connection,
            source="smartrecruiters",
            started_at=datetime(2026, 9, 29, tzinfo=UTC),
        )
        catalog = resolve_source_catalog(connection)
        stats = compute_source_stats(connection, catalog=catalog)
    assert catalog.status[-1] == "smartrecruiters"
    assert "smartrecruiters" not in catalog.statistics
    assert all(item.source != "smartrecruiters" for item in stats)
    assert (
        build_parser()
        .parse_args(["collect", "history", "--source", "smartrecruiters"])
        .source
        == "smartrecruiters"
    )


def test_smartrecruiters_reenabled_retains_recent_schedule(tmp_path: Path) -> None:
    database = _database(tmp_path / "reenabled.sqlite3")
    config = _smart_config(tmp_path / "sources.yaml", enabled=True)
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    with connect(database) as connection:
        run_id = insert_run(
            connection,
            source="smartrecruiters",
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
    assert catalog.active[-1] == "smartrecruiters"
    assert statuses[-1].configuration == "enabled"
    assert statuses[-1].due is False


def test_smartrecruiters_inherits_stats_report_export_and_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path / "surfaces.sqlite3")
    config = _smart_config(tmp_path / "sources.yaml", enabled=True)
    expected = (*BUILT_IN_SOURCES, "smartrecruiters")
    with connect(database) as connection:
        catalog = resolve_source_catalog(connection, config_path=str(config))
        stats = compute_source_stats(connection, catalog=catalog)
        exported = build_source_stats_records(
            connection,
            ExportRequest(format="json", limit=20),
            config_path=str(config),
        )
        report = build_report(
            connection,
            now=datetime(2026, 9, 29, tzinfo=UTC),
            sources_config=str(config),
        )
    assert tuple(item.source for item in stats) == expected
    assert tuple(item["source"] for item in exported.records) == expected
    assert tuple(item.source for item in report.overview.source_stats) == expected
    assert tuple(item.source for item in report.health.per_source) == expected

    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    monkeypatch.setenv("CAREEROS_SOURCES_CONFIG", str(config))
    from careeros.api import app
    from tests.support import TestClient

    with TestClient(app) as client:
        response = client.get("/sources/stats")
    assert response.status_code == 200
    assert tuple(item["source"] for item in response.json()["items"]) == expected
