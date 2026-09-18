import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from careeros.database.schema import initialize_schema
from careeros.today.service import build_today


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    db.execute("INSERT INTO jobs(source,title,collected_at) VALUES ('x','Job','2026')")
    db.commit()
    return db


def _jobs(db: sqlite3.Connection, count: int) -> list[int]:
    ids = []
    for index in range(count):
        cursor = db.execute(
            "INSERT INTO jobs(source,title,company,collected_at) VALUES (?,?,?,?)",
            (f"source-{index}", f"Job {index}", f"Company {index}", "2026"),
        )
        ids.append(cast(int, cursor.lastrowid))
    return ids


def _reminder(
    db: sqlite3.Connection, job_id: int, due_at: str, *, completed: bool = False
) -> None:
    db.execute(
        "INSERT INTO reminders(job_id,kind,due_at,completed_at,created_at,updated_at) "
        "VALUES (?, 'review', ?, ?, '2026-08-01T00:00:00+00:00', "
        "'2026-08-01T00:00:00+00:00')",
        (job_id, due_at, "2026-08-02T00:00:00+00:00" if completed else None),
    )


def _decision(
    db: sqlite3.Connection, job_id: int, decision: str, updated_at: str
) -> int:
    cursor = db.execute(
        "INSERT INTO review_decisions(job_id,decision,created_at,updated_at) "
        "VALUES (?,?,?,?)",
        (job_id, decision, updated_at, updated_at),
    )
    return cast(int, cursor.lastrowid)


def _group(db: sqlite3.Connection, group_id: str, members: list[int]) -> None:
    db.execute("INSERT INTO job_groups(id) VALUES (?)", (group_id,))
    for job_id in members:
        db.execute(
            "INSERT INTO job_group_members(job_id,group_id,reason) VALUES (?,?,'test')",
            (job_id, group_id),
        )


def _application(db: sqlite3.Connection, job_id: int, status: str) -> None:
    db.execute(
        "INSERT INTO applications(job_id,current_status,created_at,updated_at) "
        "VALUES (?,?,'2026-08-01T00:00:00+00:00','2026-08-01T00:00:00+00:00')",
        (job_id, status),
    )


def test_reminder_boundaries_are_disjoint_and_ordered() -> None:
    db = _db()
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    for due in (
        "2026-08-27T11:59:59+00:00",
        "2026-08-27T12:00:00+00:00",
        "2026-08-27T23:59:59+00:00",
        "2026-08-28T00:00:00+00:00",
    ):
        db.execute(
            "INSERT INTO reminders(job_id,kind,due_at,created_at,updated_at) "
            "VALUES (1,'review',?,?,?)",
            (due, now.isoformat(), now.isoformat()),
        )
    db.commit()
    result = build_today(db, now=now, limit=20)
    assert [item.reminder.due_at for item in result.overdue.items] == [
        "2026-08-27T11:59:59+00:00"
    ]
    assert [item.reminder.due_at for item in result.due_today.items] == [
        "2026-08-27T12:00:00+00:00",
        "2026-08-27T23:59:59+00:00",
    ]


def test_missing_connection_is_empty_without_side_effect() -> None:
    result = build_today(None, now=datetime(2026, 8, 27, tzinfo=UTC))
    assert result.overdue.total == result.due_today.total == result.unstarted.total == 0
    assert (
        result.application_overdue.total
        == result.application_due_today.total
        == result.application_work.total
        == 0
    )
    assert result.review_next.candidate_total == result.review_next.total == 0
    assert result.review_next.items == ()


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_today(None, now=datetime(2026, 8, 27))


def test_offset_aware_now_is_normalized_and_completed_are_excluded() -> None:
    db = _db()
    job_id = 1
    _reminder(db, job_id, "2026-08-27T11:00:00+00:00")
    _reminder(db, job_id, "2026-08-27T12:00:00+00:00", completed=True)
    db.commit()
    result = build_today(
        db, now=datetime(2026, 8, 27, 14, tzinfo=timezone(timedelta(hours=2))), limit=10
    )
    assert result.as_of == "2026-08-27T12:00:00+00:00"
    assert result.overdue.total == 1
    assert result.due_today.total == 0


def test_reminder_totals_precede_independent_section_limits() -> None:
    db = _db()
    for hour in range(5):
        _reminder(db, 1, f"2026-08-27T0{hour}:00:00+00:00")
    for hour in range(13, 18):
        _reminder(db, 1, f"2026-08-27T{hour}:00:00+00:00")
    db.commit()
    result = build_today(db, now=datetime(2026, 8, 27, 12, tzinfo=UTC), limit=2)
    assert result.overdue.total == 5
    assert result.due_today.total == 5
    assert len(result.overdue.items) == len(result.due_today.items) == 2


def test_active_context_is_concrete_and_terminal_or_sibling_is_ignored() -> None:
    db = _db()
    sibling = _jobs(db, 1)[0]
    _group(db, "g1", [1, sibling])
    _application(db, 1, "applied")
    _application(db, sibling, "rejected")
    _reminder(db, 1, "2026-08-27T13:00:00+00:00")
    _reminder(db, sibling, "2026-08-27T13:00:00+00:00")
    db.commit()
    result = build_today(db, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
    assert [item.active_application_status for item in result.due_today.items] == [
        "applied",
        None,
    ]


def test_shortlist_resolves_lowest_decided_owner_and_conflict() -> None:
    db = _db()
    sibling = _jobs(db, 1)[0]
    _group(db, "g1", [1, sibling])
    _decision(db, sibling, "dismiss", "2026-08-27T10:00:00+00:00")
    owner_id = _decision(db, 1, "shortlist", "2026-08-27T11:00:00+00:00")
    db.commit()
    result = build_today(db, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
    assert result.unstarted.total == 1
    item = result.unstarted.items[0]
    assert item.decision_job_id == 1
    assert item.decision_id == owner_id
    assert item.conflict is True
    assert item.group.representative.id == 1
    assert {job.id for job in item.group.members} == {1, sibling}


@pytest.mark.parametrize("application_job", [1, 2])
@pytest.mark.parametrize("status", ["applied", "rejected"])
def test_any_group_member_application_excludes_shortlist(
    application_job: int, status: str
) -> None:
    db = _db()
    sibling = _jobs(db, 1)[0]
    _group(db, "g1", [1, sibling])
    _decision(db, 1, "shortlist", "2026-08-27T11:00:00+00:00")
    _application(db, application_job, status)
    db.commit()
    result = build_today(db, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
    assert result.unstarted.total == 0


def test_unrelated_application_does_not_exclude_and_singleton_works() -> None:
    db = _db()
    unrelated = _jobs(db, 1)[0]
    _decision(db, 1, "shortlist", "2026-08-27T11:00:00+00:00")
    _application(db, unrelated, "applied")
    db.commit()
    result = build_today(db, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
    assert result.unstarted.total == 1
    assert result.unstarted.items[0].group.group_id is None


def test_shortlist_order_uses_update_then_decision_id_then_representative() -> None:
    db = _db()
    ids = _jobs(db, 2)
    _decision(db, ids[1], "shortlist", "2026-08-27T11:00:00+00:00")
    _decision(db, 1, "shortlist", "2026-08-27T11:00:00+00:00")
    db.commit()
    result = build_today(db, now=datetime(2026, 8, 27, 12, tzinfo=UTC))
    assert [item.group.representative.id for item in result.unstarted.items] == [
        1,
        ids[1],
    ]


def test_query_count_is_constant_and_bounded() -> None:
    db = _db()
    ids = _jobs(db, 4)
    for job_id in ids:
        _reminder(db, job_id, "2026-08-27T13:00:00+00:00")
        _decision(db, job_id, "shortlist", "2026-08-27T11:00:00+00:00")
    db.commit()
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    build_today(db, now=datetime(2026, 8, 27, 12, tzinfo=UTC), limit=2)
    db.set_trace_callback(None)
    assert len(statements) <= 20
    assert not any("review_events" in statement for statement in statements)
    assert not any("application_events" in statement for statement in statements)
