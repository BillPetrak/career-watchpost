"""Read-only adapter turning stored review decisions into a :class:`FeedbackSet`.

This module is the database boundary for review-feedback ranking: it translates
the ``review_decisions`` table (anchored to the stable ``jobs.id``, never the
derived grouping tables) into the pure feedback model defined in
:mod:`careeros.ranking.feedback`. It performs only SELECTs and never writes.

Cross-source duplicates contribute exactly once: decided jobs are resolved per
canonical group to the lowest decided ``jobs.id`` (the same deterministic rule
the review queue uses for conflicts), so the same underlying opportunity is a
single feedback source no matter how many sources listed it.
"""

from __future__ import annotations

import sqlite3

from careeros.database.jobs import JOB_COLUMNS, job_from_row
from careeros.ranking.feedback import FeedbackSet, FeedbackSource


def build_feedback_set(connection: sqlite3.Connection) -> FeedbackSet:
    """Build one FeedbackSet from every stored review decision (read-only).

    The set is deterministic: each decided canonical group is represented by its
    lowest decided ``jobs.id``, and groups are ordered by that representative id
    ascending, so the same database always yields the same sources.
    """
    rows = connection.execute(
        """
        SELECT d.job_id AS job_id,
               d.decision AS decision,
               m.group_id AS group_id
        FROM review_decisions d
        LEFT JOIN job_group_members m ON m.job_id = d.job_id
        ORDER BY d.job_id
        """
    ).fetchall()
    if not rows:
        return FeedbackSet()

    # Group decided jobs by canonical group (or by themselves when ungrouped),
    # then pick the lowest decided job id as the deterministic representative.
    buckets: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        group_id = row["group_id"]
        key = group_id if group_id is not None else f"__single__:{row['job_id']}"
        buckets.setdefault(key, []).append((int(row["job_id"]), row["decision"]))

    representatives: dict[int, str] = {}
    for decided in buckets.values():
        decided.sort(key=lambda pair: pair[0])
        representatives[decided[0][0]] = decided[0][1]

    rep_ids = tuple(sorted(representatives))
    placeholders = ", ".join("?" * len(rep_ids))
    job_rows = connection.execute(
        f"SELECT {JOB_COLUMNS} FROM jobs WHERE id IN ({placeholders})",
        rep_ids,
    ).fetchall()
    jobs_by_id = {int(row["id"]): job_from_row(row) for row in job_rows}

    sources = tuple(
        FeedbackSource(
            job_id=job.id,
            title=job.title or "",
            company=job.company,
            location=job.location,
            remote_status=job.remote_status,
            employment_type=job.employment_type,
            decision=representatives[job.id],
        )
        for job in (jobs_by_id[rep_id] for rep_id in rep_ids)
    )
    return FeedbackSet(sources=sources)


__all__: tuple[str, ...] = ("build_feedback_set",)
