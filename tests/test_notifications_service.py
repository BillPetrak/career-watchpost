"""Tests for the notification service layer (claim/finalize, due query, retry)."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.notifications.models import ValidationError
from careeros.notifications.service import (
    claim_due,
    count_pending_due,
    finalize_attempt,
    list_due_occurrences,
    notification_summary,
)
from careeros.reminders.service import (
    add_reminder,
    complete_reminder,
    reopen_reminder,
    update_reminder,
)

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")
_OLD_STR = "2026-08-01T10:00:00+00:00"
_FUTURE_STR = "2026-08-10T10:00:00+00:00"


def _make_job(*, source: str, source_job_id: str) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title="Python Engineer",
        company="Acme GmbH",
        location="Berlin",
        description="Build Python backends.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "careeros.sqlite3")
    initialize_schema(connection)
    save_jobs(
        connection,
        [
            _make_job(source="arbeitnow", source_job_id="a-1"),
            _make_job(source="remoteok", source_job_id="r-1"),
        ],
    )
    return connection


def _attempt_rows(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, reminder_id, scheduled_due_at, attempted_at,"
                " delivered_at, status, error, backend"
                " FROM reminder_notifications ORDER BY id"
            )
        ]


def test_due_query_excludes_future_and_completed(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        add_reminder(
            connection, job_id=1, kind="interview", due_at=_FUTURE_STR, now=_EPOCH
        )
        complete_reminder(connection, reminder_id=2, now=_EPOCH)
        due = list_due_occurrences(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()
    assert [occ.kind for occ in due] == ["follow_up"]


def test_due_query_ordering_by_due_then_id(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        for job_id, due_at in (
            (1, "2026-08-02T09:00:00+00:00"),
            (1, "2026-08-01T09:00:00+00:00"),
            (2, "2026-08-01T09:00:00+00:00"),
        ):
            add_reminder(
                connection, job_id=job_id, kind="custom", due_at=due_at, now=_EPOCH
            )
        due = list_due_occurrences(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()
    assert [(occ.job_id, occ.due_at) for occ in due] == [
        (1, "2026-08-01T09:00:00+00:00"),
        (2, "2026-08-01T09:00:00+00:00"),
        (1, "2026-08-02T09:00:00+00:00"),
    ]


def test_due_query_honors_limit(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        for job_id in (1, 2, 2, 2):
            add_reminder(
                connection, job_id=job_id, kind="review", due_at=_OLD_STR, now=_EPOCH
            )
        assert len(list_due_occurrences(connection, now=_EPOCH, limit=2)) == 2
    finally:
        connection.close()


def test_due_query_performs_no_writes(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        list_due_occurrences(connection, now=_EPOCH, limit=10)
        list_due_occurrences(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()
    assert _attempt_rows(tmp_path / "careeros.sqlite3") == []


def test_claim_then_finalize_delivered_is_exactly_once(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        claimed = claim_due(connection, now=_EPOCH, limit=10, backend="stdout")
        assert len(claimed) == 1
        assert (
            finalize_attempt(
                connection,
                attempt_id=claimed[0].attempt_id,
                outcome="delivered",
                delivered_at="2026-08-03T12:00:01+00:00",
                error=None,
            )
            is True
        )
    finally:
        connection.close()
    reread = connect(database)
    initialize_schema(reread)
    try:
        assert list_due_occurrences(reread, now=_EPOCH, limit=10) == []
    finally:
        reread.close()


def test_retry_after_failed_delivery(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        claimed = claim_due(connection, now=_EPOCH, limit=10, backend="stdout")
        assert (
            finalize_attempt(
                connection,
                attempt_id=claimed[0].attempt_id,
                outcome="failed",
                delivered_at=None,
                error="notify-send exited 1",
            )
            is True
        )
        assert list_due_occurrences(connection, now=_EPOCH, limit=10)
    finally:
        connection.close()


def test_reopened_undelivered_becomes_eligible_again(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        complete_reminder(connection, reminder_id=1, now=_EPOCH)
        assert list_due_occurrences(connection, now=_EPOCH, limit=10) == []
        reopen_reminder(connection, reminder_id=1, now=_EPOCH)
        assert len(list_due_occurrences(connection, now=_EPOCH, limit=10)) == 1
    finally:
        connection.close()


def test_reopened_delivered_occurrence_is_not_re_notified(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        claimed = claim_due(connection, now=_EPOCH, limit=10, backend="stdout")
        finalize_attempt(
            connection,
            attempt_id=claimed[0].attempt_id,
            outcome="delivered",
            delivered_at="2026-08-03T12:00:01+00:00",
            error=None,
        )
        complete_reminder(connection, reminder_id=1, now=_EPOCH)
        reopen_reminder(connection, reminder_id=1, now=_EPOCH)
        assert list_due_occurrences(connection, now=_EPOCH, limit=10) == []
    finally:
        connection.close()


def test_rescheduled_reminder_is_eligible_again(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        claimed = claim_due(connection, now=_EPOCH, limit=10, backend="stdout")
        finalize_attempt(
            connection,
            attempt_id=claimed[0].attempt_id,
            outcome="delivered",
            delivered_at="2026-08-03T12:00:01+00:00",
            error=None,
        )
        assert list_due_occurrences(connection, now=_EPOCH, limit=10) == []
        update_reminder(
            connection, reminder_id=1, due_at="2026-08-02T09:00:00+00:00", now=_EPOCH
        )
        assert len(list_due_occurrences(connection, now=_EPOCH, limit=10)) == 1
    finally:
        connection.close()


def test_claim_excludes_inflight_from_another_worker(tmp_path: Path) -> None:
    database = tmp_path / "careeros.sqlite3"
    seeder = _open(tmp_path)
    try:
        add_reminder(seeder, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH)
    finally:
        seeder.close()
    worker_a = connect(database)
    worker_b = connect(database)
    initialize_schema(worker_a)
    initialize_schema(worker_b)
    try:
        first = claim_due(worker_a, now=_EPOCH, limit=10, backend="stdout")
        second = claim_due(worker_b, now=_EPOCH, limit=10, backend="stdout")
        assert len(first) == 1
        assert second == []
    finally:
        worker_a.close()
        worker_b.close()


def test_stale_claim_is_expired_and_reclaimable(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        claimed = claim_due(
            connection,
            now=_EPOCH,
            limit=10,
            backend="stdout",
            claim_timeout=timedelta(minutes=5),
        )
        assert len(claimed) == 1
        later = datetime.fromisoformat("2026-08-03T12:06:00+00:00")
        reclaimed = claim_due(
            connection,
            now=later,
            limit=10,
            backend="stdout",
            claim_timeout=timedelta(minutes=5),
        )
        assert len(reclaimed) == 1
        expired = [
            row
            for row in connection.execute(
                "SELECT status, error FROM reminder_notifications ORDER BY id"
            )
        ]
        assert expired[0]["status"] == "failed"
        assert expired[0]["error"] == "claim-expired"
    finally:
        connection.close()


def test_finalize_is_idempotent_and_rejects_superseded(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        claimed = claim_due(connection, now=_EPOCH, limit=10, backend="stdout")
        assert (
            finalize_attempt(
                connection,
                attempt_id=claimed[0].attempt_id,
                outcome="delivered",
                delivered_at="2026-08-03T12:00:01+00:00",
                error=None,
            )
            is True
        )
        assert (
            finalize_attempt(
                connection,
                attempt_id=claimed[0].attempt_id,
                outcome="delivered",
                delivered_at="2026-08-03T12:00:02+00:00",
                error=None,
            )
            is False
        )
    finally:
        connection.close()


def test_pending_and_summary_counts(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        add_reminder(
            connection, job_id=2, kind="interview", due_at=_FUTURE_STR, now=_EPOCH
        )
        assert count_pending_due(connection, now=_EPOCH) == 1

        claimed = claim_due(connection, now=_EPOCH, limit=10, backend="stdout")
        finalize_attempt(
            connection,
            attempt_id=claimed[0].attempt_id,
            outcome="delivered",
            delivered_at="2026-08-03T12:00:01+00:00",
            error=None,
        )
        summary = notification_summary(connection, now=_EPOCH)
        assert summary.pending_due == 0
        assert summary.delivered_total == 1
        assert summary.failed_total == 0
        assert summary.last_delivered_at == "2026-08-03T12:00:01+00:00"
        assert summary.last_failed_at is None
    finally:
        connection.close()


def test_failed_summary_tracks_last_failure(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        add_reminder(
            connection, job_id=1, kind="follow_up", due_at=_OLD_STR, now=_EPOCH
        )
        claimed = claim_due(connection, now=_EPOCH, limit=10, backend="stdout")
        finalize_attempt(
            connection,
            attempt_id=claimed[0].attempt_id,
            outcome="failed",
            delivered_at=None,
            error="boom",
        )
        summary = notification_summary(connection, now=_EPOCH)
        assert summary.failed_total == 1
        assert summary.last_failed_at == _EPOCH.isoformat()
        assert summary.delivered_total == 0
        assert summary.pending_due == 1
    finally:
        connection.close()


def test_invalid_limit_rejected(tmp_path: Path) -> None:
    connection = _open(tmp_path)
    try:
        with pytest.raises(ValidationError):
            list_due_occurrences(connection, now=_EPOCH, limit=0)
        with pytest.raises(ValidationError):
            claim_due(connection, now=_EPOCH, limit=0, backend="stdout")
    finally:
        connection.close()
