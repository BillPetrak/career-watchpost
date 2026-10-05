"""Deterministic snapshots and persistence for Review ranking generations.

This module deliberately stores only derived ranking metadata and scores.  Job
rows and preference tables remain the authoritative source for reconstruction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from careeros.database.jobs import JOB_COLUMNS, StoredJob, job_from_row
from careeros.database.review_ranking import (
    EMPTY_LEARNED_FINGERPRINT,
    Authority,
    Claim,
    Generation,
    RankingItem,
    canonical_sha256,
    corpus_fingerprint,
    get_authority,
)
from careeros.database.runs import parse_utc


@dataclass(frozen=True)
class Opportunity:
    key: str
    group_id: str | None
    representative_job_id: int
    members: tuple[StoredJob, ...]
    fingerprint: str


@dataclass(frozen=True)
class MaterializedScore:
    opportunity: Opportunity
    raw_score: int
    public_match_score: int


def canonical_opportunity_key(group_id: str | None, representative_job_id: int) -> str:
    """Return the disjoint persistent identity for one canonical opportunity."""
    if group_id is not None:
        return f"group:{group_id}"
    return f"job:{representative_job_id}"


def _job_identity(job: StoredJob) -> tuple[object, ...]:
    """All scorer-visible/public fields, excluding collection timestamps."""
    return (
        job.id,
        job.source,
        job.source_job_id,
        job.title,
        job.company,
        job.location,
        job.description,
        job.url,
        job.employment_type,
        job.remote_status,
        job.published_at,
    )


def opportunity_identity(
    key: str,
    group_id: str | None,
    representative_job_id: int,
    members: tuple[StoredJob, ...],
) -> tuple[object, ...]:
    return (
        key,
        group_id,
        representative_job_id,
        tuple(_job_identity(member) for member in members),
    )


def opportunity_digest(
    key: str,
    group_id: str | None,
    representative_job_id: int,
    members: tuple[StoredJob, ...],
) -> str:
    return canonical_sha256(
        "careeros-opportunity-v1",
        opportunity_identity(key, group_id, representative_job_id, members),
    )


def snapshot_opportunities(connection: sqlite3.Connection) -> tuple[Opportunity, ...]:
    """Read the canonical opportunity projection in deterministic key order."""
    columns = JOB_COLUMNS
    rows = connection.execute(
        f"""SELECT {columns}, m.group_id AS canonical_group_id
            FROM jobs j LEFT JOIN job_group_members m ON m.job_id=j.id
            ORDER BY CASE WHEN m.group_id IS NULL THEN 'job:' || j.id
                          ELSE 'group:' || m.group_id END, j.id"""
    ).fetchall()
    grouped: dict[str, list[StoredJob]] = {}
    group_ids: dict[str, str | None] = {}
    for row in rows:
        job = job_from_row(row)
        group_id = row["canonical_group_id"]
        key = canonical_opportunity_key(
            None if group_id is None else str(group_id), job.id
        )
        grouped.setdefault(key, []).append(job)
        group_ids[key] = str(group_id) if group_id is not None else None
    result: list[Opportunity] = []
    for key in sorted(grouped):
        members = tuple(sorted(grouped[key], key=lambda job: job.id))
        group_id = group_ids[key]
        representative = members[0]
        result.append(
            Opportunity(
                key=key,
                group_id=group_id,
                representative_job_id=representative.id,
                members=members,
                fingerprint=opportunity_digest(
                    key, group_id, representative.id, members
                ),
            )
        )
    return tuple(result)


def opportunity_corpus_fingerprint(
    opportunities: tuple[Opportunity, ...],
) -> str:
    return corpus_fingerprint(
        tuple((item.key, item.fingerprint) for item in opportunities)
    )


def _publish_key(opportunity: Opportunity) -> tuple[int, int]:
    parsed: list[datetime] = []
    for member in opportunity.members:
        if member.published_at is None:
            continue
        published = parse_utc(member.published_at)
        if published is not None:
            parsed.append(published)
    return (1, 0) if not parsed else (0, -int(max(parsed).timestamp()))


def items_fingerprint(items: tuple[RankingItem, ...] | list[RankingItem]) -> str:
    """Fingerprint every persisted ranking-item field in rank order."""
    return canonical_sha256(
        "careeros-ranking-items-v1",
        tuple(
            (
                item.generation_id,
                item.opportunity_key,
                item.group_id,
                item.representative_job_id,
                item.opportunity_fingerprint,
                item.rank_ordinal,
                item.raw_score,
                item.public_match_score,
            )
            for item in items
        ),
    )


def materialize_generation(
    connection: sqlite3.Connection,
    *,
    learned_mode: int,
    scores: list[MaterializedScore] | tuple[MaterializedScore, ...],
    completed_at: datetime | None = None,
    claim: Claim,
    authority_snapshot: Authority,
) -> Generation:
    """Persist one complete, unbound generation in one short transaction.

    Rebuild workers pass the authority captured immediately after claiming.  A
    heartbeat changes only lease metadata; all scorer-visible identity fields
    must nevertheless remain equal to that snapshot and the claim must still
    own the target before any generation row is inserted.
    """
    if learned_mode not in (0, 1):
        raise ValueError("learned_mode must be 0 or 1")
    authority = get_authority(connection)
    if not _same_scoring_authority(authority, authority_snapshot):
        raise ValueError("ranking authority changed during materialization")
    if not _claim_owns_authority(authority, claim, learned_mode):
        raise ValueError("ranking claim is no longer current")
    expected_learned_revision = (
        0 if learned_mode == 0 else authority_snapshot.learned_revision
    )
    expected_learned_fp = (
        EMPTY_LEARNED_FINGERPRINT
        if learned_mode == 0
        else authority_snapshot.learned_fingerprint
    )
    opportunities = snapshot_opportunities(connection)
    by_key = {item.key: item for item in opportunities}
    if any(score.opportunity != by_key.get(score.opportunity.key) for score in scores):
        raise ValueError(
            "materialized scores do not match the current opportunity authority"
        )
    ordered = sorted(
        scores,
        key=lambda score: (
            -score.public_match_score,
            *_publish_key(score.opportunity),
            score.opportunity.representative_job_id,
        ),
    )
    if len(ordered) != len(opportunities) or {
        score.opportunity.key for score in ordered
    } != set(by_key):
        raise ValueError(
            "materialized scores must contain exactly one item per opportunity"
        )
    rows = [
        RankingItem(
            generation_id=0,
            opportunity_key=score.opportunity.key,
            group_id=score.opportunity.group_id,
            representative_job_id=score.opportunity.representative_job_id,
            opportunity_fingerprint=score.opportunity.fingerprint,
            rank_ordinal=ordinal,
            raw_score=score.raw_score,
            public_match_score=score.public_match_score,
        )
        for ordinal, score in enumerate(ordered, 1)
    ]
    completed = (completed_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        current_authority = get_authority(connection)
        if not _same_scoring_authority(current_authority, authority):
            raise ValueError("ranking authority changed during materialization")
        if not _same_scoring_authority(current_authority, authority_snapshot):
            raise ValueError("ranking authority changed during materialization")
        if not _claim_owns_authority(current_authority, claim, learned_mode):
            raise ValueError("ranking claim is no longer current")
        provisional_content = canonical_sha256(
            "careeros-ranking-generation-v1",
            (
                authority.target_revision,
                learned_mode,
                authority.explicit_fingerprint,
                authority.opportunity_fingerprint,
                expected_learned_fp,
                tuple(
                    (
                        row.opportunity_key,
                        row.rank_ordinal,
                        row.raw_score,
                        row.public_match_score,
                    )
                    for row in rows
                ),
            ),
        )
        connection.execute(
            """INSERT INTO review_ranking_generations(
                content_fingerprint,target_revision,learned_mode,
                explicit_revision,explicit_fingerprint,opportunity_revision,
                opportunity_fingerprint,opportunity_count,learned_revision,
                learned_fingerprint,scorer_version,normalizer_version,
                generation_format_version,item_count,items_fingerprint,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                provisional_content,
                authority.target_revision,
                learned_mode,
                authority.explicit_revision,
                authority.explicit_fingerprint,
                authority.opportunity_revision,
                authority.opportunity_fingerprint,
                authority.opportunity_count,
                expected_learned_revision,
                expected_learned_fp,
                authority_snapshot.scorer_version,
                authority_snapshot.normalizer_version,
                authority_snapshot.generation_format_version,
                len(rows),
                "0" * 64,
                completed,
            ),
        )
        generation_id = int(
            connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        )
        rows = [
            RankingItem(
                generation_id,
                row.opportunity_key,
                row.group_id,
                row.representative_job_id,
                row.opportunity_fingerprint,
                row.rank_ordinal,
                row.raw_score,
                row.public_match_score,
            )
            for row in rows
        ]
        for row in rows:
            connection.execute(
                """INSERT INTO review_ranking_items(
                    generation_id,opportunity_key,group_id,representative_job_id,
                    opportunity_fingerprint,rank_ordinal,raw_score,public_match_score
                ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    row.generation_id,
                    row.opportunity_key,
                    row.group_id,
                    row.representative_job_id,
                    row.opportunity_fingerprint,
                    row.rank_ordinal,
                    row.raw_score,
                    row.public_match_score,
                ),
            )
        item_fp = items_fingerprint(rows)
        content_fp = canonical_sha256(
            "careeros-ranking-generation-v1",
            (
                authority.target_revision,
                learned_mode,
                authority.explicit_fingerprint,
                authority.opportunity_fingerprint,
                expected_learned_fp,
                item_fp,
            ),
        )
        connection.execute(
            """UPDATE review_ranking_generations
               SET content_fingerprint=?, items_fingerprint=? WHERE id=?""",
            (content_fp, item_fp, generation_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    generation = connection.execute(
        "SELECT * FROM review_ranking_generations WHERE id=?", (generation_id,)
    ).fetchone()
    assert generation is not None
    return Generation(
        *(
            generation[column]
            for column in (
                "id",
                "content_fingerprint",
                "target_revision",
                "learned_mode",
                "explicit_revision",
                "explicit_fingerprint",
                "opportunity_revision",
                "opportunity_fingerprint",
                "opportunity_count",
                "learned_revision",
                "learned_fingerprint",
                "scorer_version",
                "normalizer_version",
                "generation_format_version",
                "item_count",
                "items_fingerprint",
                "completed_at",
            )
        )
    )


# Short names used by rebuild callers; keeping these aliases avoids duplicating
# persistence logic in worker and administrative command code.
create_generation = materialize_generation


def _same_scoring_authority(left: Authority, right: Authority) -> bool:
    """Compare only immutable scoring identity, excluding lease timestamps."""
    return (
        left.target_revision == right.target_revision
        and left.explicit_revision == right.explicit_revision
        and left.explicit_fingerprint == right.explicit_fingerprint
        and left.opportunity_revision == right.opportunity_revision
        and left.opportunity_fingerprint == right.opportunity_fingerprint
        and left.opportunity_count == right.opportunity_count
        and left.learned_revision == right.learned_revision
        and left.learned_fingerprint == right.learned_fingerprint
        and left.scorer_version == right.scorer_version
        and left.normalizer_version == right.normalizer_version
        and left.generation_format_version == right.generation_format_version
        and left.required_mode_mask == right.required_mode_mask
    )


def _claim_owns_authority(
    authority: Authority, claim: Claim, learned_mode: int
) -> bool:
    return (
        authority.state == "building"
        and authority.claim_token == claim.token
        and authority.claim_target_revision == claim.target_revision
        and authority.target_revision == claim.target_revision
        and authority.claim_mode_mask is not None
        and authority.claim_mode_mask & (1 << learned_mode) != 0
    )
