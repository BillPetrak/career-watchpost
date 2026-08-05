"""Pure, deterministic review-feedback adjustments for ranking.

This module is the presentation- and database-agnostic core of the review
feedback feature. It holds the immutable feedback source/adjustment models and
the strict similarity evaluation; it performs no I/O. The read-only adapter in
``careeros.review.feedback`` translates stored review decisions into a
:class:`FeedbackSet`, and the shared scorer
(:func:`careeros.ranking.scorer.score_facts`) applies one adjustment per
candidate.

Fixed weights (documented constants, never candidate-profile fields):

- ``shortlist`` contributes ``+5``
- ``dismiss`` contributes ``-5``
- ``review_later`` always contributes ``0`` (neutral, never occupies a slot)
- at most ``MAX_CONTRIBUTING_SOURCES`` (2) reviewed sources contribute, so the
  total adjustment is capped at ``+10`` / ``-10``

Similarity is deliberately conservative and mirrors the duplicate-grouping
primitives in ``careeros.grouping.normalize`` without modifying them:

- normalized title compatibility is required (equal signature, or one being a
  token-prefix of the other);
- company acts as an anchor: equal companies are similar on compatible titles;
  with different companies, only sufficiently specific (two-or-more-token)
  compatible titles count;
- an explicit remote-status conflict vetoes similarity;
- incompatible non-empty locations veto similarity;
- explicitly disjoint employment types veto similarity;
- skills are never used as a feedback-similarity channel;
- a candidate never receives feedback from itself;
- ``review_later`` is always neutral.

Feedback never hard-excludes a candidate and can never bypass an existing
hard-exclusion veto: it only ever adds or subtracts bounded points from the raw
score.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from careeros.grouping.normalize import (
    normalize_company,
    normalize_location,
    normalize_title,
    remote_class,
)

# Fixed feedback weights (documented constants, never profile fields).
SHORTLIST_POINTS = 5
DISMISS_POINTS = -5
REVIEW_LATER_POINTS = 0
MAX_CONTRIBUTING_SOURCES = 2
# 2 * 5 == 10, so the total adjustment is capped at +10 / -10.
MAX_FEEDBACK_POINTS = MAX_CONTRIBUTING_SOURCES * SHORTLIST_POINTS

_FEEDBACK_DECISIONS: tuple[str, ...] = ("shortlist", "dismiss", "review_later")


def validate_decision(decision: str) -> str:
    """Return the decision unchanged when it is a known feedback decision."""
    if decision not in _FEEDBACK_DECISIONS:
        choices = ", ".join(_FEEDBACK_DECISIONS)
        raise ValueError(
            f"Invalid feedback decision {decision!r}; expected one of: {choices}."
        )
    return decision


def decision_points(decision: str) -> int:
    """Return the fixed weight of one review decision."""
    validate_decision(decision)
    if decision == "shortlist":
        return SHORTLIST_POINTS
    if decision == "dismiss":
        return DISMISS_POINTS
    return REVIEW_LATER_POINTS


@dataclass(frozen=True)
class FeedbackSource:
    """One decided canonical group: its deterministic representative + decision.

    ``job_id`` is the deciding member's stable ``jobs.id`` (the lowest decided
    id in the group), so cross-source duplicates contribute exactly once.
    """

    job_id: int
    title: str
    company: str | None
    location: str | None
    remote_status: str | None
    employment_type: str | None
    decision: str


@dataclass(frozen=True)
class FeedbackSet:
    """All decided groups for one command/report operation.

    Built once per operation by the review adapter and shared across every
    candidate so the database is queried exactly once.
    """

    sources: tuple[FeedbackSource, ...] = ()


@dataclass(frozen=True)
class CandidateJob:
    """Evidence for one candidate (a single job or a canonical group).

    ``job_ids`` holds every member's ``jobs.id`` so self-decisions are excluded
    even when a job is a duplicate member of its own canonical group.
    """

    job_ids: frozenset[int] = frozenset()
    titles: tuple[str, ...] = ()
    company: str | None = None
    location: str | None = None
    remote_status: str | None = None
    employment_type: str | None = None


@dataclass(frozen=True)
class FeedbackAdjustment:
    """The deterministic feedback adjustment for one candidate."""

    points: int
    sources: tuple[FeedbackSource, ...]


def feedback_label(source: FeedbackSource) -> str:
    """A concise human-readable label for one contributing feedback source."""
    if source.decision == "shortlist":
        noun = "shortlisted"
    elif source.decision == "dismiss":
        noun = "dismissed"
    else:
        noun = "review-later"
    return (
        f"similar to {noun} job {source.job_id} "
        f"({source.title}, {source.company or 'N/A'})"
    )


def source_points(source: FeedbackSource) -> int:
    """Return the fixed weight contributed by one feedback source."""
    return decision_points(source.decision)


def _title_relation(
    candidate_titles: tuple[str, ...],
    source_title: str,
) -> tuple[str, str] | None:
    """Return (candidate_sig, source_sig) for a compatible title pair, else None.

    Compatibility means equal normalized signatures or one being a token-prefix
    of the other, mirroring the location compatibility used by grouping.
    """
    source_sig = normalize_title(source_title)
    if not source_sig:
        return None
    source_tokens = source_sig.split()
    for title in candidate_titles:
        sig = normalize_title(title)
        if not sig:
            continue
        if sig == source_sig:
            return sig, source_sig
        sig_tokens = sig.split()
        shorter, longer = (
            (sig_tokens, source_tokens)
            if len(sig_tokens) <= len(source_tokens)
            else (source_tokens, sig_tokens)
        )
        if longer[: len(shorter)] == shorter:
            return sig, source_sig
    return None


def _remote_conflict(candidate: str | None, source: str | None) -> bool:
    """Return True when remote classes explicitly conflict (mirrors grouping)."""
    cand = remote_class(candidate)
    src = remote_class(source)
    return cand is not None and src is not None and cand != src


def _locations_compatible(a: str, b: str) -> bool:
    """Return True when normalized location signatures are token-compatible."""
    a_tokens = a.split()
    b_tokens = b.split()
    if not a_tokens or not b_tokens:
        return True
    shorter, longer = (
        (a_tokens, b_tokens) if len(a_tokens) <= len(b_tokens) else (b_tokens, a_tokens)
    )
    return longer[: len(shorter)] == shorter


def _location_conflict(candidate: str | None, source: str | None) -> bool:
    """Return True when both locations are non-empty and incompatible."""
    cand = normalize_location(candidate)
    src = normalize_location(source)
    if not cand or not src:
        return False
    return not _locations_compatible(cand, src)


def _employment_types(value: str | None) -> frozenset[str]:
    """Normalize a stored (possibly comma-joined) employment-type string."""
    if not value:
        return frozenset()
    result: set[str] = set()
    for part in value.split(","):
        term = " ".join(
            part.strip().lower().replace("-", " ").replace("_", " ").split()
        )
        if term:
            result.add(term)
    return frozenset(result)


def _employment_conflict(candidate: str | None, source: str | None) -> bool:
    """Return True when both sides state explicit, disjoint employment types."""
    cand = _employment_types(candidate)
    src = _employment_types(source)
    if not cand or not src:
        return False
    return cand.isdisjoint(src)


def sources_similar(candidate: CandidateJob, source: FeedbackSource) -> bool:
    """Return True when a candidate is conservatively similar to a source.

    The caller is responsible for excluding the candidate's own decisions via
    ``job_ids``.
    """
    relation = _title_relation(candidate.titles, source.title)
    if relation is None:
        return False
    cand_sig, source_sig = relation
    cand_company = normalize_company(candidate.company)
    source_company = normalize_company(source.company)
    same_company = bool(cand_company) and cand_company == source_company
    if not same_company:
        # Different companies: the compatible titles must both be specific
        # enough (at least two tokens) so weak shared generics never match.
        if len(cand_sig.split()) < 2 or len(source_sig.split()) < 2:
            return False
    if _remote_conflict(candidate.remote_status, source.remote_status):
        return False
    if _location_conflict(candidate.location, source.location):
        return False
    if _employment_conflict(candidate.employment_type, source.employment_type):
        return False
    return True


def compute_feedback_adjustment(
    candidate: CandidateJob,
    sources: Sequence[FeedbackSource],
) -> FeedbackAdjustment:
    """Compute one candidate's deterministic feedback adjustment.

    Self-decisions are excluded, ``review_later`` contributes nothing and never
    occupies a slot, and at most ``MAX_CONTRIBUTING_SOURCES`` similar sources
    contribute in the (deterministic) order they are provided.
    """
    contributing: list[FeedbackSource] = []
    for source in sources:
        if source.job_id in candidate.job_ids:
            continue
        if decision_points(source.decision) == 0:
            continue
        if not sources_similar(candidate, source):
            continue
        contributing.append(source)
        if len(contributing) >= MAX_CONTRIBUTING_SOURCES:
            break
    points = sum(decision_points(source.decision) for source in contributing)
    return FeedbackAdjustment(points=points, sources=tuple(contributing))


__all__: tuple[str, ...] = (
    "MAX_CONTRIBUTING_SOURCES",
    "MAX_FEEDBACK_POINTS",
    "REVIEW_LATER_POINTS",
    "DISMISS_POINTS",
    "SHORTLIST_POINTS",
    "CandidateJob",
    "FeedbackAdjustment",
    "FeedbackSet",
    "FeedbackSource",
    "compute_feedback_adjustment",
    "decision_points",
    "feedback_label",
    "source_points",
    "sources_similar",
    "validate_decision",
)
