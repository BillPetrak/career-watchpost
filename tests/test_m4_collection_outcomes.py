from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.collecting import collect_all, collect_due, collect_one
from careeros.collection_policies import SourcePolicy
from careeros.collectors import smartrecruiters
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from careeros.source_config import SmartRecruitersCompany, SmartRecruitersConfig


def _config(*names: str) -> SmartRecruitersConfig:
    return SmartRecruitersConfig(
        True, True, tuple(SmartRecruitersCompany(name) for name in names)
    )


def _detail(identifier: str) -> dict[str, object]:
    return {
        "uuid": "123e4567-e89b-12d3-a456-426614174000",
        "name": "Role",
        "company": {"name": "Company", "identifier": identifier},
    }


_NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def _job(
    source: str = "smartrecruiters",
    source_job_id: str = "acme:one",
    title: str = "Role",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Company",
        location="Berlin",
        description="Description",
        url="https://example.test/job",
        employment_type="Full-time",
        remote_status="remote",
        published_at=_NOW.isoformat(),
        collected_at=_NOW.isoformat(),
    )


def _clock() -> Callable[[], datetime]:
    return lambda: _NOW


def test_partial_outcome_preserves_healthy_company_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, *, timeout: float) -> object:
        identifier = url.split("/companies/")[1].split("/")[0]
        if "?" in url:
            if identifier == "bad":
                raise OSError("offline")
            return {
                "limit": 2,
                "offset": 0,
                "totalFound": 1,
                "content": [{"uuid": "123e4567-e89b-12d3-a456-426614174000"}],
            }
        return _detail(identifier)

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("bad", "good"), limit=4, sleeper=lambda _: None
    )
    assert not result.succeeded
    assert [job.source_job_id for job in result.jobs] == [
        "good:123e4567-e89b-12d3-a456-426614174000"
    ]
    assert result.error == "bad: OSError: offline"


def test_programming_exceptions_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(url: str, *, timeout: float) -> object:
        raise TypeError("collector bug")

    monkeypatch.setattr(smartrecruiters, "fetch_json", broken)
    with pytest.raises(TypeError, match="collector bug"):
        smartrecruiters.fetch_result(config=_config("broken"), sleeper=lambda _: None)


def test_fetch_jobs_rejects_partial_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smartrecruiters,
        "fetch_json",
        lambda url, *, timeout: (_ for _ in ()).throw(OSError("offline")),
    )
    with pytest.raises(RuntimeError, match="offline"):
        smartrecruiters.fetch_jobs(config=_config("bad"))


def test_collect_one_persists_healthy_partial_and_reports_truthful_counts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "careeros.sqlite3"
    jobs = [_job()]
    outcomes = iter(
        [
            CollectorOutcome(
                tuple(jobs), succeeded=False, error="bad: OSError: offline"
            ),
            CollectorOutcome(
                (_job(title="Changed"),), succeeded=False, error="partial"
            ),
        ]
    )

    def collector(*, limit: int) -> CollectorOutcome:
        return next(outcomes)

    with connect(database) as connection:
        initialize_schema(connection)
        first = collect_one(
            connection,
            source="smartrecruiters",
            collectors={"smartrecruiters": collector},
            limit=25,
            clock=_clock(),
        )
        second = collect_one(
            connection,
            source="smartrecruiters",
            collectors={"smartrecruiters": collector},
            limit=25,
            clock=_clock(),
        )
        assert (first.fetched, first.inserted, first.updated, first.succeeded) == (
            1,
            1,
            0,
            False,
        )
        assert (second.fetched, second.inserted, second.updated, second.succeeded) == (
            1,
            0,
            1,
            False,
        )
        run_rows = connection.execute(
            "SELECT finished_at, succeeded, fetched, inserted, updated "
            "FROM collection_runs "
            "ORDER BY id"
        ).fetchall()
        assert all(row[0] is not None for row in run_rows)
        assert [(row[1], row[2], row[3], row[4]) for row in run_rows] == [
            (0, 1, 1, 0),
            (0, 1, 0, 1),
        ]
        assert connection.execute("SELECT title FROM jobs").fetchone()[0] == "Changed"


def test_collect_all_continues_after_smartrecruiters_partial(
    tmp_path: Path,
) -> None:
    database = tmp_path / "careeros.sqlite3"

    def smart(*, limit: int) -> CollectorOutcome:
        return CollectorOutcome((_job(),), succeeded=False, error="partial")

    def later(*, limit: int) -> list[CollectedJob]:
        return [_job(source="later", source_job_id="later:one")]

    results = list(
        collect_all(
            limit=25,
            database_path=database,
            collectors={"smartrecruiters": smart, "later": later},
            order=("smartrecruiters", "later"),
            clock=_clock(),
        )
    )
    assert [result.source for result in results] == ["smartrecruiters", "later"]
    assert results[0].succeeded is False and results[1].succeeded is True
    with connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_collect_due_continues_after_smartrecruiters_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "careeros.sqlite3"

    def smart(*, limit: int) -> CollectorOutcome:
        raise OSError("offline")

    def later(*, limit: int) -> list[CollectedJob]:
        return [_job(source="later", source_job_id="later:one")]

    policies = {
        source: SourcePolicy(source, timedelta(hours=1))
        for source in ("smartrecruiters", "later")
    }
    results = list(
        collect_due(
            limit=25,
            database_path=database,
            collectors={"smartrecruiters": smart, "later": later},
            policies=policies,
            order=("smartrecruiters", "later"),
            clock=_clock(),
        )
    )
    assert [result.source for result in results] == ["smartrecruiters", "later"]
    assert results[0].succeeded is False and results[1].succeeded is True


@pytest.mark.parametrize("error", [TypeError("bug"), AttributeError("bug")])
def test_programming_failure_leaves_incomplete_run_marker(
    tmp_path: Path, error: Exception
) -> None:
    database = tmp_path / "careeros.sqlite3"

    def broken(*, limit: int) -> list[CollectedJob]:
        raise error

    with pytest.raises(type(error)):
        list(
            collect_all(
                limit=25,
                database_path=database,
                collectors={"smartrecruiters": broken},
                clock=_clock(),
            )
        )
    with connect(database) as connection:
        row = connection.execute(
            "SELECT finished_at, succeeded FROM collection_runs"
        ).fetchone()
        assert row[0] is None and row[1] is None


def test_partial_error_order_is_deterministic_and_shape_errors_are_expected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def first(*, limit: int) -> CollectorOutcome:
        return CollectorOutcome((), succeeded=False, error="first-error")

    def second(*, limit: int) -> CollectorOutcome:
        return CollectorOutcome((), succeeded=False, error="second-error")

    # Outcome errors are preserved in source order by the orchestration layer.
    assert [
        result.error
        for result in collect_all(
            limit=1,
            database_path=tmp_path / "ordered.sqlite3",
            collectors={"first": first, "second": second},
            order=("first", "second"),
            clock=_clock(),
        )
    ] == ["first-error", "second-error"]

    # JSON/response-size failures are represented as expected exceptions by
    # the collector and therefore do not escape the provider boundary.
    for failure in (ValueError("invalid JSON"), RuntimeError("response too large")):
        monkeypatch.setattr(
            smartrecruiters,
            "fetch_json",
            lambda url, *, timeout, failure=failure: (_ for _ in ()).throw(failure),
        )
        result = smartrecruiters.fetch_result(
            config=_config("bad"),
            sleeper=lambda _: None,
            clock=lambda: 0.0,
        )
        assert result.succeeded is False
        assert type(failure).__name__ in (result.error or "")
