"""The ``diagnose`` command: environment and notification readiness checks.

Strictly read-only: nothing here writes to the database, the filesystem, or the
user systemd manager. Notification readiness is reported (notify-send presence,
the backend ``auto`` resolves to and why, and the reminder-notification timer
state) so a user can tell whether due reminders will actually surface as
desktop pop-ups.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from careeros.cli.common import PROJECT_ROOT
from careeros.notifications.backend import BackendUnavailable, resolve_backend

NOTIFY_SERVICE_NAME = "careeros-reminder-notify.service"
NOTIFY_TIMER_NAME = "careeros-reminder-notify.timer"
NOTIFY_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

_SYSTEMCTL_TIMEOUT_SECONDS = 10.0


def _systemctl(arguments: list[str]) -> str | None:
    """Run a ``systemctl --user`` query, returning stripped stdout or None."""
    executable = shutil.which("systemctl")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _timer_state() -> tuple[bool, str | None, str | None]:
    """Return ``(installed, enabled, active)`` for the notify timer.

    ``installed`` reflects the unit file's presence in the user unit
    directory; ``enabled`` and ``active`` are the live systemd states, or None
    when they cannot be determined (no systemctl, timer not known to the
    manager).
    """
    installed = (NOTIFY_UNIT_DIR / NOTIFY_TIMER_NAME).exists()
    enabled = _systemctl(["--user", "is-enabled", NOTIFY_TIMER_NAME]) or None
    active = _systemctl(["--user", "is-active", NOTIFY_TIMER_NAME]) or None
    return installed, enabled, active


def _guidance(
    notify_send: str | None,
    backend_name: str,
    installed: bool,
    enabled: str | None,
) -> list[str]:
    """Build actionable next steps when notifications are not operational."""
    steps: list[str] = []
    if notify_send is None:
        steps.append(
            "Install notify-send (Ubuntu/Debian: sudo apt install libnotify-bin) "
            "to enable desktop notifications."
        )
    if backend_name != "desktop":
        steps.append(
            "Desktop delivery needs a graphical login session with a session "
            "bus; ensure the notify timer runs inside your desktop session "
            "(e.g. after logging in, run: "
            "systemctl --user import-environment DISPLAY WAYLAND_DISPLAY "
            "XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS)."
        )
    if not installed:
        steps.append(
            "Install and enable the notification timer with: "
            "uv run python systemd/install.py --units notify --enable"
        )
    elif enabled != "enabled":
        steps.append(
            f"Enable the notification timer with: systemctl --user enable --now "
            f"{NOTIFY_TIMER_NAME}"
        )
    elif enabled == "enabled" and backend_name != "desktop":
        steps.append(
            "The timer is enabled but desktop delivery is unavailable in this "
            "environment; it will fall back to stdout until a graphical "
            "session bus is reachable."
        )
    return steps


def print_diagnostics() -> int:
    """Print basic CareerOS environment and notification readiness."""
    database_directory = PROJECT_ROOT / "data" / "database"

    print("CareerOS diagnostics")
    print("--------------------")
    print(f"Project root:      {PROJECT_ROOT}")
    print(f"Python version:    {sys.version.split()[0]}")
    print(f"Python executable: {sys.executable}")
    print(f"SQLite library:    {sqlite3.sqlite_version}")
    print(f"Database directory exists: {database_directory.is_dir()}")
    print()

    notify_send = shutil.which("notify-send")
    backend_name: str
    backend_note: str
    try:
        backend_name, backend_note = resolve_backend("auto")
    except BackendUnavailable as error:  # pragma: no cover - defensive
        backend_name, backend_note = "unavailable", str(error)
    installed, enabled, active = _timer_state()

    print("Notification readiness")
    print("----------------------")
    print(f"notify-send:              {notify_send or 'not found on PATH'}")
    print(
        f"auto backend:             {backend_name}"
        + (f" ({backend_note})" if backend_note else "")
    )
    print(f"reminder notify timer:    installed: {installed}")
    print(f"reminder notify timer:    enabled:   {enabled or 'unknown'}")
    print(f"reminder notify timer:    active:    {active or 'unknown'}")
    print()
    guidance = _guidance(
        notify_send=notify_send,
        backend_name=backend_name,
        installed=installed,
        enabled=enabled,
    )
    if guidance:
        print("Guidance:")
        for step in guidance:
            print(f"  - {step}")
        print()
    else:
        print(
            "Notification delivery is operational: due reminders will surface "
            "as desktop pop-ups via the enabled timer."
        )
        print()

    print("Environment check passed.")
    return 0
