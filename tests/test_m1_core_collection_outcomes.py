from __future__ import annotations

from datetime import UTC, datetime

from careeros.collecting import collect_one
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.database.connection import connect
from careeros.database.runs import latest_attempt_is_incomplete
from careeros.database.schema import initialize_schema


def _job() -> CollectedJob:
    return CollectedJob(
        "lever",
        "global:b:p",
        "Role",
        "Company",
        None,
        None,
        None,
        None,
        None,
        None,
        datetime.now(UTC).isoformat(),
    )


def test_partial_outcome_persists_healthy_jobs_and_failed_run(tmp_path) -> None:
    path = tmp_path / "db.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)
        result = collect_one(
            connection,
            source="lever",
            collectors={
                "lever": lambda **_: CollectorOutcome((_job(),), False, "board failed")
            },
            limit=1,
        )
        assert not result.succeeded and result.fetched == 1 and result.inserted == 1
        assert connection.execute("select count(*) from jobs").fetchone()[0] == 1
        run = connection.execute(
            "select succeeded, fetched, inserted from collection_runs"
        ).fetchone()
        assert tuple(run) == (0, 1, 1)


def test_unexpected_programming_error_leaves_incomplete_marker(tmp_path) -> None:
    path = tmp_path / "db.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)

        def broken(**_: object) -> list[CollectedJob]:
            raise TypeError("programming bug")

        try:
            collect_one(
                connection, source="lever", collectors={"lever": broken}, limit=1
            )
        except TypeError:
            pass
        else:
            raise AssertionError("programming error should propagate")
        assert latest_attempt_is_incomplete(connection, "lever")
