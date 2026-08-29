"""Regression tests for the Job Detail concurrency defect.

Manual acceptance found that the ``#job/<id>`` view intermittently returned
``500`` with the "database schema is unavailable (run 'careeros collect
<source>' to initialize it)." detail against an already-initialized database.
The captured server traceback showed the real cause:

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread. The object was created in thread id ... and
    this is thread id ...

FastAPI drives generator-dependency steps (enter, use, exit) through the anyio
threadpool (``fastapi/concurrency.py`` ``contextmanager_in_threadpool``), so a
sync generator dependency's connection can be created, used, and closed in
different worker threads. With the default ``check_same_thread=True`` any
cross-thread use raises ``sqlite3.ProgrammingError``, which the app's sqlite
handler then mislabelled as an unavailable schema. Single requests rarely
migrate threads (threadpool workers are reused), which is why the earlier
milestones and the single-threaded TestClient tests never tripped it; the Job
Detail view issues five concurrent requests, massively increasing the odds of
thread migration.

This suite locks in the root-cause fix (``check_same_thread=False`` on both
connection dependencies) and the error-classification narrowing (transient
lock/schema-lock sqlite errors map to a retryable ``503`` contention response
instead of the misleading "schema is unavailable" ``500``). It uses only the
stdlib plus the already-present FastAPI/httpx test dependencies.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from careeros.api import app
from careeros.api.deps import (
    DATABASE_UNAVAILABLE_MESSAGE,
    LOCK_CONTENTION_MESSAGE,
    read_only_connection,
    writable_connection,
)
from careeros.api.main import _is_transient_sqlite_error
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema


def _set_database(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("CAREEROS_DATABASE", str(path))


def _make_job(source_job_id: str, title: str) -> CollectedJob:
    return CollectedJob(
        source="arbeitnow",
        source_job_id=source_job_id,
        title=title,
        company="Acme GmbH",
        location="Berlin",
        description="Build things.",
        url=f"https://example.com/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-08-01T10:00:00Z",
        collected_at="2026-08-02T08:00:00+00:00",
    )


def _seed(path: Path) -> list[int]:
    connection = connect(path)
    try:
        initialize_schema(connection)
        save_jobs(
            connection,
            [_make_job("a-1", "Senior Engineer"), _make_job("a-2", "Backend Dev")],
        )
        rows = connection.execute("SELECT id FROM jobs ORDER BY id").fetchall()
        return [int(row[0]) for row in rows]
    finally:
        connection.close()


def _request_for_handler() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/jobs",
            "raw_path": b"/jobs",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


class TestThreadMigration:
    """The root-cause regression: connections must work across threads."""

    def test_read_only_connection_survives_thread_migration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        db = tmp_path / "careeros.sqlite3"
        job_ids = _seed(db)
        _set_database(monkeypatch, db)

        generator = read_only_connection()
        connection = next(generator)

        errors: list[BaseException] = []

        def use_in_worker() -> None:
            try:
                assert connection is not None
                row = connection.execute(
                    "SELECT id FROM jobs ORDER BY id LIMIT 1"
                ).fetchone()
                assert row is not None and int(row[0]) == job_ids[0]
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def close_in_worker() -> None:
            try:
                generator.close()
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=use_in_worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        closer = threading.Thread(target=close_in_worker)
        closer.start()
        closer.join()

        assert errors == [], [str(exc) for exc in errors]

    def test_writable_connection_survives_thread_migration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        db = tmp_path / "careeros.sqlite3"
        _seed(db)
        _set_database(monkeypatch, db)

        generator = writable_connection()
        connection = next(generator)

        errors: list[BaseException] = []

        def use_in_worker() -> None:
            try:
                assert connection is not None
                connection.execute("SELECT 1 FROM jobs").fetchone()
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        worker = threading.Thread(target=use_in_worker)
        worker.start()
        worker.join()
        generator.close()

        assert errors == [], [str(exc) for exc in errors]


class TestConcurrentJobDetailBursts:
    """Six-endpoint bursts through the real ASGI app must never 5xx."""

    async def _burst(
        self, client: httpx.AsyncClient, job_id: int
    ) -> list[httpx.Response]:
        return list(
            await asyncio.gather(
                client.get(f"/jobs/{job_id}"),
                client.get(f"/jobs/{job_id}/group"),
                client.get(f"/applications/{job_id}"),
                client.get(f"/applications/{job_id}/preparation"),
                client.get(f"/review-decisions/{job_id}"),
                client.get(f"/reminders?job_id={job_id}"),
            )
        )

    def test_no_spurious_5xx_and_canonical_statuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "careeros.sqlite3"
        job_ids = _seed(db)
        _set_database(monkeypatch, db)

        transport = httpx.ASGITransport(app=app)

        async def run() -> list[list[httpx.Response]]:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
                headers={"Host": "127.0.0.1"},
            ) as client:
                tasks = [
                    self._burst(client, job_id) for _ in range(25) for job_id in job_ids
                ]
                return await asyncio.gather(*tasks)

        results = asyncio.run(run())

        statuses: dict[str, set[int]] = {
            "job": set(),
            "group": set(),
            "application": set(),
            "review": set(),
            "reminders": set(),
            "preparation": set(),
        }
        names = ["job", "group", "application", "preparation", "review", "reminders"]
        for burst in results:
            for name, response in zip(names, burst, strict=True):
                statuses[name].add(response.status_code)
                assert response.status_code < 500, (
                    f"{name} returned {response.status_code} on an initialized DB"
                )

        assert statuses["job"] == {200}
        assert statuses["group"] == {200}
        assert statuses["application"] == {404}
        assert statuses["preparation"] == {404}
        assert statuses["review"] == {404}
        assert statuses["reminders"] == {200}
        for burst in results:
            reminders = burst[5]
            assert reminders.json()["items"] == []
            assert reminders.json()["total"] == 0


class TestErrorClassification:
    """Transient sqlite errors must not masquerade as an unavailable schema."""

    def test_transient_errors_map_to_503_contention(self) -> None:
        handler = app.exception_handlers[sqlite3.Error]
        for message in (
            "database is locked",
            "database is busy",
            "database schema is locked",
            "database schema has changed",
        ):
            response = asyncio.run(
                handler(
                    _request_for_handler(),
                    sqlite3.OperationalError(message),
                )
            )
            assert response.status_code == 503, message
            assert response.body.decode() == (
                f'{{"detail":"{LOCK_CONTENTION_MESSAGE}"}}'
            ), message

    def test_unavailable_schema_errors_keep_500_message(self) -> None:
        handler = app.exception_handlers[sqlite3.Error]
        for exc in (
            sqlite3.OperationalError("no such table: jobs"),
            sqlite3.OperationalError("file is not a database"),
            sqlite3.ProgrammingError(
                "SQLite objects created in a thread can only be used in that "
                "same thread."
            ),
        ):
            response = asyncio.run(handler(_request_for_handler(), exc))
            assert response.status_code == 500
            assert response.body.decode() == (
                f'{{"detail":"{DATABASE_UNAVAILABLE_MESSAGE}"}}'
            )

    def test_transient_classifier(self) -> None:
        assert _is_transient_sqlite_error(
            sqlite3.OperationalError("database is locked")
        )
        assert _is_transient_sqlite_error(sqlite3.OperationalError("database is busy"))
        assert _is_transient_sqlite_error(
            sqlite3.OperationalError("database schema is locked")
        )
        assert _is_transient_sqlite_error(
            sqlite3.OperationalError("database schema has changed")
        )
        assert not _is_transient_sqlite_error(
            sqlite3.OperationalError("no such table: jobs")
        )
        assert not _is_transient_sqlite_error(
            sqlite3.OperationalError("file is not a database")
        )
        assert not _is_transient_sqlite_error(
            sqlite3.ProgrammingError("SQLite objects created in a thread")
        )
