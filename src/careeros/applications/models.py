"""Application-status vocabulary shared across the service and CLI.

Status values are validated centrally here and exposed consistently to the
Python layer (via :func:`validate_status`) and the CLI (via argparse choices).
"""

from __future__ import annotations

APPLICATION_STATUSES: tuple[str, ...] = (
    "interested",
    "applied",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
)

ACTIVE_APPLICATION_STATUSES: tuple[str, ...] = (
    "interested",
    "applied",
    "interview",
    "offer",
)
COMPLETED_APPLICATION_STATUSES: tuple[str, ...] = ("rejected", "withdrawn")
APPLICATION_PIPELINE_VIEWS: tuple[str, ...] = (
    "active",
    "completed",
    "all",
    *ACTIVE_APPLICATION_STATUSES,
)


def validate_status(status: str) -> str:
    """Return the status unchanged when it is a known application status.

    Raises ``ValueError`` for unknown statuses so the service layer can rely on
    a single authoritative vocabulary regardless of which caller invokes it.
    """
    if status not in APPLICATION_STATUSES:
        choices = ", ".join(APPLICATION_STATUSES)
        raise ValueError(
            f"Invalid application status {status!r}; expected one of: {choices}."
        )
    return status
