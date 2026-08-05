"""Shared execution pipeline for search and rank.

This module is the single translation layer that turns a query (or a saved
search definition) into results using the existing search, grouping, ranking,
and application-filter implementations. ``cli.run_search``, ``cli.run_rank``,
and ``saved-search run`` all funnel through here, so saved searches behave
exactly like the explicit commands and no business logic is duplicated.

Everything here is read-only with respect to jobs, groups, applications, and
collection history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from careeros.applications.service import (
    ApplicationFilter,
    application_statuses_for_jobs,
    apply_filter_to_flat,
    apply_filter_to_groups,
)
from careeros.database.groups import GroupedJob, search_grouped
from careeros.database.jobs import StoredJob, search_jobs
from careeros.ranking.feedback import FeedbackSet
from careeros.ranking.scorer import (
    CandidateProfile,
    RankedGroup,
    RankedJob,
    score_groups,
    score_jobs,
)
from careeros.saved_searches.models import SavedSearch


@dataclass(frozen=True)
class SearchOutcome:
    """Filtered search results plus tracking annotations.

    Exactly one of ``flat`` or ``groups`` is populated, matching ``grouped``.
    """

    grouped: bool
    flat: tuple[StoredJob, ...]
    groups: tuple[GroupedJob, ...]
    tracking_statuses: dict[int, str] | None


@dataclass(frozen=True)
class RankOutcome:
    """Scored, min-score-filtered rank results plus tracking annotations.

    ``flat_candidate_count`` / ``grouped_candidate_count`` are the pre-score
    counts so renderers can distinguish "no matching jobs" from "no jobs met
    the minimum score".
    """

    grouped: bool
    flat_ranked: tuple[RankedJob, ...]
    grouped_ranked: tuple[RankedGroup, ...]
    flat_candidate_count: int
    grouped_candidate_count: int
    tracking_statuses: dict[int, str] | None


def _application_filter(
    *,
    untracked: bool,
    include_statuses: tuple[str, ...],
    exclude_statuses: tuple[str, ...],
) -> ApplicationFilter:
    """Build the reusable application-aware filter from raw options."""
    return ApplicationFilter(
        untracked=untracked,
        include_statuses=frozenset(include_statuses),
        exclude_statuses=frozenset(exclude_statuses),
    )


def _filter_results(
    connection: sqlite3.Connection,
    *,
    flat: tuple[StoredJob, ...],
    groups: tuple[GroupedJob, ...],
    grouped: bool,
    application_filter: ApplicationFilter,
) -> tuple[
    tuple[StoredJob, ...],
    tuple[GroupedJob, ...],
    dict[int, str] | None,
]:
    """Apply the application filter, returning (flat, groups, statuses).

    When no tracking rule is active, results pass through unchanged with no
    status map (so no ``Application:`` lines are rendered), matching the
    explicit commands.
    """
    if not application_filter.applies():
        return flat, groups, None

    if grouped:
        candidate_ids = {member.id for group in groups for member in group.members}
        statuses = application_statuses_for_jobs(connection, candidate_ids)
        filtered_groups = apply_filter_to_groups(groups, statuses, application_filter)
        return flat, tuple(filtered_groups), statuses

    candidate_ids = {job.id for job in flat}
    statuses = application_statuses_for_jobs(connection, candidate_ids)
    filtered_flat = apply_filter_to_flat(flat, statuses, application_filter)
    return tuple(filtered_flat), groups, statuses


def search_candidates(
    connection: sqlite3.Connection,
    *,
    query: str,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    limit: int = 20,
    grouped: bool = False,
    untracked: bool = False,
    include_statuses: tuple[str, ...] = (),
    exclude_statuses: tuple[str, ...] = (),
) -> SearchOutcome:
    """Run the shared flat/grouped search pipeline."""
    flat_results: tuple[StoredJob, ...]
    group_results: tuple[GroupedJob, ...]
    if grouped:
        flat_results = ()
        group_results = tuple(
            search_grouped(
                connection,
                query,
                remote=remote,
                location=location,
                source=source,
                limit=limit,
            )
        )
    else:
        flat_results = tuple(
            search_jobs(
                connection,
                query,
                remote=remote,
                location=location,
                source=source,
                limit=limit,
            )
        )
        group_results = ()

    filtered_flat, filtered_groups, statuses = _filter_results(
        connection,
        flat=flat_results,
        groups=group_results,
        grouped=grouped,
        application_filter=_application_filter(
            untracked=untracked,
            include_statuses=include_statuses,
            exclude_statuses=exclude_statuses,
        ),
    )
    return SearchOutcome(
        grouped=grouped,
        flat=filtered_flat,
        groups=filtered_groups,
        tracking_statuses=statuses,
    )


def rank_candidates(
    connection: sqlite3.Connection,
    *,
    query: str,
    profile: CandidateProfile,
    remote: bool | None = None,
    location: str | None = None,
    source: str | None = None,
    limit: int = 20,
    grouped: bool = False,
    min_score: int = 0,
    untracked: bool = False,
    include_statuses: tuple[str, ...] = (),
    exclude_statuses: tuple[str, ...] = (),
    feedback: FeedbackSet | None = None,
) -> RankOutcome:
    """Run the shared flat/grouped rank pipeline (search + score + min filter).

    ``feedback`` carries the review-feedback set, built once by the caller for
    the whole command/report operation; None disables review feedback entirely.
    The set is shared by every candidate so the database is queried exactly
    once per command.
    """
    outcome = search_candidates(
        connection,
        query=query,
        remote=remote,
        location=location,
        source=source,
        limit=limit,
        grouped=grouped,
        untracked=untracked,
        include_statuses=include_statuses,
        exclude_statuses=exclude_statuses,
    )

    if grouped:
        candidate_count = len(outcome.groups)
        ranked_groups: Sequence[RankedGroup] = score_groups(
            outcome.groups, profile, feedback=feedback
        )
        if min_score > 0:
            ranked_groups = [
                item for item in ranked_groups if item.match_score >= min_score
            ]
        return RankOutcome(
            grouped=True,
            flat_ranked=(),
            grouped_ranked=tuple(ranked_groups),
            flat_candidate_count=0,
            grouped_candidate_count=candidate_count,
            tracking_statuses=outcome.tracking_statuses,
        )

    candidate_count = len(outcome.flat)
    ranked_jobs: Sequence[RankedJob] = score_jobs(
        outcome.flat, profile, feedback=feedback
    )
    if min_score > 0:
        ranked_jobs = [item for item in ranked_jobs if item.match_score >= min_score]
    return RankOutcome(
        grouped=False,
        flat_ranked=tuple(ranked_jobs),
        grouped_ranked=(),
        flat_candidate_count=candidate_count,
        grouped_candidate_count=0,
        tracking_statuses=outcome.tracking_statuses,
    )


def execute_search(
    connection: sqlite3.Connection,
    definition: SavedSearch,
) -> SearchOutcome:
    """Run a stored saved search in search mode (thin definition adapter)."""
    return search_candidates(
        connection,
        query=definition.query,
        remote=definition.remote,
        location=definition.location,
        source=definition.source,
        limit=definition.result_limit,
        grouped=definition.grouped,
        untracked=definition.untracked,
        include_statuses=definition.include_statuses,
        exclude_statuses=definition.exclude_statuses,
    )


def execute_rank(
    connection: sqlite3.Connection,
    definition: SavedSearch,
    *,
    profile: CandidateProfile,
    feedback: FeedbackSet | None = None,
) -> RankOutcome:
    """Run a stored saved search in rank mode (thin definition adapter)."""
    return rank_candidates(
        connection,
        query=definition.query,
        profile=profile,
        remote=definition.remote,
        location=definition.location,
        source=definition.source,
        limit=definition.result_limit,
        grouped=definition.grouped,
        min_score=definition.min_score or 0,
        untracked=definition.untracked,
        include_statuses=definition.include_statuses,
        exclude_statuses=definition.exclude_statuses,
        feedback=feedback,
    )
