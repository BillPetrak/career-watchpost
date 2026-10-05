import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from careeros.api.main import create_app
from careeros.database.review_ranking import (
    activate,
    claim,
    fail_claim,
    fully_prepared,
    get_authority,
    heartbeat,
    validate_current_generations,
)
from careeros.database.schema import initialize_schema
from careeros.ranking.materialization import materialize_generation
from careeros.ranking.rebuild import rebuild_once
from careeros.ranking.worker import run_ranking_worker
from tests.support import TestClient


def _materialize(connection, assertion, *, learned_mode):
    return materialize_generation(
        connection,
        learned_mode=learned_mode,
        scores=[],
        claim=assertion,
        authority_snapshot=get_authority(connection),
    )


def test_claim_heartbeat_failure_backoff_and_stale_takeover() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    now = datetime.now(UTC)
    first = claim(connection, now=now)
    assert first is not None
    assert heartbeat(connection, first, now=now)
    assert fail_claim(connection, first, "validation", now=now)
    assert claim(connection, now=now) is None
    later = now + timedelta(seconds=6)
    second = claim(connection, now=later)
    assert second is not None
    assert second.token != first.token


def test_failure_allowlist_backoff_privacy() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    assertion = claim(connection)
    assert assertion
    assert fail_claim(connection, assertion, "validation")
    row = connection.execute("SELECT * FROM review_ranking_authority").fetchone()
    assert row["failure_code"] == "validation"
    with pytest.raises(ValueError):
        fail_claim(connection, assertion, "private exception text")
    assert "private exception text" not in repr(tuple(row))
    assert "exception" not in str(row["failure_code"])


def test_two_connection_claim_race(tmp_path) -> None:
    path = tmp_path / "race.sqlite3"
    first = sqlite3.connect(path, timeout=1)
    first.row_factory = sqlite3.Row
    initialize_schema(first)
    first.close()
    left = sqlite3.connect(path, timeout=1)
    right = sqlite3.connect(path, timeout=1)
    left.row_factory = sqlite3.Row
    right.row_factory = sqlite3.Row
    assert claim(left) is not None
    assert claim(right) is None
    left.close()
    right.close()


def test_stale_takeover_rejects_old_owner() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    now = datetime.now(UTC)
    old = claim(connection, now=now)
    assert old
    takeover = claim(connection, now=now + timedelta(seconds=301))
    assert takeover
    assert not heartbeat(connection, old, now=now + timedelta(seconds=302))
    mode_zero = _materialize(connection, takeover, learned_mode=0)
    mode_one = _materialize(connection, takeover, learned_mode=1)
    generations = {0: mode_zero.id, 1: mode_one.id}
    assert not activate(connection, old, None, generations)
    assert (
        connection.execute("SELECT count(*) FROM review_ranking_current").fetchone()[0]
        == 0
    )
    assert activate(connection, takeover, None, generations)


def test_no_partial_current() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    assertion = claim(connection)
    assert assertion
    generation = _materialize(connection, assertion, learned_mode=0)
    assert not activate(connection, assertion, None, {0: generation.id})
    assert (
        connection.execute("SELECT count(*) FROM review_ranking_current").fetchone()[0]
        == 0
    )


def test_async_worker_startup_recovers_and_stops_cooperatively(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.commit()
    connection.close()
    wake = asyncio.Event()
    stop = asyncio.Event()
    stop.set()
    asyncio.run(run_ranking_worker(path, wake, stop))
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    assert fully_prepared(connection)
    connection.close()


def test_async_worker_audit_wake_repairs_runtime_corruption(tmp_path: Path) -> None:
    path = tmp_path / "worker-audit.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.commit()
    connection.close()

    async def scenario() -> None:
        wake = asyncio.Event()
        stop = asyncio.Event()
        audit = asyncio.Event()
        task = asyncio.create_task(run_ranking_worker(path, wake, stop, audit=audit))
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                check = sqlite3.connect(path)
                check.row_factory = sqlite3.Row
                ready = fully_prepared(check)
                check.close()
                if ready:
                    break
            else:
                pytest.fail("worker did not prepare initial ranking")

            corrupt = sqlite3.connect(path)
            corrupt.execute("UPDATE review_ranking_items SET raw_score=raw_score+1")
            corrupt.commit()
            corrupt.close()
            audit.set()
            wake.set()

            for _ in range(100):
                await asyncio.sleep(0.01)
                check = sqlite3.connect(path)
                check.row_factory = sqlite3.Row
                ready = fully_prepared(check)
                valid = validate_current_generations(check)
                check.close()
                if ready and valid:
                    break
            else:
                pytest.fail("worker did not repair corrupt ranking")
        finally:
            stop.set()
            wake.set()
            await task

    asyncio.run(scenario())


def test_startup_recovers_malformed_numeric_derived_state(tmp_path: Path) -> None:
    path = tmp_path / "worker-malformed.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,description,url,"
        "collected_at) "
        "VALUES ('test','1','Engineer','Acme','Python','https://example.test/1','n')"
    )
    connection.commit()
    assert rebuild_once(connection) is not None
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute("UPDATE review_ranking_items SET representative_job_id=1.5")
    connection.commit()
    connection.close()
    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}

    async def scenario() -> None:
        wake = asyncio.Event()
        stop = asyncio.Event()
        stop.set()
        await run_ranking_worker(path, wake, stop)

    asyncio.run(scenario())
    with sqlite3.connect(path) as repaired:
        repaired.row_factory = sqlite3.Row
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)
        failure = repaired.execute(
            "SELECT failure_code FROM review_ranking_authority"
        ).fetchone()[0]
        assert failure is None or failure in {
            "database",
            "validation",
            "lost_claim",
            "cancelled",
            "unknown",
        }
    assert TestClient(create_app(database=path)).get("/today").status_code == 200


@pytest.mark.parametrize("corrupt_value", [2.5, "not-an-integer"])
def test_startup_recovers_malformed_preference_ranking_revision(
    tmp_path: Path, corrupt_value: object
) -> None:
    path = tmp_path / f"worker-preference-{type(corrupt_value).__name__}.sqlite3"
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,description,url,"
        "collected_at) "
        "VALUES ('test','1','Engineer','Acme','Python','https://example.test/1','n')"
    )
    connection.commit()
    assert rebuild_once(connection) is not None
    connection.execute("PRAGMA ignore_check_constraints=ON")
    connection.execute(
        "UPDATE preference_state SET explicit_ranking_revision=?", (corrupt_value,)
    )
    connection.commit()
    connection.close()

    response = TestClient(create_app(database=path)).get("/today")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {"detail": {"code": "review_ranking_rebuilding"}}

    async def scenario() -> None:
        wake = asyncio.Event()
        stop = asyncio.Event()
        stop.set()
        await run_ranking_worker(path, wake, stop)

    asyncio.run(scenario())
    with sqlite3.connect(path) as repaired:
        repaired.row_factory = sqlite3.Row
        state = repaired.execute(
            "SELECT typeof(explicit_ranking_revision), "
            "explicit_ranking_revision, explicit_ranking_fingerprint "
            "FROM preference_state"
        ).fetchone()
        authority = get_authority(repaired)
        assert tuple(state) == (
            "integer",
            authority.explicit_revision,
            authority.explicit_fingerprint,
        )
        assert fully_prepared(repaired)
        assert validate_current_generations(repaired)
    assert TestClient(create_app(database=path)).get("/today").status_code == 200
