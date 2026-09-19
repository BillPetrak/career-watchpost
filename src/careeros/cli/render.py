"""Shared text/JSON renderers for the CareerOS CLI.

All display and structured-output helpers used by more than one command handler
live here so handlers never import each other. This module must never import
command modules, the parser, or the main dispatch module.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from careeros.applications.service import GroupTrackingInfo, group_tracking_info
from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.presentation import enum_label
from careeros.presenters import ranked_group_dict, ranked_job_dict
from careeros.ranking.scorer import (
    MatchResult,
    RankedGroup,
    RankedJob,
    feedback_summary,
)
from careeros.saved_searches.execution import RankOutcome

_PREFERRED_MISSING_DISPLAY_CAP = 8


def _sources_line(group: GroupedJob) -> str:
    """Render a human-readable source list for a grouped result."""
    if len(group.sources) == 1:
        return str(group.sources[0])
    return ", ".join(str(s) for s in group.sources)


def _print_grouped_results(
    results: Sequence[GroupedJob],
    tracking_statuses: dict[int, str] | None = None,
) -> None:
    """Print grouped search results with per-group member details."""
    for i, group in enumerate(results, start=1):
        rep = group.representative
        location_part = rep.location or "N/A"
        if rep.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        print(f"  {i}. {rep.title}")
        print(f"     Company:  {rep.company or 'N/A'}")
        print(f"     Location: {location_part}")
        print(f"     Source:   {_sources_line(group)}")
        if tracking_statuses is not None:
            print(
                "     Application: "
                f"{_application_line(group_tracking_info(group, tracking_statuses))}"
            )
        member_count = len(group.members)
        if member_count > 1:
            print(f"     Group:    {member_count} listings grouped")
        print()

        if member_count > 1:
            print("     Members:")
            for member in group.members:
                print(f"       - [{member.id}] {member.source}: {member.url or 'N/A'}")
        print()


def _render_search_flat(
    results: Sequence[StoredJob],
    tracking_statuses: dict[int, str] | None,
) -> None:
    """Render flat search results (shared by search and saved-search run)."""
    print(f"Found {len(results)} matching job{'s' if len(results) != 1 else ''}:")
    print()

    for i, job in enumerate(results, start=1):
        location_part = job.location or "N/A"
        if job.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        print(f"  {i}. {job.title}")
        print(f"     Company:  {job.company or 'N/A'}")
        print(f"     Location: {location_part}")
        print(f"     Source:   {job.source}")
        if tracking_statuses is not None:
            print(
                "     Application: "
                f"{_format_application_status(tracking_statuses.get(job.id))}"
            )
        print()


def _render_search_grouped(
    results: Sequence[GroupedJob],
    tracking_statuses: dict[int, str] | None,
) -> None:
    """Render grouped search results (shared by search and saved-search run)."""
    print(f"Found {len(results)} matching group{'s' if len(results) != 1 else ''}:")
    print()
    _print_grouped_results(results, tracking_statuses)


def _render_rank_flat(
    ranked: Sequence[RankedJob],
    tracking_statuses: dict[int, str] | None,
    *,
    min_score: int,
    candidate_count: int,
) -> None:
    """Render flat rank results (shared by rank and saved-search run)."""
    if candidate_count == 0:
        print("No matching jobs found.")
        return
    if not ranked:
        if min_score > 0:
            print(f"No jobs meet the minimum score of {min_score}.")
        else:
            print("No matching jobs found.")
        return

    print(f"Ranked {len(ranked)} job{'s' if len(ranked) != 1 else ''}:")
    print()

    for i, job in enumerate(ranked, start=1):
        location_part = job.job.location or "N/A"
        if job.job.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        print(f"{i}. [Score {job.match_score}/100] {job.job.title}")
        print(f"   Match:    {job.match_score}/100")
        print(f"   ID:       {job.job.id}")
        print(f"   Company:  {job.job.company or 'N/A'}")
        print(f"   Location: {location_part}")
        print(f"   Remote:   {enum_label(job.job.remote_status)}")
        print(f"   Source:   {job.job.source}")
        print(f"   URL:      {job.job.url or 'N/A'}")
        if tracking_statuses is not None:
            print(
                "   Application: "
                f"{_format_application_status(tracking_statuses.get(job.job.id))}"
            )
        _print_match_sections(job.result)
        _print_feedback_line(job.result)
        print("   Reasons:")
        for reason in job.reasons:
            print(f"     {reason}")
        print()


def _render_rank_grouped(
    ranked: Sequence[RankedGroup],
    tracking_statuses: dict[int, str] | None,
    *,
    min_score: int,
    candidate_count: int,
) -> None:
    """Render grouped rank results (shared by rank and saved-search run)."""
    if candidate_count == 0:
        print("No matching jobs found.")
        return
    if not ranked:
        if min_score > 0:
            print(f"No groups meet the minimum score of {min_score}.")
        else:
            print("No matching jobs found.")
        return

    print(f"Ranked {len(ranked)} group{'s' if len(ranked) != 1 else ''}:")
    print()

    for i, ranked_group in enumerate(ranked, start=1):
        group = ranked_group.group
        rep = group.representative
        location_part = rep.location or "N/A"
        if rep.remote_status == "remote":
            location_part = f"{location_part} (remote)"

        urls = ", ".join(url for url in group.urls if url)
        print(f"{i}. [Score {ranked_group.match_score}/100] {rep.title}")
        print(f"   Match:    {ranked_group.match_score}/100")
        print(f"   ID:       {rep.id}")
        print(f"   Company:  {rep.company or 'N/A'}")
        print(f"   Location: {location_part}")
        print(f"   Remote:   {enum_label(rep.remote_status)}")
        print(f"   Source:   {_sources_line(group)}")
        print(f"   URL:      {urls or 'N/A'}")
        if tracking_statuses is not None:
            application_label = _application_line(
                group_tracking_info(group, tracking_statuses)
            )
            print(f"   Application: {application_label}")
        member_count = len(group.members)
        if member_count > 1:
            print(f"   Group:    {member_count} listings grouped")
        _print_match_sections(ranked_group.result)
        _print_feedback_line(ranked_group.result)
        print("   Reasons:")
        for reason in ranked_group.reasons:
            print(f"     {reason}")
        print()


def render_rank_json(outcome: RankOutcome) -> str:
    """Render rank results as deterministic JSON carrying the same data.

    The JSON always exposes the ``score_components`` breakdown for every result,
    so the structured output is strictly more detailed than the default text
    renderer (which keeps the existing human-readable layout).
    """
    return json.dumps(_rank_outcome_dict(outcome), indent=2, sort_keys=True) + "\n"


def _rank_outcome_dict(outcome: RankOutcome) -> dict[str, object]:
    """Serialize a rank outcome (flat or grouped) as one JSON object."""
    return {
        "grouped": outcome.grouped,
        "candidate_count": (
            outcome.grouped_candidate_count
            if outcome.grouped
            else outcome.flat_candidate_count
        ),
        "jobs": [ranked_job_dict(ranked) for ranked in outcome.flat_ranked],
        "groups": [ranked_group_dict(ranked) for ranked in outcome.grouped_ranked],
        "tracking_statuses": outcome.tracking_statuses,
    }


def _format_application_status(status: str | None) -> str:
    """Render a stored application status (or 'Untracked') for display."""
    return enum_label(status, empty="Untracked")


def _application_line(info: GroupTrackingInfo) -> str:
    """Render one 'Application:' line for a grouped result.

    Distinguishes untracked results, results tracked directly on the displayed
    representative, and results tracked through another member of the group.
    """
    if info.status is None:
        return "Untracked"
    label = enum_label(info.status)
    if info.on_representative or info.owner is None:
        return label
    return f"{label} (via job {info.owner.id}, {info.owner.source})"


def _print_feedback_line(result: MatchResult) -> None:
    """Print the ``Feedback:`` line when a non-zero feedback adjustment applies."""
    if result.feedback is None or result.feedback.points == 0:
        return
    print(f"   Feedback: {feedback_summary(result.feedback)}")


def _print_match_sections(result: MatchResult) -> None:
    """Print the Matched / Missing or unmatched summary for a scored result."""
    print("     Matched:")
    printed = False
    for title in result.matched_titles:
        print(f"       - Title: '{title}'")
        printed = True
    for skill in result.matched_skills:
        print(f"       - {skill}")
        printed = True
    if result.location_matched is not None:
        print(f"       - Location: {result.location_matched}")
        printed = True
    if result.remote_matched:
        print("       - Remote position")
        printed = True
    if result.employment_matched is not None:
        print(f"       - Employment type: {result.employment_matched}")
        printed = True
    if result.seniority_matched is not None:
        print(f"       - Seniority: {result.seniority_matched}")
        printed = True
    if not printed:
        print("       - none")

    print("     Missing or unmatched:")
    printed = False
    if result.description_unavailable:
        print("       - Description unavailable; skills not evaluated")
        printed = True
    else:
        for skill in result.missing_required_skills:
            print(f"       - {skill} (required, not found)")
            printed = True
        preferred = result.preferred_skills_not_observed
        for skill in preferred[:_PREFERRED_MISSING_DISPLAY_CAP]:
            print(f"       - {skill} (preferred, not observed)")
            printed = True
        remaining = len(preferred) - _PREFERRED_MISSING_DISPLAY_CAP
        if remaining > 0:
            print(
                f"       - ... and {remaining} more preferred skill"
                f"{'s' if remaining != 1 else ''}"
            )
            printed = True
    if result.employment_mismatch:
        print("       - Employment type mismatch")
        printed = True
    if result.seniority_mismatch:
        print("       - Seniority mismatch")
        printed = True
    for term in result.excluded_terms:
        print(f"       - Contains excluded term '{term}'")
        printed = True
    for term in result.hard_excluded_terms:
        print(f"       - Hard excluded: contains '{term}'")
        printed = True
    if not printed:
        print("       - none")


def _reminder_state_text(completed_at: str | None) -> str:
    """Render a reminder's completion state for display."""
    return "open" if completed_at is None else f"completed {completed_at}"


def _reminder_message_line(message: str | None) -> str | None:
    """Render a reminder message line, or None when absent."""
    return f"Message: {message}" if message is not None else None
