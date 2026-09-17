"""Bounded SQL read primitives for the Today snapshot.

All functions are read-only. They deliberately operate on the caller's single
connection so the service can materialize every section from one SQLite
snapshot and never invoke the full-pool grouping helpers.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import cast

from careeros.database.groups import GroupedJob
from careeros.database.jobs import JOB_COLUMNS, job_from_row
from careeros.database.reminders import StoredReminder
from careeros.today.models import (
    TodayApplication,
    TodayPackage,
    TodayPreparation,
    TodayReminder,
    TodayShortlist,
)

_JOB_COLUMNS = tuple(JOB_COLUMNS.split(", "))
_STAGE_RANK = {"offer": 0, "interview": 1, "applied": 2, "interested": 3}


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _application_sort_time(value: str) -> datetime:
    return _aware_utc(value) or datetime.min.replace(tzinfo=UTC)


def list_today_applications(
    connection: sqlite3.Connection,
    *,
    as_of: str,
    today_ends_at: str,
    limit: int,
) -> tuple[
    tuple[TodayApplication, ...],
    int,
    tuple[TodayApplication, ...],
    int,
    tuple[TodayApplication, ...],
    int,
]:
    """Return the three deterministic Application-work sections.

    Two batched reads load every active concrete Application and its narrow
    Preparation/Package facts, then its possible deadline authority.  The
    query count therefore does not depend on result cardinality and no Package
    completeness or Material authority is consulted.
    """
    rows = connection.execute(
        f"""
        WITH preparation AS (
            SELECT p.application_id, p.revision,
                   p.requirements_reviewed_revision,
                   p.ready_confirmed_revision,
                   SUM(CASE WHEN i.confirmed=0 THEN 1 ELSE 0 END)
                       AS unconfirmed_count,
                   SUM(CASE WHEN i.required=1 AND i.kind!='deadline'
                             AND i.fulfillment_state='open' THEN 1 ELSE 0 END)
                       AS open_count
            FROM application_preparations AS p
            LEFT JOIN application_requirement_items AS i
              ON i.application_id=p.application_id
            GROUP BY p.application_id
        ), draft AS (
            SELECT p.application_id, v.revision AS draft_revision,
                   v.preparation_revision AS draft_preparation_revision,
                   v.approved_revision
            FROM application_packages AS p
            JOIN application_package_versions AS v ON v.package_id=p.id
            WHERE v.state='draft'
        )
        SELECT a.id AS application_id, a.job_id, a.current_status,
               a.updated_at AS application_updated_at,
               prep.application_id AS preparation_application_id,
               prep.revision AS preparation_revision,
               prep.requirements_reviewed_revision,
               prep.ready_confirmed_revision,
               prep.unconfirmed_count, prep.open_count,
               draft.draft_revision, draft.draft_preparation_revision,
               draft.approved_revision,
               {", ".join(f"j.{column}" for column in _JOB_COLUMNS)}
        FROM applications AS a
        JOIN jobs AS j ON j.id=a.job_id
        LEFT JOIN preparation AS prep ON prep.application_id=a.id
        LEFT JOIN draft ON draft.application_id=a.id
        WHERE a.current_status IN ('interested','applied','interview','offer')
        """
    ).fetchall()
    deadline_rows = connection.execute(
        """
        SELECT i.application_id, i.deadline_at, i.confirmed
        FROM application_requirement_items AS i
        JOIN application_preparations AS p ON p.application_id=i.application_id
        JOIN applications AS a ON a.id=i.application_id
        WHERE a.current_status IN ('interested','applied','interview','offer')
          AND i.kind='deadline' AND i.required=1
          AND i.fulfillment_state='open' AND i.deadline_at IS NOT NULL
        """
    ).fetchall()

    snapshot_at = _aware_utc(as_of)
    snapshot_end = _aware_utc(today_ends_at)
    assert snapshot_at is not None and snapshot_end is not None
    passed_counts: dict[int, int] = {}
    qualifying: dict[int, list[datetime]] = {}
    for deadline in deadline_rows:
        parsed = _aware_utc(deadline["deadline_at"])
        if parsed is None:
            continue
        application_id = int(deadline["application_id"])
        if parsed <= snapshot_at:
            passed_counts[application_id] = passed_counts.get(application_id, 0) + 1
        if bool(deadline["confirmed"]):
            qualifying.setdefault(application_id, []).append(parsed)

    overdue: list[TodayApplication] = []
    due_today: list[TodayApplication] = []
    work: list[TodayApplication] = []
    deadline_sort: dict[int, datetime] = {}
    for row in rows:
        application_id = int(row["application_id"])
        preparation: TodayPreparation | None = None
        if row["preparation_application_id"] is not None:
            revision = int(row["preparation_revision"])
            reviewed = row["requirements_reviewed_revision"] == revision
            blocking = int(row["unconfirmed_count"] or 0)
            blocking += int(row["open_count"] or 0)
            blocking += passed_counts.get(application_id, 0)
            if not reviewed:
                blocking += 1
            ready = row["ready_confirmed_revision"] == revision
            category = (
                "user_confirmed_ready"
                if reviewed and blocking == 0 and ready
                else (
                    "requirements_satisfied"
                    if reviewed and blocking == 0
                    else ("incomplete" if reviewed else "unknown")
                )
            )
            preparation = TodayPreparation(category, blocking)

        package: TodayPackage | None = None
        if row["draft_revision"] is not None:
            live_revision = (
                int(row["preparation_revision"])
                if row["preparation_application_id"] is not None
                else 0
            )
            package = TodayPackage(
                has_draft=True,
                draft_approved_for_current_revision=(
                    row["approved_revision"] == row["draft_revision"]
                ),
                draft_preparation_revision_stale=(
                    int(row["draft_preparation_revision"]) != live_revision
                ),
            )

        earliest = min(qualifying.get(application_id, ()), default=None)
        if earliest is not None and earliest < snapshot_at:
            classification = "overdue"
        elif earliest is not None and earliest < snapshot_end:
            classification = "due_today"
        else:
            classification = "attention"
        status_eligible = row["current_status"] in ("offer", "interview")
        preparation_eligible = (
            preparation is not None and preparation.category != "user_confirmed_ready"
        )
        temporal_eligible = earliest is not None and earliest < snapshot_end
        if not (
            status_eligible or preparation_eligible or package or temporal_eligible
        ):
            continue

        if classification != "attention":
            next_action = "review_preparation_deadline"
        elif preparation is not None and preparation.category in (
            "unknown",
            "incomplete",
        ):
            next_action = "complete_preparation"
        elif (
            preparation is not None and preparation.category == "requirements_satisfied"
        ):
            next_action = "confirm_preparation_ready"
        elif package is not None and package.draft_preparation_revision_stale:
            next_action = "review_rebase_package"
        elif package is not None:
            next_action = "continue_package"
        else:
            next_action = "manage_application"

        earliest_deadline_at = None
        if classification != "attention":
            assert earliest is not None
            earliest_deadline_at = earliest.isoformat()
        item = TodayApplication(
            application_id=application_id,
            job_id=int(row["job_id"]),
            current_status=row["current_status"],
            updated_at=row["application_updated_at"],
            job=job_from_row(row),
            classification=classification,
            next_action=next_action,
            earliest_deadline_at=earliest_deadline_at,
            preparation=preparation,
            package=package,
        )
        if earliest is not None:
            deadline_sort[application_id] = earliest
        if classification == "overdue":
            overdue.append(item)
        elif classification == "due_today":
            due_today.append(item)
        else:
            work.append(item)

    def temporal_key(item: TodayApplication) -> tuple[datetime, int, float, int]:
        return (
            deadline_sort[item.application_id],
            _STAGE_RANK[item.current_status],
            -_application_sort_time(item.updated_at).timestamp(),
            item.application_id,
        )

    def work_key(item: TodayApplication) -> tuple[int, float, int]:
        return (
            _STAGE_RANK[item.current_status],
            -_application_sort_time(item.updated_at).timestamp(),
            item.application_id,
        )

    overdue.sort(key=temporal_key)
    due_today.sort(key=temporal_key)
    work.sort(key=work_key)
    return (
        tuple(overdue[:limit]),
        len(overdue),
        tuple(due_today[:limit]),
        len(due_today),
        tuple(work[:limit]),
        len(work),
    )


def _reminder_rows(
    connection: sqlite3.Connection,
    *,
    lower: str | None,
    upper: str | None,
    limit: int,
) -> tuple[list[TodayReminder], int]:
    conditions = ["r.completed_at IS NULL"]
    params: list[object] = []
    if lower is not None:
        conditions.append("r.due_at >= ?")
        params.append(lower)
    if upper is not None:
        conditions.append("r.due_at < ?")
        params.append(upper)
    where_clause = " AND ".join(conditions)
    rows = connection.execute(
        f"""
        SELECT r.id AS reminder_id, r.job_id AS reminder_job_id, r.kind,
               r.due_at, r.completed_at, r.created_at AS reminder_created,
               r.updated_at AS reminder_updated,
               a.current_status AS active_application_status,
               {", ".join(f"j.{column}" for column in _JOB_COLUMNS)}
        FROM reminders AS r
        JOIN jobs AS j ON j.id = r.job_id
        LEFT JOIN applications AS a
          ON a.job_id = r.job_id
         AND a.current_status IN ('interested', 'applied', 'interview', 'offer')
        WHERE {where_clause}
        ORDER BY r.due_at ASC, r.id ASC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    count_row = connection.execute(
        f"SELECT COUNT(*) AS total FROM reminders AS r WHERE {where_clause}",
        params,
    ).fetchone()
    total = int(count_row["total"])
    items = [
        TodayReminder(
            reminder=StoredReminder(
                id=row["reminder_id"],
                job_id=row["reminder_job_id"],
                kind=row["kind"],
                message=None,
                due_at=row["due_at"],
                completed_at=row["completed_at"],
                created_at=row["reminder_created"],
                updated_at=row["reminder_updated"],
            ),
            job=job_from_row(row),
            active_application_status=row["active_application_status"],
        )
        for row in rows
    ]
    return items, total


def list_today_reminders(
    connection: sqlite3.Connection,
    *,
    as_of: str,
    today_ends_at: str,
    limit: int,
) -> tuple[tuple[TodayReminder, ...], int, tuple[TodayReminder, ...], int]:
    """Return overdue and due-later-today reminders in two bounded reads."""
    overdue, overdue_total = _reminder_rows(
        connection, lower=None, upper=as_of, limit=limit
    )
    due, due_total = _reminder_rows(
        connection, lower=as_of, upper=today_ends_at, limit=limit
    )
    return tuple(overdue), overdue_total, tuple(due), due_total


def list_today_shortlist(
    connection: sqlite3.Connection, *, limit: int
) -> tuple[tuple[TodayShortlist, ...], int]:
    """Return canonical, unstarted shortlist opportunities.

    The owner and conflict CTEs are the SQL equivalent of
    ``review.service.resolve_group_decision``: lowest decided member id wins,
    while distinct decisions set conflict. Application exclusion is evaluated
    over every current opportunity member before the windowed limit.
    """
    rows = connection.execute(
        f"""
        WITH decided AS (
            SELECT COALESCE(m.group_id, '__single:' || d.job_id) AS opportunity_key,
                   m.group_id, d.job_id AS decision_job_id,
                   d.id AS decision_id,
                   d.decision, d.updated_at AS decision_updated_at
            FROM review_decisions AS d
            LEFT JOIN job_group_members AS m ON m.job_id = d.job_id
        ), owners AS (
            SELECT opportunity_key, MIN(decision_job_id) AS owner_job_id
            FROM decided
            GROUP BY opportunity_key
        ), resolved AS (
            SELECT d.opportunity_key,
                   MAX(d.group_id) AS group_id,
                   o.owner_job_id AS decision_job_id,
                   owner.decision AS decision,
                   owner.decision_id,
                   owner.decision_updated_at,
                   COUNT(DISTINCT d.decision) > 1 AS conflict
            FROM decided AS d
            JOIN owners AS o USING (opportunity_key)
            JOIN decided AS owner
              ON owner.opportunity_key = o.opportunity_key
             AND owner.decision_job_id = o.owner_job_id
            GROUP BY d.opportunity_key
        ), representatives AS (
            SELECT group_id, MIN(job_id) AS representative_id
            FROM job_group_members
            GROUP BY group_id
        ), acted_groups AS (
            SELECT DISTINCT m.group_id
            FROM applications AS a
            JOIN job_group_members AS m ON m.job_id = a.job_id
        ), acted_singletons AS (
            SELECT a.job_id
            FROM applications AS a
            LEFT JOIN job_group_members AS m ON m.job_id = a.job_id
            WHERE m.job_id IS NULL
        ), eligible AS (
            SELECT r.*, CASE WHEN r.group_id IS NULL THEN r.decision_job_id
                             ELSE rep.representative_id END AS representative_id,
                   COUNT(*) OVER () AS total
            FROM resolved AS r
            LEFT JOIN representatives AS rep USING (group_id)
            LEFT JOIN acted_groups AS ag ON ag.group_id = r.group_id
            LEFT JOIN acted_singletons AS ass ON ass.job_id = r.decision_job_id
            WHERE r.decision = 'shortlist'
              AND ag.group_id IS NULL
              AND ass.job_id IS NULL
            ORDER BY r.decision_updated_at DESC,
                     r.decision_id DESC,
                     rep.representative_id ASC
            LIMIT ?
        ), selected_members AS (
            SELECT e.opportunity_key, e.decision_job_id,
                   e.decision_updated_at, e.decision_id, e.conflict,
                   e.representative_id, e.total, m.job_id AS member_job_id
            FROM eligible AS e
            JOIN job_group_members AS m ON m.group_id = e.group_id
            WHERE e.group_id IS NOT NULL
            UNION ALL
            SELECT e.opportunity_key, e.decision_job_id,
                   e.decision_updated_at, e.decision_id, e.conflict,
                   e.representative_id, e.total,
                   e.decision_job_id AS member_job_id
            FROM eligible AS e
            WHERE e.group_id IS NULL
        )
        SELECT e.opportunity_key, e.decision_job_id, e.decision_updated_at,
               e.decision_id, e.conflict, e.representative_id, e.total,
               {", ".join(f"b.{column} AS b_{column}" for column in _JOB_COLUMNS)},
               bgm.group_id AS b_group_id
        FROM selected_members AS e
        JOIN jobs AS b ON b.id = e.member_job_id
        LEFT JOIN job_group_members bgm ON bgm.job_id = b.id
        ORDER BY e.decision_updated_at DESC, e.decision_id DESC,
                 e.representative_id ASC, b.id ASC
        """,
        (limit,),
    ).fetchall()
    if not rows:
        count = connection.execute(
            """
            WITH decided AS (
                SELECT COALESCE(m.group_id, '__single:' || d.job_id) AS opportunity_key,
                       m.group_id, d.job_id AS decision_job_id, d.decision
                FROM review_decisions d
                LEFT JOIN job_group_members m ON m.job_id=d.job_id
            ), owners AS (
                SELECT opportunity_key, MIN(decision_job_id) AS owner_job_id
                FROM decided GROUP BY opportunity_key
            ), resolved AS (
                SELECT d.opportunity_key, MAX(d.group_id) AS group_id,
                       o.owner_job_id AS decision_job_id,
                       owner.decision
                FROM decided AS d
                JOIN owners AS o USING (opportunity_key)
                JOIN decided AS owner
                  ON owner.opportunity_key=o.opportunity_key
                 AND owner.decision_job_id=o.owner_job_id
                GROUP BY d.opportunity_key
            ), acted_groups AS (
                SELECT DISTINCT m.group_id
                FROM applications a JOIN job_group_members m ON m.job_id=a.job_id
            ), acted_singletons AS (
                SELECT a.job_id FROM applications a
                LEFT JOIN job_group_members m ON m.job_id=a.job_id
                WHERE m.job_id IS NULL
            )
            SELECT COUNT(*) AS total FROM resolved r
            LEFT JOIN acted_groups ag ON ag.group_id=r.group_id
            LEFT JOIN acted_singletons ass ON ass.job_id=r.decision_job_id
            WHERE r.decision='shortlist' AND ag.group_id IS NULL
              AND ass.job_id IS NULL
            """
        ).fetchone()["total"]
        return (), int(count)
    total = int(rows[0]["total"])
    groups: dict[str, list[sqlite3.Row]] = {}
    metadata: dict[str, sqlite3.Row] = {}
    for row in rows:
        key = row["opportunity_key"]
        groups.setdefault(key, []).append(row)
        metadata[key] = row
    result: list[TodayShortlist] = []
    for key, members in groups.items():
        first = metadata[key]
        jobs = tuple(
            sorted(
                (
                    job_from_row(
                        cast(
                            sqlite3.Row,
                            {name: row[f"b_{name}"] for name in _JOB_COLUMNS},
                        )
                    )
                    for row in members
                ),
                key=lambda job: job.id,
            )
        )
        group_id = first["b_group_id"]
        result.append(
            TodayShortlist(
                group=GroupedJob(
                    group_id=group_id,
                    representative=jobs[0],
                    members=jobs,
                    sources=tuple(dict.fromkeys(job.source for job in jobs)),
                    urls=tuple(dict.fromkeys(job.url for job in jobs)),
                ),
                decision_job_id=first["decision_job_id"],
                decision_id=first["decision_id"],
                decision_updated_at=first["decision_updated_at"],
                conflict=bool(first["conflict"]),
            )
        )
    return tuple(result), total
