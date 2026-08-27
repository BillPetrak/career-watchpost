"""Presentation-neutral models for the Today snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.database.reminders import StoredReminder


@dataclass(frozen=True)
class TodayReminder:
    reminder: StoredReminder
    job: StoredJob
    active_application_status: str | None


@dataclass(frozen=True)
class TodayShortlist:
    group: GroupedJob
    decision_job_id: int
    decision_id: int
    decision_updated_at: str
    conflict: bool


T = TypeVar("T")


@dataclass(frozen=True)
class TodaySection(Generic[T]):
    items: tuple[T, ...]
    total: int


@dataclass(frozen=True)
class TodaySnapshot:
    as_of: str
    timezone: str
    today_ends_at: str
    limit: int
    overdue: TodaySection[TodayReminder]
    due_today: TodaySection[TodayReminder]
    unstarted: TodaySection[TodayShortlist]
