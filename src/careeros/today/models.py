"""Presentation-neutral models for the Today snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.database.reminders import StoredReminder
from careeros.review.service import QueueItem


@dataclass(frozen=True)
class TodayPreparation:
    category: str
    blocking_count: int


@dataclass(frozen=True)
class TodayPackage:
    has_draft: bool
    draft_approved_for_current_revision: bool
    draft_preparation_revision_stale: bool


@dataclass(frozen=True)
class TodayApplication:
    application_id: int
    job_id: int
    current_status: str
    updated_at: str
    job: StoredJob
    classification: str
    next_action: str
    earliest_deadline_at: str | None
    preparation: TodayPreparation | None
    package: TodayPackage | None


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
class TodayReviewNext:
    candidate_total: int
    total: int
    limit: Literal[3]
    minimum_score: Literal[1]
    personalized: bool
    use_learned_preferences: bool
    items: tuple[QueueItem, ...]


@dataclass(frozen=True)
class TodaySnapshot:
    as_of: str
    timezone: str
    today_ends_at: str
    limit: int
    overdue: TodaySection[TodayReminder]
    due_today: TodaySection[TodayReminder]
    application_overdue: TodaySection[TodayApplication]
    application_due_today: TodaySection[TodayApplication]
    application_work: TodaySection[TodayApplication]
    unstarted: TodaySection[TodayShortlist]
    review_next: TodayReviewNext
