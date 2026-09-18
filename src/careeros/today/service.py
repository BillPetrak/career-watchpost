"""Service for the deterministic Today / Action Center snapshot."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta
from typing import Final, Literal

from careeros.database.today import (
    list_today_applications,
    list_today_reminders,
    list_today_shortlist,
)
from careeros.preferences.models import EXPLICIT_PREFERENCE_DIMENSIONS
from careeros.preferences.service import (
    build_adaptive_preference_set,
    resolve_explicit_profile,
)
from careeros.review.service import build_queue
from careeros.today.models import TodayReviewNext, TodaySection, TodaySnapshot

REVIEW_NEXT_LIMIT: Final[Literal[3]] = 3
REVIEW_NEXT_MINIMUM_SCORE: Final[Literal[1]] = 1


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Today timestamps must be timezone-aware.")
    return value.astimezone(UTC)


def build_today(
    connection: sqlite3.Connection | None,
    *,
    now: datetime | None = None,
    limit: int = 10,
    use_learned_preferences: bool = True,
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
            application_overdue=TodaySection((), 0),
            application_due_today=TodaySection((), 0),
            application_work=TodaySection((), 0),
            unstarted=TodaySection((), 0),
            review_next=TodayReviewNext(
                candidate_total=0,
                total=0,
                limit=REVIEW_NEXT_LIMIT,
                minimum_score=REVIEW_NEXT_MINIMUM_SCORE,
                personalized=False,
                use_learned_preferences=use_learned_preferences,
                items=(),
            ),
        )
    connection.execute("BEGIN")
    try:
        overdue, overdue_total, due, due_total = list_today_reminders(
            connection,
            as_of=as_of_text,
            today_ends_at=tomorrow_text,
            limit=limit,
        )
        (
            application_overdue,
            application_overdue_total,
            application_due,
            application_due_total,
            application_work,
            application_work_total,
        ) = list_today_applications(
            connection,
            as_of=as_of_text,
            today_ends_at=tomorrow_text,
            limit=limit,
        )
        shortlist, shortlist_total = list_today_shortlist(connection, limit=limit)
        explicit_context = resolve_explicit_profile(connection)
        explicit_profile = explicit_context.explicit_profile
        adaptive_preferences = build_adaptive_preference_set(
            connection,
            supplied_profile=explicit_profile,
            enabled=use_learned_preferences,
        )
        queue = build_queue(
            connection,
            profile=explicit_profile.to_candidate_profile(),
            state="unreviewed",
            min_score=REVIEW_NEXT_MINIMUM_SCORE,
            include_tracked=False,
            limit=REVIEW_NEXT_LIMIT,
            grouped=True,
            feedback=None,
            adaptive_preferences=adaptive_preferences,
        )
        has_explicit_authority = explicit_context.source == "stored" and any(
            getattr(explicit_profile, dimension)
            for dimension in EXPLICIT_PREFERENCE_DIMENSIONS
        )
        has_learned_authority = use_learned_preferences and any(
            preference.active
            and not preference.suppressed
            and preference.polarity is not None
            and preference.dimension not in adaptive_preferences.locked_dimensions
            for preference in adaptive_preferences.learned
        )
    finally:
        connection.rollback()
    return TodaySnapshot(
        as_of=as_of_text,
        timezone="UTC",
        today_ends_at=tomorrow_text,
        limit=limit,
        overdue=TodaySection(overdue, overdue_total),
        due_today=TodaySection(due, due_total),
        application_overdue=TodaySection(
            application_overdue, application_overdue_total
        ),
        application_due_today=TodaySection(application_due, application_due_total),
        application_work=TodaySection(application_work, application_work_total),
        unstarted=TodaySection(shortlist, shortlist_total),
        review_next=TodayReviewNext(
            candidate_total=queue.candidate_count,
            total=queue.qualified_count,
            limit=REVIEW_NEXT_LIMIT,
            minimum_score=REVIEW_NEXT_MINIMUM_SCORE,
            personalized=has_explicit_authority or has_learned_authority,
            use_learned_preferences=use_learned_preferences,
            items=queue.items,
        ),
    )
