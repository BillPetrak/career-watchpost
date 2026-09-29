"""Focused shared-surface integration checks for M2 Greenhouse."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from careeros.cli.commands.collect import run_collect_command
from careeros.cli.parser import build_parser
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.export.models import ExportRequest
from careeros.export.service import build_source_stats_records
from careeros.reporting.service import build_report
from careeros.source_stats.service import compute_source_stats
from careeros.sources import BUILT_IN_SOURCES, resolve_source_catalog


def _greenhouse_config(path: Path, *, enabled: bool) -> Path:
    path.write_text(
        "version: 1\n"
        "sources:\n"
        "  greenhouse:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    boards:\n"
        "      - token: example-company\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _database(path: Path) -> Path:
    with connect(path) as connection:
        initialize_schema(connection)
    return path


def test_disabled_greenhouse_flows_through_stats_export_report_and_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "careeros.sqlite3")
    config = _greenhouse_config(tmp_path / "sources.yaml", enabled=False)
    expected = (*BUILT_IN_SOURCES, "greenhouse")

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
    assert report.health.per_source[-1].configuration == "disabled"
    assert report.health.per_source[-1].due is False

    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    monkeypatch.setenv("CAREEROS_SOURCES_CONFIG", str(config))
    from careeros.api import app
    from tests.support import TestClient

    with TestClient(app) as client:
        response = client.get("/sources/stats")
    assert response.status_code == 200
    assert tuple(item["source"] for item in response.json()["items"]) == expected


def test_zero_config_shared_surfaces_remain_exactly_the_released_seven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _database(tmp_path / "zero.sqlite3")
    monkeypatch.delenv("CAREEROS_SOURCES_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config"))
    expected = BUILT_IN_SOURCES

    with connect(database) as connection:
        catalog = resolve_source_catalog(connection)
        stats = compute_source_stats(connection, catalog=catalog)
        exported = build_source_stats_records(
            connection,
            ExportRequest(format="json", limit=20),
        )
        report = build_report(
            connection,
            now=datetime(2026, 9, 29, tzinfo=UTC),
        )

    assert catalog.active == expected
    assert catalog.status == expected
    assert catalog.statistics == expected
    assert tuple(item.source for item in stats) == expected
    assert tuple(item["source"] for item in exported.records) == expected
    assert tuple(item.source for item in report.overview.source_stats) == expected
    assert tuple(item.source for item in report.health.per_source) == expected

    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    from careeros.api import app
    from tests.support import TestClient

    with TestClient(app) as client:
        response = client.get("/sources/stats")
    assert response.status_code == 200
    assert response.json()["total"] == len(BUILT_IN_SOURCES) == 7
    assert tuple(item["source"] for item in response.json()["items"]) == expected


def test_known_greenhouse_disabled_direct_collect_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    history = build_parser().parse_args(
        ["collect", "history", "--source", "greenhouse"]
    )
    assert history.source == "greenhouse"

    config = _greenhouse_config(tmp_path / "disabled.yaml", enabled=False)
    database = tmp_path / "must-not-exist.sqlite3"
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "collect",
            "greenhouse",
            "--sources-config",
            str(config),
            "--database",
            str(database),
        ]
    )
    assert run_collect_command(arguments, parser) == 1
    assert not database.exists()
    assert "Greenhouse is not enabled" in capsys.readouterr().err
