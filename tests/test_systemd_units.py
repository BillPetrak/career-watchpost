"""Deterministic offline tests for the systemd templates, installer, and uninstaller.

Every test uses ``tmp_path`` for the unit directory and runs the scripts via a
subprocess with ``--no-systemctl`` or ``--dry-run`` so the live user manager is
never touched. No test requires network or a running systemd user instance.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = PROJECT_ROOT / "systemd"
SERVICE_NAME = "careeros-collect.service"
TIMER_NAME = "careeros-collect.timer"


def _run_script(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SYSTEMD_DIR / script), *arguments],
        capture_output=True,
        text=True,
    )


def _overrides(tmp_path: Path) -> list[str]:
    return [
        "--uv",
        str(tmp_path / "bin" / "uv"),
        "--project",
        str(tmp_path / "proj"),
    ]


def _install(tmp_path: Path, unit_dir: Path) -> None:
    result = _run_script(
        "install.py",
        "--unit-dir",
        str(unit_dir),
        "--no-systemctl",
        *_overrides(tmp_path),
    )
    assert result.returncode == 0, result.stderr


def test_service_template_contains_expected_directives() -> None:
    template = (SYSTEMD_DIR / SERVICE_NAME).read_text()
    assert "Type=oneshot" in template
    assert "SuccessExitStatus=75" in template
    assert "Environment=PYTHONUNBUFFERED=1" in template
    assert (
        "ExecStart=@UV_BIN@ run --project @PROJECT_DIR@ careeros collect due"
        in template
    )
    assert "bash -c" not in template
    assert "WorkingDirectory=" not in template


def test_timer_template_contains_expected_directives() -> None:
    template = (SYSTEMD_DIR / TIMER_NAME).read_text()
    assert "OnStartupSec=5min" in template
    assert "OnUnitActiveSec=15min" in template
    assert "RandomizedDelaySec=3min" in template
    assert "[Install]" in template
    assert "WantedBy=timers.target" in template
    assert "Persistent=" not in template
    assert "OnBootSec=" not in template
    assert "OnCalendar=" not in template


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    result = _run_script("install.py", "--dry-run", "--unit-dir", str(unit_dir))
    assert result.returncode == 0
    assert not unit_dir.exists()
    assert SERVICE_NAME in result.stdout
    assert TIMER_NAME in result.stdout


def test_install_writes_rendered_units(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    uv = tmp_path / "bin" / "uv"
    project = tmp_path / "proj"
    result = _run_script(
        "install.py",
        "--unit-dir",
        str(unit_dir),
        "--no-systemctl",
        "--uv",
        str(uv),
        "--project",
        str(project),
    )
    assert result.returncode == 0, result.stderr
    service = unit_dir / SERVICE_NAME
    timer = unit_dir / TIMER_NAME
    assert service.is_file()
    assert timer.is_file()
    assert (
        f"ExecStart={uv} run --project {project} careeros collect due"
        in service.read_text()
    )
    assert "SuccessExitStatus=75" in service.read_text()
    assert "OnStartupSec=5min" in timer.read_text()
    assert os.stat(service).st_mode & 0o777 == 0o644


def test_exec_args_quote_spaces_and_escape_percent(tmp_path: Path) -> None:
    uv = tmp_path / "uv tools" / "uv"
    project = tmp_path / "Proj 100%"
    unit_dir = tmp_path / "units"
    result = _run_script(
        "install.py",
        "--unit-dir",
        str(unit_dir),
        "--no-systemctl",
        "--uv",
        str(uv),
        "--project",
        str(project),
    )
    assert result.returncode == 0, result.stderr
    rendered = (unit_dir / SERVICE_NAME).read_text()
    escaped_project = str(project).replace("%", "%%")
    assert f'ExecStart="{uv}"' in rendered
    assert f'--project "{escaped_project}"' in rendered
    assert "100%%" in rendered


def test_reinstall_is_idempotent(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    _install(tmp_path, unit_dir)
    first = (unit_dir / SERVICE_NAME).read_text()
    _install(tmp_path, unit_dir)
    second = (unit_dir / SERVICE_NAME).read_text()
    assert first == second


def test_existing_unit_that_differs_aborts_without_force(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    service = unit_dir / SERVICE_NAME
    service.write_text("tampered")
    result = _run_script("install.py", "--unit-dir", str(unit_dir), "--no-systemctl")
    assert result.returncode != 0
    assert "--force" in result.stdout + result.stderr
    assert service.read_text() == "tampered"


def test_force_overwrites_differing_unit(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    service = unit_dir / SERVICE_NAME
    service.write_text("tampered")
    result = _run_script(
        "install.py",
        "--unit-dir",
        str(unit_dir),
        "--no-systemctl",
        "--force",
    )
    assert result.returncode == 0, result.stderr
    assert service.read_text() != "tampered"
    assert "SuccessExitStatus=75" in service.read_text()


def test_uninstall_dry_run_keeps_units(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    _install(tmp_path, unit_dir)
    result = _run_script(
        "uninstall.py",
        "--dry-run",
        "--unit-dir",
        str(unit_dir),
        *_overrides(tmp_path),
    )
    assert result.returncode == 0
    assert (unit_dir / SERVICE_NAME).is_file()
    assert (unit_dir / TIMER_NAME).is_file()


def test_uninstall_removes_only_owned_units(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    _install(tmp_path, unit_dir)
    foreign = unit_dir / "custom.service"
    foreign.write_text("custom")
    result = _run_script(
        "uninstall.py",
        "--unit-dir",
        str(unit_dir),
        "--no-systemctl",
        *_overrides(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert not (unit_dir / SERVICE_NAME).exists()
    assert not (unit_dir / TIMER_NAME).exists()
    assert foreign.read_text() == "custom"


def test_uninstall_skips_modified_units(tmp_path: Path) -> None:
    unit_dir = tmp_path / "units"
    _install(tmp_path, unit_dir)
    service = unit_dir / SERVICE_NAME
    service.write_text("tampered")
    result = _run_script(
        "uninstall.py",
        "--unit-dir",
        str(unit_dir),
        "--no-systemctl",
        *_overrides(tmp_path),
    )
    assert result.returncode == 0
    assert service.read_text() == "tampered"
    assert not (unit_dir / TIMER_NAME).exists()
