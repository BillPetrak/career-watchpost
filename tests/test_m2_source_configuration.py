"""Focused offline M2 Greenhouse configuration and catalog coverage."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.collection_policies import SOURCE_POLICIES
from careeros.database.connection import connect
from careeros.database.runs import complete_run, insert_run
from careeros.database.schema import initialize_schema
from careeros.source_config import (
    MAX_BOARDS,
    GreenhouseConfig,
    LeverConfig,
    SourceConfig,
    SourceConfigError,
    load_source_config,
)
from careeros.sources import BUILT_IN_SOURCES, resolve_source_catalog
from careeros.status import build_statuses


def _write_config(path: Path, body: str) -> Path:
    path.write_text("version: 1\nsources:\n" + body, encoding="utf-8")
    path.chmod(0o600)
    return path


def _greenhouse(
    path: Path, *, enabled: bool = True, boards: str = "  - token: acme"
) -> Path:
    return _write_config(
        path,
        "  greenhouse:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        + (
            "    boards:\n"
            + "\n".join("    " + line for line in boards.splitlines())
            + "\n"
            if boards
            else ""
        ),
    )


def _db(path: Path) -> sqlite3.Connection:
    connection = connect(path)
    initialize_schema(connection)
    return connection


def test_greenhouse_and_lever_parse_and_source_order_is_fixed(tmp_path: Path) -> None:
    config = _write_config(
        tmp_path / "sources.yaml",
        "  greenhouse:\n"
        "    enabled: true\n"
        "    boards:\n"
        "      - token: acme\n"
        "  lever:\n"
        "    enabled: true\n"
        "    boards:\n"
        "      - id: board\n"
        "        instance: global\n"
        "        company: Board\n",
    )
    parsed = load_source_config(config)
    assert parsed.version == 1
    assert parsed.greenhouse.boards[0].token == "acme"
    catalog = resolve_source_catalog(config=parsed)
    assert catalog.active == (*BUILT_IN_SOURCES, "lever", "greenhouse")


def test_source_config_positional_m1_compatibility() -> None:
    config = SourceConfig(1, LeverConfig(False, False, ()))
    assert config.greenhouse == GreenhouseConfig(False, False, ())


@pytest.mark.parametrize(
    "body",
    [
        "  greenhouse: {enabled: true, extra: 1}\n",
        "  greenhouse:\n    enabled: true\n    boards: [{token: acme, extra: 1}]\n",
        "  greenhouse:\n    enabled: 'yes'\n    boards: [{token: acme}]\n",
        "  greenhouse:\n    enabled: true\n",
    ],
)
def test_greenhouse_exact_schema_and_enabled_boards(tmp_path: Path, body: str) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(_write_config(tmp_path / "bad.yaml", body))


def test_disabled_greenhouse_may_omit_boards_and_retains_entries(
    tmp_path: Path,
) -> None:
    config = load_source_config(
        _greenhouse(tmp_path / "disabled.yaml", enabled=False, boards="")
    )
    assert config.greenhouse.present is True
    assert config.greenhouse.enabled is False
    retained = load_source_config(
        _greenhouse(tmp_path / "retained.yaml", enabled=False)
    )
    assert retained.greenhouse.boards[0].token == "acme"
    empty = load_source_config(
        _write_config(
            tmp_path / "empty.yaml",
            "  greenhouse:\n    enabled: false\n    boards: []\n",
        )
    )
    assert empty.greenhouse.boards == ()


@pytest.mark.parametrize(
    "token",
    ["", "ABC", "a_b", "a" * 101, "équipe", "-acme"],
)
def test_greenhouse_token_policy(tmp_path: Path, token: str) -> None:
    text = f"  greenhouse:\n    enabled: true\n    boards:\n      - token: {token!r}\n"
    with pytest.raises(SourceConfigError):
        load_source_config(_write_config(tmp_path / "bad.yaml", text))


@pytest.mark.parametrize("token_yaml", ["1", "true", "[]", "{}"])
def test_greenhouse_token_must_be_a_string(tmp_path: Path, token_yaml: str) -> None:
    text = (
        f"  greenhouse:\n    enabled: true\n    boards:\n      - token: {token_yaml}\n"
    )
    with pytest.raises(SourceConfigError):
        load_source_config(_write_config(tmp_path / "bad-type.yaml", text))


def test_greenhouse_token_length_boundaries_are_accepted(tmp_path: Path) -> None:
    for index, token in enumerate(("a", "a" * 100)):
        text = (
            f"  greenhouse:\n    enabled: true\n    boards:\n      - token: {token}\n"
        )
        parsed = load_source_config(
            _write_config(tmp_path / f"boundary-{index}.yaml", text)
        )
        assert parsed.greenhouse.boards[0].token == token


def test_greenhouse_token_is_trimmed_and_duplicate_canonical_tokens_rejected(
    tmp_path: Path,
) -> None:
    path = _write_config(
        tmp_path / "sources.yaml",
        "  greenhouse:\n    enabled: true\n    boards:\n      - token: '  acme  '\n",
    )
    assert load_source_config(path).greenhouse.boards[0].token == "acme"
    duplicate = _write_config(
        tmp_path / "duplicate.yaml",
        "  greenhouse:\n"
        "    enabled: true\n"
        "    boards:\n"
        "      - token: acme\n"
        "      - token: ' acme '\n",
    )
    with pytest.raises(SourceConfigError, match="duplicate"):
        load_source_config(duplicate)


def test_greenhouse_board_limit_and_zero_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boards = "\n".join(f"      - token: board-{i}" for i in range(MAX_BOARDS))
    accepted = load_source_config(
        _write_config(
            tmp_path / "accepted.yaml",
            f"  greenhouse:\n    enabled: true\n    boards:\n{boards}\n",
        )
    )
    assert len(accepted.greenhouse.boards) == 8
    too_many = "\n".join(f"      - token: board-{i}" for i in range(MAX_BOARDS + 1))
    with pytest.raises(SourceConfigError):
        load_source_config(
            _write_config(
                tmp_path / "too-many.yaml",
                f"  greenhouse:\n    enabled: true\n    boards:\n{too_many}\n",
            )
        )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing"))
    assert resolve_source_catalog().active == BUILT_IN_SOURCES
    assert resolve_source_catalog().status == BUILT_IN_SOURCES


def test_greenhouse_historical_and_removed_states(tmp_path: Path) -> None:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    database = tmp_path / "history.sqlite3"
    with _db(database) as connection:
        connection.execute(
            "INSERT INTO jobs(source, source_job_id, title, collected_at) "
            "VALUES (?, ?, ?, ?)",
            ("greenhouse", "acme:1", "Job", now.isoformat()),
        )
        connection.commit()
        catalog = resolve_source_catalog(
            connection, config=SourceConfig(1, LeverConfig(False, False, ()))
        )
        assert catalog.status[-1] == "greenhouse"
        assert catalog.statistics[-1] == "greenhouse"

    runs_only = tmp_path / "runs.sqlite3"
    with _db(runs_only) as connection:
        run_id = insert_run(connection, source="greenhouse", started_at=now)
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
        catalog = resolve_source_catalog(connection)
        assert catalog.status[-1] == "greenhouse"
        assert catalog.statistics == BUILT_IN_SOURCES


def test_greenhouse_disabled_and_reenable_scheduling(tmp_path: Path) -> None:
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    database = tmp_path / "schedule.sqlite3"
    disabled = _greenhouse(tmp_path / "disabled.yaml", enabled=False)
    enabled = _greenhouse(tmp_path / "enabled.yaml")
    with _db(database) as connection:
        status = build_statuses(connection, now=now, config_path=str(disabled))[-1]
        assert status.state == "disabled" and not status.due and status.next_due is None
        status = build_statuses(connection, now=now, config_path=str(enabled))[-1]
        assert status.state == "active" and status.due and status.next_due is None
        run_id = insert_run(
            connection, source="greenhouse", started_at=now - timedelta(hours=1)
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
        status = build_statuses(connection, now=now, config_path=str(enabled))[-1]
        assert status.state == "active" and not status.due
        assert status.next_due == now + timedelta(hours=5)
        insert_run(connection, source="greenhouse", started_at=now)
        status = build_statuses(connection, now=now, config_path=str(enabled))[-1]
        assert status.due and status.next_due is None
        assert SOURCE_POLICIES["greenhouse"].min_interval == timedelta(hours=6)
