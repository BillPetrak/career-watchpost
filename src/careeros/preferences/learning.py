"""Deterministic review-driven preference learning.

Learning is deliberately a small adapter around current review state.  Evidence
is a replaceable snapshot, while learned preferences are always derived on read.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from careeros.database.groups import list_all_groups
from careeros.database.jobs import StoredJob, get_job_by_id
from careeros.database.preferences import (
    get_preference_state,
    list_preference_evidence,
    list_suppressions,
    replace_job_evidence,
)
from careeros.database.review import decisions_for_jobs
from careeros.grouping.normalize import (
    normalize_company,
    normalize_location,
    normalize_title,
)
from careeros.preferences.models import PreferenceEvidence, preference_key
from careeros.ranking.scorer import (
    _detect_seniority,
    _normalize_employment,
    _split_employment_types,
)


@dataclass(frozen=True)
class OpportunityReference:
    """Bounded, non-sensitive provenance for one evidence example."""

    job_id: int
    title: str
    company: str | None


@dataclass(frozen=True)
class LearnedPreference:
    """One deterministic aggregate of current review evidence."""

    preference_key: str
    dimension: str
    normalized_value: str
    display_value: str
    positive_count: int
    negative_count: int
    net_evidence: int
    active: bool
    polarity: str | None
    strength_level: int
    suppressed: bool
    activation_reason: str
    positive_examples: tuple[OpportunityReference, ...]
    negative_examples: tuple[OpportunityReference, ...]


def extract_evidence(
    job: StoredJob,
    *,
    review_event_id: int,
    signal: int,
    observed_at: str,
) -> tuple[PreferenceEvidence, ...]:
    """Snapshot only the frozen structured attributes present on ``job``."""
    if signal not in (-1, 1):
        raise ValueError("learning signal must be -1 or 1")
    values: list[tuple[str, str, str]] = []
    title = normalize_title(job.title)
    company = normalize_company(job.company)
    location = normalize_location(job.location)
    if title:
        values.append(("title", title, title))
    if company:
        values.append(("company", company, company))
    if location:
        values.append(("location", location, location))
    if job.remote_status == "remote":
        values.append(("remote", "remote", "remote"))
    for employment in _split_employment_types(job.employment_type):
        normalized = _normalize_employment(employment)
        if normalized:
            values.append(("employment_type", employment.strip(), normalized))
    for seniority in _detect_seniority((job.title,)):
        values.append(("seniority", seniority, seniority))

    seen: set[tuple[str, str]] = set()
    result: list[PreferenceEvidence] = []
    for dimension, value, normalized in values:
        if (dimension, normalized) in seen:
            continue
        seen.add((dimension, normalized))
        result.append(
            PreferenceEvidence(
                job_id=job.id,
                review_event_id=review_event_id,
                dimension=dimension,
                value=value,
                normalized_value=normalized,
                signal=signal,
                observed_at=observed_at,
            )
        )
    return tuple(result)


def replace_review_evidence(
    connection: sqlite3.Connection,
    *,
    job_id: int,
    review_event_id: int,
    decision: str,
    observed_at: str,
) -> None:
    """Replace evidence for a meaningful shortlist/dismiss transition."""
    if decision not in ("shortlist", "dismiss"):
        raise ValueError("only shortlist and dismiss decisions create evidence")
    job = get_job_by_id(connection, job_id)
    if job is None:
        raise ValueError(f"job {job_id} does not exist")
    state = get_preference_state(connection)
    if review_event_id <= state.learning_reset_review_event_id:
        replace_job_evidence(connection, job_id=job_id, evidence=())
        return
    replace_job_evidence(
        connection,
        job_id=job_id,
        evidence=extract_evidence(
            job,
            review_event_id=review_event_id,
            signal=1 if decision == "shortlist" else -1,
            observed_at=observed_at,
        ),
    )


def remove_review_evidence(connection: sqlite3.Connection, *, job_id: int) -> None:
    """Remove the current evidence snapshot for one job."""
    replace_job_evidence(connection, job_id=job_id, evidence=())


def derive_learned_preferences(
    connection: sqlite3.Connection,
) -> tuple[LearnedPreference, ...]:
    """Derive aggregates from current evidence, collapsing canonical groups."""
    state = get_preference_state(connection)
    evidence = [
        row
        for row in list_preference_evidence(connection)
        if row.review_event_id > state.learning_reset_review_event_id
    ]
    by_job: dict[int, list[PreferenceEvidence]] = defaultdict(list)
    for row in evidence:
        by_job[row.job_id].append(row)

    groups = list_all_groups(connection)
    member_ids = [member.id for group in groups for member in group.members]
    decisions = decisions_for_jobs(connection, member_ids)
    selected: list[PreferenceEvidence] = []
    for group in groups:
        decided = [member.id for member in group.members if member.id in decisions]
        if not decided:
            continue
        # Match the existing review-group authority exactly: the lowest-ID
        # member with a current decision owns the canonical opportunity, even
        # when that decision predates the learning boundary and has no evidence.
        selected.extend(by_job.get(min(decided), ()))

    jobs = {row.job_id: get_job_by_id(connection, row.job_id) for row in selected}
    aggregates: dict[tuple[str, str], list[PreferenceEvidence]] = defaultdict(list)
    for row in selected:
        aggregates[(row.dimension, row.normalized_value)].append(row)
    suppressed = {
        (row.dimension, row.normalized_value) for row in list_suppressions(connection)
    }
    result: list[LearnedPreference] = []
    for dimension, normalized in sorted(aggregates):
        rows = aggregates[(dimension, normalized)]
        positive = sum(row.signal == 1 for row in rows)
        negative = sum(row.signal == -1 for row in rows)
        net = positive - negative
        polarity: str | None = None
        if positive >= 2 and net >= 2:
            polarity = "positive"
        elif negative >= 3 and net <= -2:
            polarity = "negative"
        active = polarity is not None and (dimension, normalized) not in suppressed
        strength = min(2, abs(net) - 1) if polarity is not None else 0
        positives = _examples(rows, 1, jobs)
        negatives = _examples(rows, -1, jobs)
        display = next(row.value for row in rows if row.normalized_value == normalized)
        reason = (
            "positive threshold met"
            if polarity == "positive"
            else "negative threshold met"
            if polarity == "negative"
            else "activation threshold not met"
        )
        result.append(
            LearnedPreference(
                preference_key=preference_key(dimension, normalized),
                dimension=dimension,
                normalized_value=normalized,
                display_value=display,
                positive_count=positive,
                negative_count=negative,
                net_evidence=net,
                active=active,
                polarity=polarity,
                strength_level=strength,
                suppressed=(dimension, normalized) in suppressed,
                activation_reason=reason,
                positive_examples=positives,
                negative_examples=negatives,
            )
        )
    return tuple(result)


def _examples(
    rows: list[PreferenceEvidence],
    signal: int,
    jobs: dict[int, StoredJob | None],
) -> tuple[OpportunityReference, ...]:
    result: list[OpportunityReference] = []
    for row in sorted(
        (item for item in rows if item.signal == signal), key=lambda item: item.job_id
    ):
        job = jobs[row.job_id]
        if job is not None:
            result.append(OpportunityReference(job.id, job.title, job.company))
        if len(result) == 3:
            break
    return tuple(result)
