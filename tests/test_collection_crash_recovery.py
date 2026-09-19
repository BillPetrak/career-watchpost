"""Fault-injection coverage for collection commit and restart semantics."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros import collecting
from careeros.collecting import collect_all, collect_due
from careeros.collection_policies import SourcePolicy
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.runs import insert_run
from careeros.database.schema import initialize_schema

_NOW = datetime(2026, 9, 18, 12, tzinfo=UTC)


def _job(source_job_id: str = "restart-1") -> CollectedJob:
    return CollectedJob(
        source="jobicy",
        source_job_id=source_job_id,
        title="Python Engineer",
        company="Example",
        location="Remote",
        description="Build systems.",
        url=f"https://example.invalid/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at=_NOW.isoformat(),
        collected_at=_NOW.isoformat(),
    )


def _collector(*, limit: int = 25, timeout: float = 30.0) -> list[CollectedJob]:
    return [_job("restart-1"), _job("restart-2")][:limit]


def test_grouping_failure_rolls_back_jobs_and_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "careeros.sqlite3"

    def fail_grouping(connection: sqlite3.Connection, *, commit: bool = True) -> None:
        raise sqlite3.OperationalError("fault after upserts")

    monkeypatch.setattr(collecting, "ensure_grouped", fail_grouping)
    results = list(
        collect_all(
            limit=2,
            database_path=database,
            collectors={"jobicy": _collector},
        )
    )
    assert results[0].succeeded is False
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = connection.execute(
            "SELECT finished_at, succeeded FROM collection_runs"
        ).fetchone()
        assert run["finished_at"] is not None
        assert run["succeeded"] == 0


def test_process_crash_leaves_only_marker_then_restart_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "careeros.sqlite3"
    real_save_jobs = collecting.save_jobs

    def crash_after_upserts(
        connection: sqlite3.Connection,
        jobs: list[CollectedJob],
        *,
        commit: bool = True,
    ) -> None:
        real_save_jobs(connection, jobs, commit=False)
        raise KeyboardInterrupt

    monkeypatch.setattr(collecting, "save_jobs", crash_after_upserts)
    with pytest.raises(KeyboardInterrupt):
        list(
            collect_all(
                limit=1,
                database_path=database,
                collectors={"jobicy": _collector},
            )
        )
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        run = connection.execute(
            "SELECT finished_at, succeeded FROM collection_runs"
        ).fetchone()
        assert run["finished_at"] is None
        assert run["succeeded"] is None

    monkeypatch.setattr(collecting, "save_jobs", real_save_jobs)
    results = list(
        collect_due(
            limit=2,
            database_path=database,
            collectors={"jobicy": _collector},
            policies={"jobicy": SourcePolicy("jobicy", timedelta(hours=1), True)},
            order=("jobicy",),
            clock=lambda: _NOW,
        )
    )
    assert results[0].succeeded is True
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM job_group_members").fetchone()[0]
            == 2
        )
        runs = connection.execute(
            "SELECT finished_at, succeeded FROM collection_runs ORDER BY id"
        ).fetchall()
        assert runs[0]["finished_at"] is None
        assert runs[1]["succeeded"] == 1


def test_crash_before_success_bookkeeping_rolls_back_jobs_and_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "careeros.sqlite3"

    def crash_before_success(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(collecting, "complete_run", crash_before_success)
    with pytest.raises(KeyboardInterrupt):
        list(
            collect_all(
                limit=2,
                database_path=database,
                collectors={"jobicy": _collector},
            )
        )

    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM job_group_members").fetchone()[0]
            == 0
        )
        run = connection.execute(
            "SELECT finished_at, succeeded FROM collection_runs"
        ).fetchone()
        assert run["finished_at"] is None
        assert run["succeeded"] is None


def test_recent_completed_run_does_not_delay_newer_incomplete_retry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "careeros.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        completed = insert_run(
            connection, source="jobicy", started_at=_NOW - timedelta(minutes=5)
        )
        collecting.complete_run(
            connection,
            run_id=completed,
            finished_at=_NOW - timedelta(minutes=4),
            succeeded=True,
            fetched=0,
            inserted=0,
            updated=0,
            error=None,
        )
        insert_run(connection, source="jobicy", started_at=_NOW - timedelta(minutes=1))

    results = list(
        collect_due(
            limit=1,
            database_path=database,
            collectors={"jobicy": _collector},
            policies={"jobicy": SourcePolicy("jobicy", timedelta(hours=1), True)},
            order=("jobicy",),
            clock=lambda: _NOW,
        )
    )
    assert [result.source for result in results] == ["jobicy"]


def test_restart_repairs_pre_hardening_partial_grouping_even_if_fetch_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "careeros.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        save_jobs(connection, [_job("legacy-1"), _job("legacy-2")])
        insert_run(connection, source="jobicy", started_at=_NOW - timedelta(minutes=1))

    def failed_collector(
        *, limit: int = 25, timeout: float = 30.0
    ) -> list[CollectedJob]:
        raise RuntimeError("network unavailable")

    results = list(
        collect_due(
            limit=2,
            database_path=database,
            collectors={"jobicy": failed_collector},
            policies={"jobicy": SourcePolicy("jobicy", timedelta(hours=1), True)},
            order=("jobicy",),
            clock=lambda: _NOW,
        )
    )
    assert results[0].succeeded is False
    with connect(database) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM job_group_members").fetchone()[0]
            == 2
        )
