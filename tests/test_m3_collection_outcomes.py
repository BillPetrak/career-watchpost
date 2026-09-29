"""Focused M3 orchestration regressions (no network access)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from careeros.collecting import collect_all, collect_due, collect_one
from careeros.collection_policies import SourcePolicy
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.database.connection import connect
from careeros.database.runs import latest_attempt_is_incomplete
from careeros.database.schema import initialize_schema


def _job(source: str, identity: str, *, title: str = "Role") -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=identity,
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


def test_partial_workable_outcome_persists_healthy_jobs_and_counts(tmp_path) -> None:
    database = tmp_path / "workable.sqlite3"
    outcome = CollectorOutcome(
        (_job("workable", "account:1"),), False, "account: OSError: offline"
    )
    with connect(database) as connection:
        initialize_schema(connection)
    with connect(database) as connection:
        result = collect_one(
            connection,
            source="workable",
            limit=4,
            collectors={"workable": lambda **_: outcome},
            clock=lambda: datetime(2026, 9, 29, tzinfo=UTC),
        )
        assert result.succeeded is False
        assert result.fetched == result.inserted == 1
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_collect_all_continues_after_workable_partial_failure(tmp_path) -> None:
    database = tmp_path / "all.sqlite3"
    outcome = CollectorOutcome((_job("workable", "account:1"),), False, "bad")
    results = list(
        collect_all(
            limit=4,
            database_path=database,
            order=("workable", "later"),
            collectors={
                "workable": lambda **_: outcome,
                "later": lambda **_: [_job("later", "2")],
            },
        )
    )
    assert [item.source for item in results] == ["workable", "later"]
    assert [item.succeeded for item in results] == [False, True]


@pytest.mark.parametrize("operation", [collect_all, collect_due])
def test_workable_failure_continues_later_active_source_and_counts(
    tmp_path, operation: Callable[..., object]
) -> None:
    database = tmp_path / f"{operation.__name__}-counts.sqlite3"
    collectors = {
        "workable": lambda **_: CollectorOutcome(
            (_job("workable", "account:1"),), False, "account: OSError: offline"
        ),
        "later": lambda **_: [_job("later", "later:1")],
    }
    arguments: dict[str, object] = {
        "limit": 4,
        "database_path": database,
        "collectors": collectors,
        "order": ("workable", "later"),
    }
    if operation is collect_due:
        arguments["policies"] = {
            source: SourcePolicy(source, timedelta(hours=6))
            for source in ("workable", "later")
        }
        arguments["clock"] = lambda: datetime(2026, 9, 29, tzinfo=UTC)
    results = list(operation(**arguments))
    assert [result.source for result in results] == ["workable", "later"]
    assert [
        (result.fetched, result.inserted, result.updated, result.succeeded)
        for result in results
    ] == [(1, 1, 0, False), (1, 1, 0, True)]


def test_repeat_workable_update_reports_truthful_updated_count(tmp_path) -> None:
    database = tmp_path / "repeat-update.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        first = collect_one(
            connection,
            source="workable",
            limit=1,
            collectors={"workable": lambda **_: [_job("workable", "a:1")]},
        )
        second = collect_one(
            connection,
            source="workable",
            limit=1,
            collectors={
                "workable": lambda **_: [_job("workable", "a:1", title="Updated")]
            },
        )
    assert (first.fetched, first.inserted, first.updated) == (1, 1, 0)
    assert (second.fetched, second.inserted, second.updated) == (1, 0, 1)


def test_workable_programming_error_propagates_and_marker_stays_incomplete(
    tmp_path,
) -> None:
    database = tmp_path / "programming-error.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)

        def broken(**_: object) -> list[CollectedJob]:
            raise TypeError("unexpected programming error")

        with pytest.raises(TypeError, match="unexpected programming error"):
            collect_one(
                connection,
                source="workable",
                limit=1,
                collectors={"workable": broken},
            )
        assert latest_attempt_is_incomplete(connection, "workable")
