"""Canonical group persistence and grouped retrieval.

This module is the database boundary for duplicate detection. It persists the
pure grouping produced by ``careeros.grouping.matcher`` and exposes grouped
search results and diagnostics. Job rows are never modified here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from careeros.database.jobs import (
    _SELECT_COLUMNS,
    StoredJob,
    _stored_job_from_row,
    list_all_jobs,
)
from careeros.database.runs import parse_utc
from careeros.grouping.matcher import assign_groups, identity_for

_JOB_COLUMNS: tuple[str, ...] = tuple(_SELECT_COLUMNS.split(", "))


@dataclass(frozen=True)
class RegroupStats:
    """Summary of a regrouping operation."""

    groups: int
    members: int


@dataclass(frozen=True)
class GroupedJob:
    """A displayed search/rank result: one per canonical group."""

    group_id: str | None
    representative: StoredJob
    members: tuple[StoredJob, ...]
    sources: tuple[str, ...]
    urls: tuple[str | None, ...]


@dataclass(frozen=True)
class GroupInfo:
    """A canonical group with its members, for the diagnostic command."""

    group_id: str
    reason: str
    created_at: str
    members: tuple[StoredJob, ...]
    sources: tuple[str, ...]
    urls: tuple[str | None, ...]


@dataclass(frozen=True)
class GroupingSummary:
    """Aggregate grouping counts for the diagnostic command."""

    total_jobs: int
    groups: int
    grouped_jobs: int


def ensure_grouped(connection: sqlite3.Connection) -> RegroupStats:
    """Recompute canonical groups and persist them.

    Deterministic and idempotent: the same stored jobs always produce the same
    groups and the same content-addressed group IDs. Only the derived group
    tables are rewritten; the jobs table is never touched.
    """
    jobs = list_all_jobs(connection)
    groups = assign_groups(jobs)

    connection.execute("DELETE FROM job_group_members")
    connection.execute("DELETE FROM job_groups")
    for group in groups:
        connection.execute(
            "INSERT INTO job_groups (id) VALUES (?)",
            (group.group_id,),
        )
        for member in group.members:
            ident = identity_for(member)
            connection.execute(
                """
                INSERT INTO job_group_members (
                    job_id, group_id, reason, remote_sig, location_sig
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    member.id,
                    group.group_id,
                    group.reason,
                    ident.remote_class,
                    ident.location_sig,
                ),
            )
    connection.commit()
    return RegroupStats(
        groups=len(groups),
        members=sum(len(group.members) for group in groups),
    )


def _sources_and_urls(
    members: tuple[StoredJob, ...],
) -> tuple[
    tuple[str, ...],
    tuple[str | None, ...],
]:
    """Derive ordered distinct sources and URLs from a group's members."""
    sources = tuple(dict.fromkeys(member.source for member in members))
    urls = tuple(dict.fromkeys(member.url for member in members))
    return sources, urls


def search_grouped(
    connection: sqlite3.Connection,
    keyword: str,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    employment_type: str | None = None,
    limit: int | None = 20,
) -> list[GroupedJob]:
    """Search jobs and collapse matches into one result per canonical group.

    Filtering is applied to the candidate member rows BEFORE grouping, so
    ``source``/``remote``/``location``/``employment_type`` narrow which listings
    participate and a group only appears when at least one of its matching
    members passes every filter. Jobs with no stored group behave as
    single-member groups, keeping results backward compatible for ungrouped
    databases. ``limit`` caps the returned groups; None returns every group.
    """
    pattern = f"%{keyword}%"
    conditions: list[str] = [
        "(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ?)",
    ]
    params: list[str | int] = [pattern, pattern, pattern]

    if remote:
        conditions.append("j.remote_status = 'remote'")

    if location is not None:
        conditions.append("j.location LIKE ?")
        params.append(f"%{location}%")

    if source is not None:
        conditions.append("j.source = ?")
        params.append(source)

    if employment_type is not None:
        conditions.append("j.employment_type LIKE ?")
        params.append(f"%{employment_type}%")

    rows = connection.execute(
        f"""
        SELECT {", ".join(f"j.{column}" for column in _JOB_COLUMNS)},
               m.group_id AS group_id
        FROM jobs j
        LEFT JOIN job_group_members m ON m.job_id = j.id
        WHERE {" AND ".join(conditions)}
        ORDER BY j.id
        """,
        params,
    ).fetchall()

    buckets: dict[str, list[StoredJob]] = {}
    order: list[str] = []
    for row in rows:
        job = _stored_job_from_row(row)
        group_id = row["group_id"]
        key = group_id if group_id is not None else f"__single__:{job.id}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(job)

    results: list[GroupedJob] = []
    for key in order:
        members = tuple(sorted(buckets[key], key=lambda job: job.id))
        sources, urls = _sources_and_urls(members)
        group_id = key if not key.startswith("__single__:") else None
        results.append(
            GroupedJob(
                group_id=group_id,
                representative=members[0],
                members=members,
                sources=sources,
                urls=urls,
            )
        )

    results.sort(key=lambda group: _publish_sort_key(group.representative))
    return results[:limit] if limit is not None else results


def _publish_sort_key(job: StoredJob) -> tuple[int, int, int]:
    """Stable sort key: published desc (NULLS LAST), then ascending job ID."""
    if job.published_at is None:
        return (1, 0, job.id)
    parsed = parse_utc(job.published_at)
    if parsed is None:
        return (1, 0, job.id)
    return (0, -int(parsed.timestamp()), job.id)


def list_all_groups(
    connection: sqlite3.Connection,
    *,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    employment_type: str | None = None,
) -> list[GroupedJob]:
    """Return every distinct opportunity as a GroupedJob.

    Jobs with a stored group are collapsed into one result per canonical group;
    ungrouped jobs behave as single-member groups. Optional ``remote``/
    ``location``/``source``/``employment_type`` filters apply to the candidate
    member rows BEFORE grouping, so a group only appears when at least one of
    its members passes every filter (matching the grouped-search semantics).
    Results are ordered by ascending representative (lowest member) job ID,
    which is deterministic regardless of grouping state.
    """
    conditions: list[str] = []
    params: list[str] = []

    if remote:
        conditions.append("j.remote_status = 'remote'")

    if location is not None:
        conditions.append("j.location LIKE ?")
        params.append(f"%{location}%")

    if source is not None:
        conditions.append("j.source = ?")
        params.append(source)

    if employment_type is not None:
        conditions.append("j.employment_type LIKE ?")
        params.append(f"%{employment_type}%")

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    rows = connection.execute(
        f"""
        SELECT {", ".join(f"j.{column}" for column in _JOB_COLUMNS)},
               m.group_id AS group_id
        FROM jobs j
        LEFT JOIN job_group_members m ON m.job_id = j.id
        {where_clause}
        ORDER BY j.id
        """,
        params,
    ).fetchall()

    buckets: dict[str, list[StoredJob]] = {}
    order: list[str] = []
    for row in rows:
        job = _stored_job_from_row(row)
        group_id = row["group_id"]
        key = group_id if group_id is not None else f"__single__:{job.id}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(job)

    results: list[GroupedJob] = []
    for key in order:
        members = tuple(sorted(buckets[key], key=lambda job: job.id))
        sources, urls = _sources_and_urls(members)
        group_id = key if not key.startswith("__single__:") else None
        results.append(
            GroupedJob(
                group_id=group_id,
                representative=members[0],
                members=members,
                sources=sources,
                urls=urls,
            )
        )

    results.sort(key=lambda group: group.representative.id)
    return results


def list_group_infos(
    connection: sqlite3.Connection,
    *,
    group_id: str | None = None,
    limit: int = 20,
) -> list[GroupInfo]:
    """Return canonical groups (with members) for the diagnostic command."""
    rows = connection.execute(
        f"""
        SELECT g.id AS gid,
               g.created_at AS created_at,
               m.reason AS reason,
               {", ".join(f"j.{column}" for column in _JOB_COLUMNS)}
        FROM job_groups g
        JOIN job_group_members m ON m.group_id = g.id
        JOIN jobs j ON j.id = m.job_id
        ORDER BY j.id
        """
    ).fetchall()

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["gid"], []).append(row)

    infos: list[GroupInfo] = []
    for gid in sorted(grouped):
        if group_id is not None and gid != group_id:
            continue
        member_rows = grouped[gid]
        members = tuple(_stored_job_from_row(row) for row in member_rows)
        sources, urls = _sources_and_urls(members)
        infos.append(
            GroupInfo(
                group_id=gid,
                reason=member_rows[0]["reason"],
                created_at=member_rows[0]["created_at"],
                members=members,
                sources=sources,
                urls=urls,
            )
        )

    infos.sort(key=lambda info: min(member.id for member in info.members))
    return infos[:limit]


def grouping_summary(connection: sqlite3.Connection) -> GroupingSummary:
    """Return aggregate grouping counts from the derived tables."""
    total_jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    grouped_jobs = connection.execute(
        "SELECT COUNT(*) FROM job_group_members"
    ).fetchone()[0]
    groups = connection.execute("SELECT COUNT(*) FROM job_groups").fetchone()[0]
    return GroupingSummary(
        total_jobs=int(total_jobs),
        groups=int(groups),
        grouped_jobs=int(grouped_jobs),
    )


def group_ids_for_jobs(
    connection: sqlite3.Connection,
    job_ids: Iterable[int],
) -> dict[int, str | None]:
    """Map job IDs to their canonical group ID (or None when ungrouped).

    Batch lookup for exporting: every input ID is represented in the returned
    dict so callers never need to default individual keys. The result is
    deterministic and never writes.
    """
    ids = list(job_ids)
    mapping: dict[int, str | None] = {job_id: None for job_id in ids}
    if not ids:
        return mapping

    placeholders = ", ".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT job_id, group_id
        FROM job_group_members
        WHERE job_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    for row in rows:
        mapping[int(row["job_id"])] = row["group_id"]
    return mapping
