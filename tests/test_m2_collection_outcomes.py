"""Focused M2 Greenhouse orchestration and persistence regressions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from careeros.cli.commands import collect as collect_command
from careeros.collecting import collect_all, collect_due, collect_one
from careeros.collection_policies import SourcePolicy
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.database.connection import connect
from careeros.database.runs import latest_attempt_is_incomplete
from careeros.database.schema import initialize_schema
from careeros.source_config import (
    GreenhouseBoard,
    GreenhouseConfig,
    LeverConfig,
    SourceConfig,
)


def _job(source: str, source_job_id: str, *, title: str = "Role") -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Company",
        location=None,
        description=None,
        url=None,
        employment_type=None,
        remote_status=None,
        published_at=None,
        collected_at="2026-09-29T00:00:00+00:00",
    )


def _greenhouse_config() -> SourceConfig:
    return SourceConfig(
        version=1,
        lever=LeverConfig(False, False, ()),
        greenhouse=GreenhouseConfig(
            True,
            True,
            (GreenhouseBoard("board"),),
        ),
    )


def test_direct_greenhouse_partial_failure_persists_truthful_counts(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "direct.sqlite3"
    outcome = CollectorOutcome(
        (_job("greenhouse", "healthy:1"),),
        succeeded=False,
        error="failed: OSError: offline",
    )
    monkeypatch.setattr(
        collect_command,
        "configured_collectors",
        lambda config: {"greenhouse": lambda **_: outcome},
    )

    assert (
        collect_command.run_collect(
            "greenhouse",
            2,
            database,
            config=_greenhouse_config(),
        )
        == 1
    )
    assert "Collection failed: failed: OSError: offline" in capsys.readouterr().err

    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        run = connection.execute(
            "SELECT succeeded, fetched, inserted, updated, error "
            "FROM collection_runs WHERE source = 'greenhouse'"
        ).fetchone()
    assert tuple(run) == (0, 1, 1, 0, "failed: OSError: offline")


@pytest.mark.parametrize("operation", [collect_all, collect_due])
def test_partial_greenhouse_outcome_continues_later_active_provider(
    tmp_path,
    operation: Callable[..., object],
) -> None:
    database = tmp_path / f"{operation.__name__}.sqlite3"
    collectors = {
        "greenhouse": lambda **_: CollectorOutcome(
            (_job("greenhouse", "healthy:1"),),
            succeeded=False,
            error="board failed",
        ),
        "later": lambda **_: [_job("later", "later:1")],
    }
    keywords: dict[str, object] = {
        "limit": 2,
        "database_path": database,
        "collectors": collectors,
        "order": ("greenhouse", "later"),
    }
    if operation is collect_due:
        keywords["policies"] = {
            "greenhouse": SourcePolicy("greenhouse", timedelta(hours=6)),
            "later": SourcePolicy("later", timedelta(hours=6)),
        }
        keywords["clock"] = lambda: datetime(2026, 9, 29, tzinfo=UTC)

    results = list(operation(**keywords))

    assert [result.source for result in results] == ["greenhouse", "later"]
    assert [result.succeeded for result in results] == [False, True]
    assert [
        (result.fetched, result.inserted, result.updated) for result in results
    ] == [
        (1, 1, 0),
        (1, 1, 0),
    ]
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        runs = connection.execute(
            "SELECT source, succeeded FROM collection_runs ORDER BY id"
        ).fetchall()
    assert [tuple(run) for run in runs] == [("greenhouse", 0), ("later", 1)]


def test_greenhouse_update_counts_remain_truthful_on_failed_outcome(tmp_path) -> None:
    database = tmp_path / "updates.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        first = collect_one(
            connection,
            source="greenhouse",
            collectors={
                "greenhouse": lambda **_: CollectorOutcome(
                    (_job("greenhouse", "board:1", title="First"),),
                    False,
                    "board failed",
                )
            },
            limit=1,
        )
        second = collect_one(
            connection,
            source="greenhouse",
            collectors={
                "greenhouse": lambda **_: CollectorOutcome(
                    (_job("greenhouse", "board:1", title="Updated"),),
                    False,
                    "board failed again",
                )
            },
            limit=1,
        )

    assert (first.fetched, first.inserted, first.updated) == (1, 1, 0)
    assert (second.fetched, second.inserted, second.updated) == (1, 0, 1)
    assert not first.succeeded and not second.succeeded


def test_greenhouse_programming_error_propagates_and_preserves_incomplete_marker(
    tmp_path,
) -> None:
    database = tmp_path / "crash.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)

        def broken(**_: object) -> list[CollectedJob]:
            raise TypeError("programming bug")

        with pytest.raises(TypeError, match="programming bug"):
            collect_one(
                connection,
                source="greenhouse",
                collectors={"greenhouse": broken},
                limit=1,
            )
        assert latest_attempt_is_incomplete(connection, "greenhouse")
