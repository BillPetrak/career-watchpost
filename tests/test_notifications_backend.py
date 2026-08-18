"""Tests for the notification backends (auto selection, notify-send handling).

All subprocess behavior is faked: nothing here talks to D-Bus or shows a real
desktop notification.
"""

from __future__ import annotations

import subprocess

import pytest

import careeros.notifications.backend as backend
from careeros.notifications.backend import (
    BackendUnavailable,
    deliver,
    format_occurrence_line,
    resolve_backend,
)
from careeros.notifications.models import DueOccurrence

_OCCURRENCE = DueOccurrence(
    reminder_id=3,
    due_at="2026-08-03T12:00:00+00:00",
    kind="follow_up",
    message="call recruiter",
    job_id=7,
    job_title="Python Engineer",
    job_company="Acme GmbH",
    job_source="arbeitnow",
)


def _patch_env(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _patch_which(monkeypatch: pytest.MonkeyPatch, result: str | None) -> None:
    def fake_which(_name: str) -> str | None:
        return result

    monkeypatch.setattr(backend.shutil, "which", fake_which)


def test_stdout_preference_always_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    _patch_env(
        monkeypatch,
        DISPLAY=":0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    assert resolve_backend("stdout") == ("stdout", "")


def test_desktop_preference_requires_notify_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    _patch_env(
        monkeypatch,
        DISPLAY=":0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    assert resolve_backend("desktop") == ("desktop", "")


def test_desktop_preference_without_notify_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, None)
    _patch_env(
        monkeypatch,
        DISPLAY=":0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    with pytest.raises(BackendUnavailable):
        resolve_backend("desktop")


def test_auto_selects_desktop_when_graphical_session_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    _patch_env(
        monkeypatch,
        XDG_SESSION_TYPE="x11",
        DISPLAY=":0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    assert resolve_backend("auto") == ("desktop", "")


def test_auto_selects_desktop_with_bus_but_no_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A systemd user service routinely inherits a session bus but no display
    # variables; the bus is the delivery signal, so this still selects desktop
    # and only records a soft note.
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    _patch_env(
        monkeypatch,
        XDG_SESSION_TYPE="x11",
        DISPLAY=None,
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    backend_name, note = resolve_backend("auto")
    assert backend_name == "desktop"
    assert "display" in note


def test_auto_wayland_uses_session_bus_not_display_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    _patch_env(
        monkeypatch,
        XDG_SESSION_TYPE="wayland",
        WAYLAND_DISPLAY=None,
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    backend_name, note = resolve_backend("auto")
    assert backend_name == "desktop"
    assert "display" in note

    _patch_env(
        monkeypatch,
        XDG_SESSION_TYPE="wayland",
        WAYLAND_DISPLAY="wayland-0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    assert resolve_backend("auto") == ("desktop", "")


def test_auto_detects_bus_via_xdg_runtime_dir_socket(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # The standard per-user bus socket at $XDG_RUNTIME_DIR/bus is enough: a
    # user timer may carry XDG_RUNTIME_DIR without DBUS_SESSION_BUS_ADDRESS.
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    _patch_env(
        monkeypatch,
        XDG_SESSION_TYPE="x11",
        DISPLAY=":0",
        DBUS_SESSION_BUS_ADDRESS=None,
        XDG_RUNTIME_DIR=None,
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "bus").touch()
    _patch_env(monkeypatch, XDG_RUNTIME_DIR=str(runtime_dir))
    assert resolve_backend("auto") == ("desktop", "")


def test_auto_falls_back_to_stdout_without_session_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # notify-send alone is not enough: without DBUS_SESSION_BUS_ADDRESS or the
    # $XDG_RUNTIME_DIR/bus socket there is no way to reach a notification
    # daemon, so auto falls back to stdout rather than failing the run.
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    _patch_env(
        monkeypatch,
        XDG_SESSION_TYPE="x11",
        DISPLAY=":0",
        DBUS_SESSION_BUS_ADDRESS=None,
        XDG_RUNTIME_DIR=None,
    )
    backend_name, note = resolve_backend("auto")
    assert backend_name == "stdout"
    assert "fallback" in note


def test_auto_falls_back_when_notify_send_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_which(monkeypatch, None)
    _patch_env(
        monkeypatch,
        XDG_SESSION_TYPE="x11",
        DISPLAY=":0",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
    )
    backend_name, note = resolve_backend("auto")
    assert backend_name == "stdout"
    assert "fallback; notify-send not found on PATH" in note


def test_stdout_backend_is_deterministic(capsys: pytest.CaptureFixture[str]) -> None:
    result = deliver("stdout", _OCCURRENCE)
    captured = capsys.readouterr()
    assert result.success is True
    assert result.error is None
    expected = (
        "[notify] reminder 3 job 7: Python Engineer (Acme GmbH, arbeitnow) - "
        "follow_up due 2026-08-03T12:00:00+00:00; message=call recruiter"
    )
    assert captured.out.strip() == expected
    assert format_occurrence_line(_OCCURRENCE) == expected


def test_stdout_line_omits_message_when_absent() -> None:
    bare = DueOccurrence(
        reminder_id=3,
        due_at="2026-08-03T12:00:00+00:00",
        kind="interview",
        message=None,
        job_id=7,
        job_title="Python Engineer",
        job_company=None,
        job_source="arbeitnow",
    )
    line = format_occurrence_line(bare)
    assert "message=" not in line
    assert "(N/A, arbeitnow)" in line


def test_notify_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs.get("shell") is not True
        assert kwargs.get("timeout") == backend.NOTIFY_SEND_TIMEOUT_SECONDS
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    result = deliver("desktop", _OCCURRENCE)
    assert result.success is True
    assert len(calls) == 1
    args = calls[0]
    assert args[0] == "/usr/bin/notify-send"
    assert "--app-name" in args and "CareerOS" in args
    assert "--expire-time" in args and str(backend.NOTIFY_EXPIRE_MILLISECONDS) in args
    assert "follow_up" not in args  # title uses a display label
    assert "CareerOS: Follow up reminder" in args
    assert "Python Engineer" in args[-1]


def test_notify_send_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, None)
    result = deliver("desktop", _OCCURRENCE)
    assert result.success is False
    assert "notify-send not found" in (result.error or "")


def test_notify_send_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, backend.NOTIFY_SEND_TIMEOUT_SECONDS)

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    result = deliver("desktop", _OCCURRENCE)
    assert result.success is False
    assert "timed out" in (result.error or "")


def test_notify_send_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="Cannot connect to session bus\n"
        )

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    result = deliver("desktop", _OCCURRENCE)
    assert result.success is False
    assert "exited 1" in (result.error or "")
    assert "Cannot connect to session bus" in (result.error or "")


def test_notify_send_sanitizes_title_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_which(monkeypatch, "/usr/bin/notify-send")
    captured: list[list[str]] = []

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    dirty = DueOccurrence(
        reminder_id=3,
        due_at="2026-08-03T12:00:00+00:00",
        kind="follow_up",
        message="line one\nline two\ttabbed",
        job_id=7,
        job_title="Python\nEngineer",
        job_company="Acme GmbH",
        job_source="arbeitnow",
    )
    result = deliver("desktop", dirty)
    assert result.success is True
    title = captured[0][-2]
    body = captured[0][-1]
    assert "\n" not in title
    assert "\t" not in title
    assert "line one line two tabbed" in body


def test_clean_removes_control_characters() -> None:
    value = "a\x00b\x1bc d"
    assert backend._clean(value) == "a b c d"
