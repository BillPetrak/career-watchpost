"""Service for the deterministic Today / Action Center snapshot."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta

from careeros.database.today import list_today_reminders, list_today_shortlist
from careeros.today.models import TodaySection, TodaySnapshot


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Today timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def build_today(
    connection: sqlite3.Connection | None,
    *,
    now: datetime | None = None,
    limit: int = 10,
) -> TodaySnapshot:
    """Build all Today sections from one read transaction and one UTC instant."""
    if limit < 1 or limit > 20:
        raise ValueError("Today limit must be between 1 and 20.")
    as_of = _as_utc(now if now is not None else datetime.now(UTC))
    tomorrow = datetime.combine(as_of.date() + timedelta(days=1), time.min, tzinfo=UTC)
    as_of_text = as_of.isoformat()
    tomorrow_text = tomorrow.isoformat()
    if connection is None:
        return TodaySnapshot(
            as_of=as_of_text,
            timezone="UTC",
            today_ends_at=tomorrow_text,
            limit=limit,
            overdue=TodaySection((), 0),
            due_today=TodaySection((), 0),
            unstarted=TodaySection((), 0),
        )
    connection.execute("BEGIN")
    try:
        overdue, overdue_total, due, due_total = list_today_reminders(
            connection,
            as_of=as_of_text,
            today_ends_at=tomorrow_text,
            limit=limit,
        )
        shortlist, shortlist_total = list_today_shortlist(connection, limit=limit)
    finally:
        connection.rollback()
    return TodaySnapshot(
        as_of=as_of_text,
        timezone="UTC",
        today_ends_at=tomorrow_text,
        limit=limit,
        overdue=TodaySection(overdue, overdue_total),
        due_today=TodaySection(due, due_total),
        unstarted=TodaySection(shortlist, shortlist_total),
    )
