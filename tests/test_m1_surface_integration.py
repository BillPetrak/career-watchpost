"""Focused M1 surface integration checks (offline and temporary-state only)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.cli.commands.collect import run_collect_command
from careeros.cli.commands.report import run_report
from careeros.cli.commands.source import run_source_stats
from careeros.cli.parser import build_parser
from careeros.database.connection import connect
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import initialize_schema
from careeros.export.models import ExportRequest
from careeros.export.service import build_source_stats_records
from careeros.source_config import SourceConfigError, load_source_config
from careeros.source_stats.service import compute_source_stats, empty_source_stats
from careeros.sources import BUILT_IN_SOURCES, resolve_source_catalog
from careeros.status import build_statuses


def _config(path: Path, *, enabled: bool = True) -> Path:
    path.write_text(
        "version: 1\n"
        "sources:\n"
        "  lever:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    boards:\n"
        "      - id: appfollow\n"
        "        instance: global\n"
        "        company: AppFollow\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _db(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    initialize_schema(connection)
    return connection


def test_zero_config_is_exactly_the_released_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    catalog = resolve_source_catalog()
    assert catalog.active == BUILT_IN_SOURCES
    assert catalog.status == BUILT_IN_SOURCES
    assert catalog.statistics == BUILT_IN_SOURCES
    assert (
        tuple(item.source for item in empty_source_stats(catalog=catalog))
        == BUILT_IN_SOURCES
    )


def test_enabled_and_disabled_config_share_deterministic_order(tmp_path: Path) -> None:
    path = _config(tmp_path / "sources.yaml")
    enabled = load_source_config(path)
    assert resolve_source_catalog(config=enabled).active == (*BUILT_IN_SOURCES, "lever")
    disabled_path = _config(tmp_path / "disabled.yaml", enabled=False)
    disabled = load_source_config(disabled_path)
    catalog = resolve_source_catalog(config=disabled)
    assert catalog.active == BUILT_IN_SOURCES
    assert catalog.status == (*BUILT_IN_SOURCES, "lever")
    assert catalog.statistics == (*BUILT_IN_SOURCES, "lever")


def test_historical_lever_is_visible_without_current_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    database = tmp_path / "careeros.sqlite3"
    with _db(database) as connection:
        insert_run(
            connection,
            source="lever",
            started_at=datetime.now(UTC),
        )
        catalog = resolve_source_catalog(connection)
        assert catalog.status[-1] == "lever"
        assert catalog.statistics == BUILT_IN_SOURCES


def test_source_stats_and_export_use_same_configured_set(tmp_path: Path) -> None:
    config = _config(tmp_path / "sources.yaml", enabled=False)
    database = tmp_path / "careeros.sqlite3"
    with _db(database) as connection:
        catalog = resolve_source_catalog(connection, config_path=str(config))
        stats = compute_source_stats(connection, catalog=catalog)
        exported = build_source_stats_records(
            connection,
            ExportRequest(format="json", limit=20),
            config_path=str(config),
        )
    assert tuple(item.source for item in stats) == (*BUILT_IN_SOURCES, "lever")
    assert tuple(item["source"] for item in exported.records) == (
        *BUILT_IN_SOURCES,
        "lever",
    )


def test_disabled_status_is_inactive_and_not_due(tmp_path: Path) -> None:
    config = _config(tmp_path / "sources.yaml", enabled=False)
    database = tmp_path / "careeros.sqlite3"
    with _db(database) as connection:
        statuses = build_statuses(
            connection,
            now=datetime.now(UTC),
            config_path=str(config),
        )
    lever = statuses[-1]
    assert lever.source == "lever"
    assert lever.configuration == "disabled"
    assert lever.state == "disabled"
    assert lever.due is False
    assert lever.next_due is None


def test_history_selector_accepts_lever_without_config() -> None:
    arguments = build_parser().parse_args(["collect", "history", "--source", "lever"])
    assert arguments.source == "lever"
    assert not hasattr(arguments, "sources_config")


def test_invalid_direct_lever_config_is_rejected_before_database_setup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("version: 1\nsources: [bad]\n", encoding="utf-8")
    config.chmod(0o600)
    database = tmp_path / "never-created.sqlite3"
    arguments = build_parser().parse_args(
        [
            "collect",
            "lever",
            "--sources-config",
            str(config),
            "--database",
            str(database),
        ]
    )
    assert run_collect_command(arguments, build_parser()) == 1
    assert not database.exists()
    assert "Collection failed" in capsys.readouterr().err


def test_missing_explicit_config_is_fail_closed() -> None:
    with pytest.raises(SourceConfigError):
        load_source_config("/definitely/missing/careeros-sources.yaml")


def test_source_stats_cli_reports_malformed_config_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("not: [valid", encoding="utf-8")
    config.chmod(0o600)
    result = run_source_stats(
        source=None,
        database_path=tmp_path / "missing.sqlite3",
        output_format="text",
        sources_config=config,
    )
    assert result == 1
    assert "Source stats failed" in capsys.readouterr().err


def test_report_cli_reports_malformed_config_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("not: [valid", encoding="utf-8")
    config.chmod(0o600)
    database = tmp_path / "report.sqlite3"
    with _db(database):
        pass
    arguments = build_parser().parse_args(
        [
            "report",
            "--database",
            str(database),
            "--sources-config",
            str(config),
        ]
    )
    assert run_report(arguments, build_parser()) == 1
    assert "Report failed" in capsys.readouterr().err


def test_http_source_stats_reports_invalid_config_as_controlled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from careeros.api import app
    from tests.support import TestClient

    database = tmp_path / "http.sqlite3"
    with _db(database):
        pass
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "careeros").mkdir()
    config = config_home / "careeros" / "sources.yaml"
    config.write_text("not: [valid", encoding="utf-8")
    config.chmod(0o600)
    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    with TestClient(app) as client:
        response = client.get("/sources/stats")
    assert response.status_code == 500
    assert response.json()["detail"].startswith("Invalid source configuration:")


def test_removed_runs_only_do_not_fabricate_quality_row(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    with _db(database) as connection:
        run_id = insert_run(connection, source="lever", started_at=datetime.now(UTC))
        complete_run(
            connection,
            run_id=run_id,
            finished_at=datetime.now(UTC),
            succeeded=False,
            fetched=0,
            inserted=0,
            updated=0,
            error="offline fixture",
        )
        catalog = resolve_source_catalog(connection)
        assert catalog.status[-1] == "lever"
        assert catalog.statistics == BUILT_IN_SOURCES


def test_recent_success_failure_and_incomplete_lever_scheduling(tmp_path: Path) -> None:
    config = _config(tmp_path / "sources.yaml")
    database = tmp_path / "careeros.sqlite3"
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    with _db(database) as connection:
        run_id = insert_run(
            connection, source="lever", started_at=now - timedelta(hours=1)
        )
        complete_run(
            connection,
            run_id=run_id,
            finished_at=now,
            succeeded=False,
            fetched=0,
            inserted=0,
            updated=0,
            error="fixture",
        )
        status = build_statuses(connection, now=now, config_path=str(config))[-1]
        assert status.due is False
        assert status.next_due == now + timedelta(hours=5)
        run_id = insert_run(connection, source="lever", started_at=now)
        assert run_id
        status = build_statuses(connection, now=now, config_path=str(config))[-1]
        assert status.due is True

    expired = tmp_path / "expired.sqlite3"
    with _db(expired) as connection:
        run_id = insert_run(
            connection, source="lever", started_at=now - timedelta(hours=7)
        )
        complete_run(
            connection,
            run_id=run_id,
            finished_at=now,
            succeeded=True,
            fetched=1,
            inserted=1,
            updated=0,
            error=None,
        )
        assert (
            build_statuses(connection, now=now, config_path=str(config))[-1].due is True
        )


def test_configured_disabled_status_retains_history_fields(tmp_path: Path) -> None:
    config = _config(tmp_path / "sources.yaml", enabled=False)
    database = tmp_path / "careeros.sqlite3"
    with _db(database) as connection:
        run_id = insert_run(connection, source="lever", started_at=datetime.now(UTC))
        complete_run(
            connection,
            run_id=run_id,
            finished_at=datetime.now(UTC),
            succeeded=True,
            fetched=3,
            inserted=2,
            updated=1,
            error=None,
        )
        status = build_statuses(
            connection, now=datetime.now(UTC), config_path=str(config)
        )[-1]
    assert status.configuration == "disabled"
    assert status.state == "disabled"
    assert status.next_due is None
    assert (status.last_fetched, status.last_inserted, status.last_updated) == (3, 2, 1)
