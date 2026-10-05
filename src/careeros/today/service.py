"""Service for the deterministic Today / Action Center snapshot."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, time, timedelta
from typing import Final, Literal

from careeros.applications.service import group_tracking_info
from careeros.database.groups import GroupedJob
from careeros.database.jobs import JOB_COLUMNS, StoredJob, job_from_row
from careeros.database.preferences import get_preference_state
from careeros.database.review import decisions_for_jobs
from careeros.database.review_ranking import (
    EMPTY_LEARNED_FINGERPRINT,
    MODE_WITH_LEARNED,
    Generation,
    RankingDerivedStateError,
    canonical_sha256,
    fully_prepared,
    generation_from_row,
    get_authority,
    list_generation_items,
)
from careeros.database.today import (
    list_today_applications,
    list_today_reminders,
    list_today_shortlist,
)
from careeros.preferences.models import (
    EXPLICIT_PREFERENCE_DIMENSIONS,
    AdaptivePreferenceSet,
    ExplicitPreferenceProfile,
    explicit_ranking_authority_fingerprint,
    learned_ranking_authority_fingerprint,
)
from careeros.preferences.service import (
    build_ranking_adaptive_preference_set,
    resolve_explicit_profile,
)
from careeros.ranking.materialization import (
    canonical_opportunity_key,
    items_fingerprint,
    opportunity_digest,
)
from careeros.ranking.scorer import score_groups
from careeros.review.service import (
    QueueItem,
    QueueOutcome,
    resolve_group_decision,
)
from careeros.today.models import TodayReviewNext, TodaySection, TodaySnapshot

REVIEW_NEXT_LIMIT: Final[Literal[3]] = 3
REVIEW_NEXT_MINIMUM_SCORE: Final[Literal[1]] = 1


class ReviewRankingUnavailable(RuntimeError):
    """The durable materialized ranking is not currently usable."""


def _derived_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RankingDerivedStateError(f"ranking {label} must be an integer")
    return value


def _validate_persisted_generation(
    connection: sqlite3.Connection,
    generation: Generation,
) -> None:
    """Validate complete persisted item integrity inside the read snapshot."""
    items = list_generation_items(connection, generation.id)
    if (
        len(items) != generation.item_count
        or len({item.opportunity_key for item in items}) != len(items)
        or len({item.rank_ordinal for item in items}) != len(items)
        or tuple(item.rank_ordinal for item in items)
        != tuple(range(1, generation.item_count + 1))
        or items_fingerprint(items) != generation.items_fingerprint
        or generation.content_fingerprint
        != canonical_sha256(
            "careeros-ranking-generation-v1",
            (
                generation.target_revision,
                generation.learned_mode,
                generation.explicit_fingerprint,
                generation.opportunity_fingerprint,
                generation.learned_fingerprint,
                generation.items_fingerprint,
            ),
        )
    ):
        raise ReviewRankingUnavailable("review ranking is rebuilding")


def _selected_groups(
    connection: sqlite3.Connection,
    keys: set[str],
) -> dict[str, GroupedJob]:
    """Reconstruct only the selected canonical opportunities."""
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = connection.execute(
        f"""SELECT {JOB_COLUMNS}, m.group_id AS canonical_group_id
            FROM jobs j LEFT JOIN job_group_members m ON m.job_id=j.id
            WHERE CASE WHEN m.group_id IS NULL THEN 'job:' || j.id
                       ELSE 'group:' || m.group_id END IN ({placeholders})
            ORDER BY j.id""",
        tuple(sorted(keys)),
    ).fetchall()
    members: dict[str, list[StoredJob]] = {}
    group_ids: dict[str, str | None] = {}
    for row in rows:
        job = job_from_row(row)
        group_id = row["canonical_group_id"]
        normalized_group_id = None if group_id is None else str(group_id)
        key = canonical_opportunity_key(normalized_group_id, job.id)
        members.setdefault(key, []).append(job)
        group_ids[key] = normalized_group_id
    result: dict[str, GroupedJob] = {}
    for key, raw_members in members.items():
        ordered = tuple(sorted(raw_members, key=lambda job: job.id))
        result[key] = GroupedJob(
            group_id=group_ids[key],
            representative=ordered[0],
            members=ordered,
            sources=tuple(dict.fromkeys(member.source for member in ordered)),
            urls=tuple(dict.fromkeys(member.url for member in ordered)),
        )
    return result


def _materialized_queue_unchecked(
    connection: sqlite3.Connection,
    *,
    explicit_profile: ExplicitPreferenceProfile,
    adaptive_preferences: AdaptivePreferenceSet,
    use_learned_preferences: bool,
) -> QueueOutcome:
    """Build Review-next from durable ranking metadata and live eligibility."""
    if not fully_prepared(connection):
        raise ReviewRankingUnavailable("review ranking is rebuilding")
    mode = MODE_WITH_LEARNED if use_learned_preferences else 0
    current = connection.execute(
        """SELECT g.*
           FROM review_ranking_current AS c
           JOIN review_ranking_generations AS g ON g.id=c.generation_id
           WHERE c.learned_mode=?""",
        (mode,),
    ).fetchone()
    if current is None:
        raise ReviewRankingUnavailable("review ranking is rebuilding")
    generation = generation_from_row(current)
    generation_id = generation.id
    _validate_persisted_generation(connection, generation)
    annotated_items = """
        WITH annotated AS (
          SELECT i.opportunity_key, i.group_id, i.representative_job_id,
                  i.opportunity_fingerprint, i.rank_ordinal, i.raw_score,
                  i.public_match_score,
                  CASE WHEN i.group_id IS NULL THEN
                    EXISTS (SELECT 1 FROM review_decisions d
                            WHERE d.job_id=i.representative_job_id)
                  ELSE EXISTS (SELECT 1 FROM job_group_members gm
                               JOIN review_decisions d ON d.job_id=gm.job_id
                               WHERE gm.group_id=i.group_id)
                  END AS reviewed,
                  CASE WHEN i.group_id IS NULL THEN
                    EXISTS (SELECT 1 FROM applications a
                            WHERE a.job_id=i.representative_job_id)
                  ELSE EXISTS (SELECT 1 FROM job_group_members gm
                               JOIN applications a ON a.job_id=gm.job_id
                               WHERE gm.group_id=i.group_id)
                  END AS tracked
           FROM review_ranking_items AS i
           WHERE i.generation_id=?
        )
    """
    totals = connection.execute(
        annotated_items
        + """SELECT count(*) AS item_count,
                    coalesce(sum(CASE WHEN reviewed=0 AND tracked=0
                                      THEN 1 ELSE 0 END), 0) AS candidate_total,
                    coalesce(sum(CASE WHEN reviewed=0 AND tracked=0
                                           AND public_match_score>=?
                                      THEN 1 ELSE 0 END), 0) AS qualified_total
             FROM annotated""",
        (generation_id, REVIEW_NEXT_MINIMUM_SCORE),
    ).fetchone()
    if (
        totals is None
        or _derived_integer(totals["item_count"], "item_count") != generation.item_count
        or _derived_integer(totals["item_count"], "item_count")
        != generation.opportunity_count
    ):
        raise ReviewRankingUnavailable("review ranking is rebuilding")
    selected_rows = connection.execute(
        annotated_items
        + """SELECT opportunity_key, group_id, representative_job_id,
                    opportunity_fingerprint, rank_ordinal, raw_score,
                    public_match_score
             FROM annotated
             WHERE reviewed=0 AND tracked=0 AND public_match_score>=?
             ORDER BY rank_ordinal
             LIMIT ?""",
        (
            generation_id,
            REVIEW_NEXT_MINIMUM_SCORE,
            REVIEW_NEXT_LIMIT,
        ),
    ).fetchall()
    authority = get_authority(connection)
    preference_state = get_preference_state(connection)
    explicit_identity = explicit_ranking_authority_fingerprint(explicit_profile)
    learned_identity = (
        learned_ranking_authority_fingerprint(adaptive_preferences.learned)
        if use_learned_preferences
        else EMPTY_LEARNED_FINGERPRINT
    )
    if (
        preference_state.explicit_ranking_revision != authority.explicit_revision
        or preference_state.explicit_ranking_fingerprint != explicit_identity
        or authority.explicit_fingerprint != explicit_identity
        or (
            use_learned_preferences
            and authority.learned_fingerprint != learned_identity
        )
    ):
        raise ReviewRankingUnavailable("review ranking is rebuilding")
    groups = _selected_groups(
        connection, {str(row["opportunity_key"]) for row in selected_rows}
    )
    if len(groups) != len(selected_rows):
        raise ReviewRankingUnavailable("review ranking is rebuilding")
    selected_items: list[QueueItem] = []
    decisions = decisions_for_jobs(
        connection,
        {member.id for group in groups.values() for member in group.members},
    )
    ranked = score_groups(
        [groups[str(row["opportunity_key"])] for row in selected_rows],
        explicit_profile.to_candidate_profile(),
        adaptive_preferences=adaptive_preferences,
    )
    by_key = {
        canonical_opportunity_key(
            ranked_group.group.group_id,
            ranked_group.group.representative.id,
        ): ranked_group
        for ranked_group in ranked
    }
    member_ids = {member.id for group in groups.values() for member in group.members}
    status_rows = (
        connection.execute(
            "SELECT job_id,current_status FROM applications WHERE job_id IN ("
            + ",".join("?" for _ in member_ids)
            + ")",
            tuple(sorted(member_ids)),
        ).fetchall()
        if member_ids
        else ()
    )
    statuses = {
        int(item["job_id"]): str(item["current_status"]) for item in status_rows
    }
    for row in selected_rows:
        key = str(row["opportunity_key"])
        group = groups[key]
        expected = opportunity_digest(
            key, group.group_id, group.representative.id, group.members
        )
        if (
            expected != row["opportunity_fingerprint"]
            or row["group_id"] != group.group_id
            or _derived_integer(row["representative_job_id"], "representative_job_id")
            != group.representative.id
            or key not in by_key
            or by_key[key].raw_score != _derived_integer(row["raw_score"], "raw_score")
            or by_key[key].match_score
            != _derived_integer(row["public_match_score"], "public_match_score")
        ):
            raise ReviewRankingUnavailable("review ranking is rebuilding")
        selected_items.append(
            QueueItem(
                group=group,
                group_decision=resolve_group_decision(group, decisions),
                ranked=by_key[key],
                application=group_tracking_info(group, statuses),
            )
        )
    return QueueOutcome(
        grouped=True,
        state="unreviewed",
        items=tuple(selected_items),
        candidate_count=_derived_integer(totals["candidate_total"], "candidate_total"),
        qualified_count=_derived_integer(totals["qualified_total"], "qualified_total"),
        min_score=REVIEW_NEXT_MINIMUM_SCORE,
        include_tracked=False,
    )


def _materialized_queue(
    connection: sqlite3.Connection,
    *,
    explicit_profile: ExplicitPreferenceProfile,
    adaptive_preferences: AdaptivePreferenceSet,
    use_learned_preferences: bool,
) -> QueueOutcome:
    """Convert malformed ranking-derived numeric state into unavailability."""
    try:
        return _materialized_queue_unchecked(
            connection,
            explicit_profile=explicit_profile,
            adaptive_preferences=adaptive_preferences,
            use_learned_preferences=use_learned_preferences,
        )
    except RankingDerivedStateError as exc:
        raise ReviewRankingUnavailable("review ranking is rebuilding") from exc


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
        raise ReviewRankingUnavailable("review ranking is rebuilding")
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
        try:
            explicit_context = resolve_explicit_profile(connection)
            explicit_profile = explicit_context.explicit_profile
            adaptive_preferences = build_ranking_adaptive_preference_set(
                connection,
                supplied_profile=explicit_profile,
                enabled=use_learned_preferences,
            )
        except RankingDerivedStateError as exc:
            raise ReviewRankingUnavailable("review ranking is rebuilding") from exc
        queue = _materialized_queue(
            connection,
            explicit_profile=explicit_profile,
            adaptive_preferences=adaptive_preferences,
            use_learned_preferences=use_learned_preferences,
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
