"""Deterministic renderers for the daily review queue.

Both the default text renderer and the JSON renderer consume the same immutable
:class:`~careeros.review.service.QueueOutcome` produced by
:mod:`careeros.review.service`; no business logic is reimplemented here. Text
output is readable in a terminal and JSON is sorted-key and independently
deterministic. JSON keys follow the existing CareerOS report conventions
(``match_score``, ``score``, ``reasons``, and the shared job/group shapes).
"""

from __future__ import annotations

import json
from typing import Any

from careeros.applications.service import GroupTrackingInfo
from careeros.database.groups import GroupedJob
from careeros.database.jobs import StoredJob
from careeros.review.service import GroupDecision, QueueItem, QueueOutcome


def render_queue_text(outcome: QueueOutcome) -> str:
    """Render the review queue as deterministic plain text."""
    lines = [
        "Review queue",
        "------------",
        f"Mode:    {'grouped' if outcome.grouped else 'flat'}",
        f"State:   {outcome.state}",
    ]
    if outcome.min_score > 0:
        lines.append(f"Min score: {outcome.min_score}")
    if outcome.include_tracked:
        lines.append("Including tracked applications")
    lines.append("")

    if outcome.candidate_count == 0:
        lines.append("No eligible opportunities found.")
        return "\n".join(lines) + "\n"

    if not outcome.items:
        noun = "groups" if outcome.grouped else "jobs"
        if outcome.min_score > 0:
            lines.append(
                f"No eligible {noun} meet the minimum score of {outcome.min_score}."
            )
        else:
            lines.append("No matching opportunities found.")
        return "\n".join(lines) + "\n"

    noun = "group" if outcome.grouped else "job"
    plural = "s" if len(outcome.items) != 1 else ""
    lines.append(f"Ranked {len(outcome.items)} eligible {noun}{plural}:")
    lines.append("")
    for index, item in enumerate(outcome.items, start=1):
        lines.extend(_queue_item_lines(index, item))

    return "\n".join(lines) + "\n"


def render_queue_json(outcome: QueueOutcome) -> str:
    """Render the review queue as deterministic JSON."""
    return json.dumps(_queue_to_dict(outcome), indent=2, sort_keys=True) + "\n"


def _decision_label(group_decision: GroupDecision) -> str:
    """Render a resolved group decision for display."""
    if group_decision.decision is None:
        return "unreviewed"
    return group_decision.decision


def _application_label(info: GroupTrackingInfo) -> str:
    """Render one 'Application:' label, mirroring the search/rank convention.

    Distinguishes untracked groups, groups tracked directly on the displayed
    representative, and groups tracked through another member.
    """
    if info.status is None:
        return "Untracked"
    label = info.status.capitalize()
    if info.on_representative or info.owner is None:
        return label
    return f"{label} (via job {info.owner.id}, {info.owner.source})"


def _queue_item_lines(index: int, item: QueueItem) -> list[str]:
    """Render one ranked queue entry as labeled lines."""
    group = item.group
    rep = group.representative
    location = rep.location or "N/A"
    if rep.remote_status == "remote":
        location = f"{location} (remote)"

    lines = [
        f"{index}. [Score {item.ranked.match_score}/100] {rep.title}",
        f"   Decision: {_decision_label(item.group_decision)}",
        f"   Company:  {rep.company or 'N/A'}",
        f"   Location: {location}",
        f"   Source:   {', '.join(group.sources)}",
    ]
    if item.group_decision.conflict and item.group_decision.owner is not None:
        owner = item.group_decision.owner
        lines.append(f"   Conflict: resolved from job {owner.id} ({owner.source})")
    if item.application.status is not None:
        lines.append(f"   Application: {_application_label(item.application)}")
    member_count = len(group.members)
    if member_count > 1:
        lines.append(f"   Group:    {member_count} listings grouped")
    lines.append("   Reasons:")
    for reason in item.ranked.reasons:
        lines.append(f"     {reason}")
    lines.append("")
    return lines


def _job_dict(job: StoredJob) -> dict[str, Any]:
    """Serialize a stored job with the stable CareerOS job keys."""
    return {
        "id": job.id,
        "source": job.source,
        "source_job_id": job.source_job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "remote_status": job.remote_status,
        "employment_type": job.employment_type,
        "published_at": job.published_at,
        "url": job.url,
    }


def _group_dict(group: GroupedJob) -> dict[str, Any]:
    """Serialize a canonical group with the stable CareerOS group keys."""
    return {
        "group_id": group.group_id,
        "representative": _job_dict(group.representative),
        "members": [_job_dict(member) for member in group.members],
        "sources": list(group.sources),
        "urls": list(group.urls),
    }


def _queue_item_dict(item: QueueItem) -> dict[str, Any]:
    """Serialize one ranked queue entry."""
    group_decision = item.group_decision
    return {
        "decision": group_decision.decision,
        "decision_conflict": group_decision.conflict,
        "decision_owner": (
            _job_dict(group_decision.owner)
            if group_decision.owner is not None
            else None
        ),
        "match_score": item.ranked.match_score,
        "score": item.ranked.score,
        "reasons": list(item.ranked.reasons),
        "group": _group_dict(item.group),
        "application_status": item.application.status,
        "application_owner": (
            _job_dict(item.application.owner)
            if item.application.owner is not None
            else None
        ),
        "application_on_representative": item.application.on_representative,
    }


def _queue_to_dict(outcome: QueueOutcome) -> dict[str, Any]:
    """Serialize the whole queue outcome."""
    return {
        "grouped": outcome.grouped,
        "state": outcome.state,
        "min_score": outcome.min_score,
        "include_tracked": outcome.include_tracked,
        "candidate_count": outcome.candidate_count,
        "items": [_queue_item_dict(item) for item in outcome.items],
    }


__all__: tuple[str, ...] = (
    "render_queue_json",
    "render_queue_text",
)
