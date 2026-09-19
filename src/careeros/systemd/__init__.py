"""Installed-artifact systemd user-unit administration."""

from __future__ import annotations

import argparse
import os
import shlex
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path

SERVICE_NAME = "careeros-collect.service"
TIMER_NAME = "careeros-collect.timer"
NOTIFY_SERVICE_NAME = "careeros-reminder-notify.service"
NOTIFY_TIMER_NAME = "careeros-reminder-notify.timer"
UNIT_NAMES = (SERVICE_NAME, TIMER_NAME)
NOTIFY_UNIT_NAMES = (NOTIFY_SERVICE_NAME, NOTIFY_TIMER_NAME)
ALL_UNIT_NAMES = (*UNIT_NAMES, *NOTIFY_UNIT_NAMES)
UNIT_SETS: dict[str, tuple[str, ...]] = {
    "collect": UNIT_NAMES,
    "notify": NOTIFY_UNIT_NAMES,
    "all": ALL_UNIT_NAMES,
}
_SAFE_EXEC_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-"
)


def default_unit_dir() -> Path:
    """Return the systemd user-unit directory without creating it."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config")
    return config_home.expanduser().absolute() / "systemd/user"


def unit_names_for(choice: str) -> tuple[str, ...]:
    return UNIT_SETS[choice]


def _quote_exec_arg(value: str) -> str:
    value = value.replace("%", "%%")
    if value and all(character in _SAFE_EXEC_CHARS for character in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def template_text(name: str) -> str:
    """Read a packaged unit template."""
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def render_unit(name: str, python_bin: Path) -> str:
    return template_text(name).replace("@PYTHON_BIN@", _quote_exec_arg(str(python_bin)))


def _rendered_units(python_bin: Path, unit_names: tuple[str, ...]) -> dict[str, str]:
    return {name: render_unit(name, python_bin) for name in unit_names}


def _path_exists(path: Path) -> bool:
    """Return true for any directory entry, including a broken symlink."""
    return path.exists() or path.is_symlink()


def _unit_content(path: Path) -> str | None:
    """Read a plain, single-link unit file without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _write_new_unit(path: Path, content: str) -> None:
    """Create a unit without following or truncating a raced path."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    try:
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fchmod(fd, 0o644)
    finally:
        os.close(fd)


def _systemctl(arguments: list[str], *, execute: bool) -> int:
    command = ["systemctl", "--user", *arguments]
    if execute:
        return subprocess.run(command).returncode
    print("would run: " + shlex.join(command))
    return 0


def _run_install(arguments: argparse.Namespace) -> int:
    # Preserve a virtual environment's interpreter path. Resolving its symlink
    # would embed the base interpreter, which cannot see the wheel installed in
    # the environment that owns this command.
    python_bin = (arguments.python or Path(sys.executable)).expanduser().absolute()
    unit_dir = (arguments.unit_dir or default_unit_dir()).resolve()
    unit_names = unit_names_for(arguments.units)
    units = _rendered_units(python_bin, unit_names)
    conflicts = [
        unit_dir / name
        for name, content in units.items()
        if _path_exists(unit_dir / name) and _unit_content(unit_dir / name) != content
    ]
    if conflicts and not arguments.force:
        print(
            "Aborting: existing unit files differ from the generated templates:",
            file=sys.stderr,
        )
        for path in conflicts:
            print(f"  {path}", file=sys.stderr)
        print("Pass --force to replace CareerOS units deliberately.", file=sys.stderr)
        return 1

    if arguments.dry_run:
        for name in unit_names:
            target = unit_dir / name
            action = "overwrite" if _path_exists(target) else "write"
            print(f"would {action}: {target}")
        print(f"Dry run: no changes made. Target directory: {unit_dir}")
    else:
        unit_dir.mkdir(parents=True, exist_ok=True)
        try:
            for name, content in units.items():
                path = unit_dir / name
                current = _unit_content(path)
                if current == content:
                    os.chmod(path, 0o644)
                    print(f"Current: {path}")
                    continue
                if _path_exists(path):
                    info = path.lstat()
                    if not (path.is_symlink() or stat.S_ISREG(info.st_mode)):
                        raise OSError(f"refusing to replace non-file unit path: {path}")
                    path.unlink()
                _write_new_unit(path, content)
                print(f"Wrote: {path}")
        except OSError as error:
            print(f"Failed to install user unit: {error}", file=sys.stderr)
            return 1

    execute = not (arguments.dry_run or arguments.no_systemctl)
    returncode = _systemctl(["daemon-reload"], execute=execute)
    if execute and returncode != 0:
        print("systemctl --user daemon-reload failed.", file=sys.stderr)
        return returncode
    if arguments.enable:
        for timer in (name for name in unit_names if name.endswith(".timer")):
            returncode = _systemctl(["enable", "--now", timer], execute=execute)
            if execute and returncode != 0:
                print(f"systemctl --user enable failed for {timer}.", file=sys.stderr)
                return returncode
            print(f"Timer enabled: {timer}")
    print(
        "Optional: run 'loginctl enable-linger $USER' so timers also run "
        "without a login session."
    )
    return 0


def _timer_is_enabled(timer_name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", timer_name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() not in {
        "disabled",
        "static",
        "indirect",
    }


def _run_uninstall(arguments: argparse.Namespace) -> int:
    python_bin = (arguments.python or Path(sys.executable)).expanduser().absolute()
    unit_dir = (arguments.unit_dir or default_unit_dir()).resolve()
    unit_names = unit_names_for(arguments.units)
    units = _rendered_units(python_bin, unit_names)
    removable: list[Path] = []
    for name, content in units.items():
        path = unit_dir / name
        if not _path_exists(path):
            print(f"Not installed: {path}")
        elif _unit_content(path) == content:
            removable.append(path)
            print(f"Would remove: {path}" if arguments.dry_run else f"Removing: {path}")
        else:
            print(f"Skipping modified/foreign unit: {path}", file=sys.stderr)
    removable_timers = [path.name for path in removable if path.name.endswith(".timer")]
    if arguments.dry_run:
        for timer in removable_timers:
            print(f"Would run: systemctl --user disable --now {timer}")
        print("Would run: systemctl --user daemon-reload")
        print("Dry run: no changes made.")
        return 0
    for timer in removable_timers:
        if arguments.no_systemctl:
            _systemctl(["disable", "--now", timer], execute=False)
        elif _timer_is_enabled(timer):
            returncode = _systemctl(["disable", "--now", timer], execute=True)
            if returncode != 0:
                print(f"systemctl --user disable failed for {timer}.", file=sys.stderr)
    for path in removable:
        path.unlink()
    returncode = _systemctl(["daemon-reload"], execute=not arguments.no_systemctl)
    if not arguments.no_systemctl and returncode != 0:
        print("systemctl --user daemon-reload failed.", file=sys.stderr)
        return returncode
    return 0


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the bounded installed-artifact systemd command family."""
    parser = subparsers.add_parser(
        "systemd", help="Manage packaged CareerOS systemd user units."
    )
    commands = parser.add_subparsers(dest="systemd_command", required=True)
    install = commands.add_parser("install", help="Install CareerOS user units.")
    uninstall = commands.add_parser("uninstall", help="Remove owned CareerOS units.")
    for command in (install, uninstall):
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--unit-dir", type=Path)
        command.add_argument("--python", type=Path)
        command.add_argument(
            "--units", choices=("collect", "notify", "all"), default="collect"
        )
        command.add_argument("--no-systemctl", action="store_true")
    install.add_argument("--enable", action="store_true")
    install.add_argument("--force", action="store_true")


def run_command(arguments: argparse.Namespace) -> int:
    if arguments.systemd_command == "install":
        return _run_install(arguments)
    return _run_uninstall(arguments)
