"""Offline tests for packaged systemd user-unit administration."""

from __future__ import annotations

import os
from pathlib import Path

import careeros.systemd as systemd_admin
from careeros.cli import main
from careeros.systemd import (
    ALL_UNIT_NAMES,
    NOTIFY_UNIT_NAMES,
    UNIT_NAMES,
    render_unit,
    template_text,
)


def _install(unit_dir: Path, python: Path, *, units: str = "collect") -> int:
    return main(
        [
            "systemd",
            "install",
            "--unit-dir",
            str(unit_dir),
            "--python",
            str(python),
            "--units",
            units,
            "--no-systemctl",
        ]
    )


def test_packaged_templates_have_required_contract() -> None:
    collect = template_text(UNIT_NAMES[0])
    notify = template_text(NOTIFY_UNIT_NAMES[0])
    assert "Type=oneshot" in collect
    assert "SuccessExitStatus=75" in collect
    assert "ExecStart=@PYTHON_BIN@ -m careeros collect due" in collect
    assert "ExecStart=@PYTHON_BIN@ -m careeros reminder notify-due" in notify
    assert "@PROJECT_DIR@" not in collect
    assert "uv run" not in collect
    for name in ALL_UNIT_NAMES:
        assert template_text(name)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    assert (
        main(
            [
                "systemd",
                "install",
                "--dry-run",
                "--unit-dir",
                str(unit_dir),
                "--units",
                "all",
            ]
        )
        == 0
    )
    assert not unit_dir.exists()


def test_install_uses_selected_python_and_independent_unit_sets(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    python = tmp_path / "venv with space" / "bin" / "python%3"
    assert _install(unit_dir, python, units="notify") == 0
    assert {path.name for path in unit_dir.iterdir()} == set(NOTIFY_UNIT_NAMES)
    service = unit_dir / NOTIFY_UNIT_NAMES[0]
    escaped = str(python.resolve()).replace("%", "%%")
    assert f'ExecStart="{escaped}" -m careeros' in service.read_text()
    assert os.stat(service).st_mode & 0o777 == 0o644


def test_install_preserves_active_virtualenv_interpreter_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    unit_dir = tmp_path / "units"
    base_python = tmp_path / "base/bin/python3"
    base_python.parent.mkdir(parents=True)
    base_python.touch()
    venv_python = tmp_path / "venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(base_python)
    monkeypatch.setattr(systemd_admin.sys, "executable", str(venv_python))

    assert (
        main(
            [
                "systemd",
                "install",
                "--unit-dir",
                str(unit_dir),
                "--no-systemctl",
            ]
        )
        == 0
    )

    service = (unit_dir / UNIT_NAMES[0]).read_text()
    assert f"ExecStart={venv_python.absolute()} -m careeros" in service
    assert str(base_python.resolve()) not in service


def test_reinstall_is_idempotent_and_conflicts_fail_closed(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    python = tmp_path / "bin/python"
    assert _install(unit_dir, python) == 0
    service = unit_dir / UNIT_NAMES[0]
    expected = service.read_bytes()
    assert _install(unit_dir, python) == 0
    assert service.read_bytes() == expected
    service.write_text("foreign")
    assert _install(unit_dir, python) == 1
    assert service.read_text() == "foreign"


def test_force_replaces_symlink_without_modifying_its_target(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    python = tmp_path / "bin/python"
    target = tmp_path / "valuable"
    target.write_text("must remain unchanged")
    service = unit_dir / UNIT_NAMES[0]
    service.symlink_to(target)

    assert (
        main(
            [
                "systemd",
                "install",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
                "--force",
                "--no-systemctl",
            ]
        )
        == 0
    )
    assert target.read_text() == "must remain unchanged"
    assert service.is_file()
    assert not service.is_symlink()


def test_force_replaces_and_uninstall_only_removes_exact_units(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    python = tmp_path / "bin/python"
    service = unit_dir / UNIT_NAMES[0]
    service.write_text("foreign")
    assert (
        main(
            [
                "systemd",
                "install",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
                "--force",
                "--no-systemctl",
            ]
        )
        == 0
    )
    assert service.read_text() == render_unit(UNIT_NAMES[0], python.resolve())
    (unit_dir / UNIT_NAMES[1]).write_text("locally modified")
    assert (
        main(
            [
                "systemd",
                "uninstall",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
                "--no-systemctl",
            ]
        )
        == 0
    )
    assert not service.exists()
    assert (unit_dir / UNIT_NAMES[1]).read_text() == "locally modified"


def test_uninstall_never_disables_modified_timer(tmp_path: Path, monkeypatch) -> None:
    unit_dir = tmp_path / "units"
    python = tmp_path / "bin/python"
    assert _install(unit_dir, python) == 0
    service = unit_dir / UNIT_NAMES[0]
    timer = unit_dir / UNIT_NAMES[1]
    timer.write_text("locally modified")
    enabled_checks: list[str] = []
    systemctl_calls: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(
        systemd_admin,
        "_timer_is_enabled",
        lambda name: enabled_checks.append(name) or True,
    )
    monkeypatch.setattr(
        systemd_admin,
        "_systemctl",
        lambda arguments, *, execute: systemctl_calls.append((arguments, execute)) or 0,
    )

    assert (
        main(
            [
                "systemd",
                "uninstall",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
            ]
        )
        == 0
    )

    assert not service.exists()
    assert timer.read_text() == "locally modified"
    assert enabled_checks == []
    assert all(call[0][:2] != ["disable", "--now"] for call in systemctl_calls)


def test_uninstall_disables_exact_owned_timer(tmp_path: Path, monkeypatch) -> None:
    unit_dir = tmp_path / "units"
    python = tmp_path / "bin/python"
    assert _install(unit_dir, python) == 0
    systemctl_calls: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(systemd_admin, "_timer_is_enabled", lambda _name: True)
    monkeypatch.setattr(
        systemd_admin,
        "_systemctl",
        lambda arguments, *, execute: systemctl_calls.append((arguments, execute)) or 0,
    )

    assert (
        main(
            [
                "systemd",
                "uninstall",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
            ]
        )
        == 0
    )

    assert (["disable", "--now", UNIT_NAMES[1]], True) in systemctl_calls
    assert not any((unit_dir / name).exists() for name in UNIT_NAMES)


def test_uninstall_dry_run_reports_only_exact_owned_timers(
    tmp_path: Path, capsys
) -> None:
    unit_dir = tmp_path / "units"
    python = tmp_path / "bin/python"
    assert _install(unit_dir, python, units="all") == 0
    (unit_dir / UNIT_NAMES[1]).write_text("locally modified")
    capsys.readouterr()

    assert (
        main(
            [
                "systemd",
                "uninstall",
                "--dry-run",
                "--units",
                "all",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert f"disable --now {NOTIFY_UNIT_NAMES[1]}" in captured.out
    assert f"disable --now {UNIT_NAMES[1]}" not in captured.out
    assert all((unit_dir / name).exists() for name in ALL_UNIT_NAMES)


def test_uninstall_no_systemctl_reports_only_exact_owned_timers(
    tmp_path: Path, capsys
) -> None:
    unit_dir = tmp_path / "units"
    python = tmp_path / "bin/python"
    assert _install(unit_dir, python, units="all") == 0
    modified_timer = unit_dir / UNIT_NAMES[1]
    modified_timer.write_text("locally modified")
    capsys.readouterr()

    assert (
        main(
            [
                "systemd",
                "uninstall",
                "--no-systemctl",
                "--units",
                "all",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()

    assert f"disable --now {NOTIFY_UNIT_NAMES[1]}" in captured.out
    assert f"disable --now {UNIT_NAMES[1]}" not in captured.out
    assert modified_timer.read_text() == "locally modified"
    assert {path.name for path in unit_dir.iterdir()} == {UNIT_NAMES[1]}


def test_uninstall_dry_run_keeps_exact_units(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    python = tmp_path / "bin/python"
    assert _install(unit_dir, python) == 0
    assert (
        main(
            [
                "systemd",
                "uninstall",
                "--dry-run",
                "--unit-dir",
                str(unit_dir),
                "--python",
                str(python),
            ]
        )
        == 0
    )
    assert all((unit_dir / name).is_file() for name in UNIT_NAMES)
