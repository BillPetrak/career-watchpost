"""Tests for ``careeros diagnose`` notification-readiness reporting.

The command is strictly read-only; every check is faked here (PATH,
``systemctl``, the unit directory, and the resolved backend) so no test writes
to the database, the filesystem, or the live user systemd manager.
"""

from __future__ import annotations

import pytest

from careeros import cli
from careeros.cli.commands import diagnose


def _fake_systemctl(arguments: list[str]) -> str | None:
    if not arguments:
        return None
    if "is-enabled" in arguments:
        return "enabled"
    if "is-active" in arguments:
        return "active"
    return None


def _run_diagnose(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    unit_dir,
    resolve: tuple[str, str],
    notify_send: str | None = "/usr/bin/notify-send",
    systemctl: bool = True,
    enabled: str | None = "enabled",
    active: str | None = "active",
) -> str:
    monkeypatch.setattr(diagnose, "NOTIFY_UNIT_DIR", unit_dir)

    def fake_which(name: str) -> str | None:
        if name == "notify-send":
            return notify_send
        if name == "systemctl":
            return "/usr/bin/systemctl" if systemctl else None
        return None

    monkeypatch.setattr(diagnose.shutil, "which", fake_which)
    monkeypatch.setattr(diagnose, "resolve_backend", lambda _preference: resolve)

    def fake_systemctl(arguments: list[str]) -> str | None:
        if not systemctl or not arguments:
            return None
        if "is-enabled" in arguments:
            return enabled
        if "is-active" in arguments:
            return active
        return None

    monkeypatch.setattr(diagnose, "_systemctl", fake_systemctl)
    assert cli.main(["diagnose"]) == 0
    return capsys.readouterr().out


def test_reports_operational_notification_state(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / diagnose.NOTIFY_TIMER_NAME).touch()
    out = _run_diagnose(
        capsys,
        monkeypatch,
        unit_dir=unit_dir,
        resolve=("desktop", ""),
    )
    assert "notify-send:              /usr/bin/notify-send" in out
    assert "auto backend:             desktop" in out
    assert "installed: True" in out
    assert "enabled:   enabled" in out
    assert "active:    active" in out
    assert "Notification delivery is operational" in out


def test_reports_missing_notify_send_and_guidance(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    out = _run_diagnose(
        capsys,
        monkeypatch,
        unit_dir=tmp_path,
        notify_send=None,
        resolve=("stdout", "fallback; notify-send not found on PATH"),
    )
    assert "notify-send:              not found on PATH" in out
    assert (
        "auto backend:             stdout (fallback; notify-send not found on PATH)"
        in out
    )
    assert "libnotify-bin" in out


def test_reports_uninstalled_timer_and_install_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    out = _run_diagnose(
        capsys,
        monkeypatch,
        unit_dir=tmp_path,
        resolve=("desktop", ""),
    )
    assert "installed: False" in out
    assert "careeros systemd install --units notify --enable" in out


def test_reports_installed_but_disabled_timer(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / diagnose.NOTIFY_TIMER_NAME).touch()
    out = _run_diagnose(
        capsys,
        monkeypatch,
        unit_dir=unit_dir,
        resolve=("desktop", ""),
        enabled=None,
    )
    assert "installed: True" in out
    assert "enabled:   unknown" in out
    assert f"systemctl --user enable --now {diagnose.NOTIFY_TIMER_NAME}" in out


def test_reports_fallback_backend_and_reason(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / diagnose.NOTIFY_TIMER_NAME).touch()
    out = _run_diagnose(
        capsys,
        monkeypatch,
        unit_dir=unit_dir,
        resolve=("stdout", "fallback; no usable session bus for desktop notifications"),
    )
    assert "auto backend:             stdout" in out
    assert "fallback; no usable session bus" in out
    assert "systemctl --user import-environment" in out
    assert "Notification delivery is operational" not in out


def test_survives_missing_systemctl(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / diagnose.NOTIFY_TIMER_NAME).touch()
    out = _run_diagnose(
        capsys,
        monkeypatch,
        unit_dir=unit_dir,
        resolve=("desktop", ""),
        systemctl=False,
    )
    assert "enabled:   unknown" in out
    assert "active:    unknown" in out
    assert "Environment check passed." in out


def test_timer_state_composes_installation_and_systemctl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    (unit_dir / diagnose.NOTIFY_TIMER_NAME).touch()
    monkeypatch.setattr(diagnose, "NOTIFY_UNIT_DIR", unit_dir)
    monkeypatch.setattr(diagnose, "_systemctl", _fake_systemctl)
    installed, enabled, active = diagnose._timer_state()
    assert (installed, enabled, active) == (True, "enabled", "active")


def test_timer_state_uses_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    config_home = tmp_path / "config"
    unit_dir = config_home / "systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / diagnose.NOTIFY_TIMER_NAME).touch()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(diagnose, "NOTIFY_UNIT_DIR", None)
    monkeypatch.setattr(diagnose, "_systemctl", _fake_systemctl)

    installed, enabled, active = diagnose._timer_state()

    assert (installed, enabled, active) == (True, "enabled", "active")

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(diagnose, "NOTIFY_UNIT_DIR", empty)
    installed, _enabled, _active = diagnose._timer_state()
    assert installed is False
