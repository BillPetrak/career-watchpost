"""Review decision vocabulary, states, and domain errors.

Decision values are validated centrally here and exposed consistently to the
Python layer (via :func:`validate_decision`) and the CLI (via argparse choices).
"""

from __future__ import annotations

DECISION_VALUES: tuple[str, ...] = (
    "shortlist",
    "dismiss",
    "review_later",
)

# Queue-states vocabulary used by ``queue next --state``. ``unreviewed`` is the
# default daily-review state; ``any`` includes every group regardless of
# decision.
QUEUE_STATES: tuple[str, ...] = (
    "unreviewed",
    "shortlist",
    "dismiss",
    "review_later",
    "any",
)


class ReviewError(Exception):
    """Base class for review-decision domain errors."""


class UnknownJobError(ReviewError):
    """Raised when a decision is set or cleared for a job id that does not exist."""


class ValidationError(ReviewError):
    """Raised when a decision value or queue-state argument is invalid."""


def validate_decision(decision: str) -> str:
    """Return the decision unchanged when it is a known review decision.

    Raises ``ValueError`` for unknown decisions so the service layer can rely on
    a single authoritative vocabulary regardless of which caller invokes it.
    """
    if decision not in DECISION_VALUES:
        choices = ", ".join(DECISION_VALUES)
        raise ValueError(
            f"Invalid review decision {decision!r}; expected one of: {choices}."
        )
    return decision


def validate_queue_state(state: str) -> str:
    """Return the state unchanged when it is a known queue state.

    Raises :class:`ValidationError` for unknown states so the service layer can
    reject bad filters deterministically regardless of the caller.
    """
    if state not in QUEUE_STATES:
        choices = ", ".join(QUEUE_STATES)
        raise ValidationError(
            f"Invalid queue state {state!r}; expected one of: {choices}."
        )
    return state
