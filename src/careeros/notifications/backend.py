"""Notification delivery backends: deterministic stdout and local desktop.

The desktop backend shells out to the standard ``notify-send`` command with an
argument list (never ``shell=True``), a bounded timeout, and sanitized title and
body. Executable-not-found, timeout, nonzero exit, and unavailable display or
session bus are each converted into a structured :class:`DeliveryResult` so the
caller can record a retryable failure. Nothing here ever uses the network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Protocol

from careeros.notifications.models import (
    BackendUnavailable,
    DeliveryResult,
    DueOccurrence,
)

NOTIFY_SEND_TIMEOUT_SECONDS = 10.0
NOTIFY_EXPIRE_MILLISECONDS = 10000
_TITLE_MAX = 60
_BODY_MAX = 300

# C0 control characters (including NUL, ESC, CR, LF, TAB) are replaced with a
# space before display so no raw control bytes reach notify-send's argv.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Display labels for the machine reminder kinds.
KIND_LABELS: dict[str, str] = {
    "follow_up": "Follow up",
    "interview": "Interview",
    "offer": "Offer",
    "review": "Review",
    "custom": "Reminder",
}


class _Dispatcher(Protocol):
    def deliver(self, occurrence: DueOccurrence) -> DeliveryResult: ...


def resolve_backend(preference: str) -> tuple[str, str]:
    """Resolve a backend preference to an actual backend plus a note.

    ``stdout`` is always usable. ``desktop`` requires ``notify-send`` on PATH.
    ``auto`` selects desktop only when a usable graphical session (a matching
    display variable for the session type) and a session bus are present;
    otherwise it falls back to ``stdout`` with an explanatory note, so a
    headless timer run never fails on environment alone.
    """
    if preference == "stdout":
        return "stdout", ""
    if preference == "desktop":
        if shutil.which("notify-send") is None:
            raise BackendUnavailable(
                "--backend desktop requires notify-send on PATH, but it was not found."
            )
        return "desktop", ""
    if (
        shutil.which("notify-send") is not None
        and _graphical_session()
        and _session_bus()
    ):
        return "desktop", ""
    return "stdout", "fallback; no usable graphical session or notify-send"


def _graphical_session() -> bool:
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type == "wayland":
        return bool(os.environ.get("WAYLAND_DISPLAY"))
    return bool(os.environ.get("DISPLAY"))


def _session_bus() -> bool:
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def format_occurrence_line(occurrence: DueOccurrence) -> str:
    """Render one occurrence as a deterministic single stdout line."""
    company = occurrence.job_company or "N/A"
    line = (
        f"[notify] reminder {occurrence.reminder_id} job {occurrence.job_id}: "
        f"{occurrence.job_title} ({company}, {occurrence.job_source}) - "
        f"{occurrence.kind} due {occurrence.due_at}"
    )
    if occurrence.message is not None:
        line += f"; message={_clean(occurrence.message)}"
    return line


def deliver(backend: str, occurrence: DueOccurrence) -> DeliveryResult:
    """Dispatch one occurrence through the selected backend."""
    if backend == "stdout":
        print(format_occurrence_line(occurrence))
        return DeliveryResult(success=True)
    return _deliver_desktop(occurrence)


def _deliver_desktop(occurrence: DueOccurrence) -> DeliveryResult:
    executable = shutil.which("notify-send")
    if executable is None:
        return DeliveryResult(success=False, error="notify-send not found")
    title = _title(occurrence)
    body = _body(occurrence)
    try:
        result = subprocess.run(
            [
                executable,
                "--app-name",
                "CareerOS",
                "--urgency",
                "normal",
                "--expire-time",
                str(NOTIFY_EXPIRE_MILLISECONDS),
                title,
                body,
            ],
            timeout=NOTIFY_SEND_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return DeliveryResult(success=False, error="notify-send timed out")
    except OSError as error:
        return DeliveryResult(success=False, error=f"notify-send failed: {error}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "unknown error"
        return DeliveryResult(
            success=False,
            error=f"notify-send exited {result.returncode}: {tail}",
        )
    return DeliveryResult(success=True)


def _title(occurrence: DueOccurrence) -> str:
    label = KIND_LABELS.get(occurrence.kind, occurrence.kind)
    return _clean(f"CareerOS: {label} reminder")[:_TITLE_MAX]


def _body(occurrence: DueOccurrence) -> str:
    company = occurrence.job_company or "N/A"
    body = (
        f"{occurrence.job_title}\n{company} - {occurrence.job_source}\n"
        f"Due {occurrence.due_at}"
    )
    if occurrence.message is not None:
        body += f"\n{occurrence.message}"
    return _clean(body)[:_BODY_MAX]


def _clean(value: str) -> str:
    """Collapse whitespace and control characters to single spaces."""
    return " ".join(_CONTROL_RE.sub(" ", value).split())


__all__: tuple[str, ...] = (
    "KIND_LABELS",
    "NOTIFY_EXPIRE_MILLISECONDS",
    "NOTIFY_SEND_TIMEOUT_SECONDS",
    "BackendUnavailable",
    "deliver",
    "format_occurrence_line",
    "resolve_backend",
)
