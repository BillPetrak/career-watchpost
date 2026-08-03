"""Install the CareerOS systemd user units from the repository templates.

Writes the service and timer into the systemd user unit directory (default
``$HOME/.config/systemd/user``), then reloads the user manager. The CareerOS
database is never touched. ``--dry-run`` previews every change without making
any; ``--no-systemctl`` writes the units but only prints the systemctl commands.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "careeros-collect.service"
TIMER_NAME = "careeros-collect.timer"
UNIT_NAMES = (SERVICE_NAME, TIMER_NAME)
DEFAULT_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

_SAFE_EXEC_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-"
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def escape_specifiers(value: str) -> str:
    return value.replace("%", "%%")


def quote_exec_arg(value: str) -> str:
    if value and all(character in _SAFE_EXEC_CHARS for character in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_unit(name: str, uv_bin: Path, project_dir: Path) -> str:
    template = repo_root() / "systemd" / name
    uv_token = quote_exec_arg(escape_specifiers(str(uv_bin)))
    project_token = quote_exec_arg(escape_specifiers(str(project_dir)))
    return (
        template.read_text()
        .replace("@UV_BIN@", uv_token)
        .replace("@PROJECT_DIR@", project_token)
    )


def rendered_units(uv_bin: Path, project_dir: Path) -> dict[str, str]:
    return {name: render_unit(name, uv_bin, project_dir) for name in UNIT_NAMES}


def find_conflicts(unit_dir: Path, units: dict[str, str]) -> list[Path]:
    conflicts = []
    for name, content in units.items():
        path = unit_dir / name
        if path.exists() and path.read_text() != content:
            conflicts.append(path)
    return conflicts


def systemctl(arguments: list[str], *, execute: bool) -> int:
    command = ["systemctl", "--user", *arguments]
    if execute:
        result = subprocess.run(command)
        return result.returncode
    print("would run: " + shlex.join(command))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the CareerOS systemd user units from the "
        "repository templates.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing files or running systemctl.",
    )
    parser.add_argument(
        "--unit-dir",
        type=Path,
        default=DEFAULT_UNIT_DIR,
        help="Target systemd user unit directory.",
    )
    parser.add_argument(
        "--uv",
        type=Path,
        help="Absolute path to the uv binary (default: resolve from PATH).",
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="Absolute path to the CareerOS project root.",
    )
    parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable and start the timer after installing the units.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing unit files that differ from the templates.",
    )
    parser.add_argument(
        "--no-systemctl",
        action="store_true",
        help="Write units but print systemctl commands instead of running them.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.uv is not None:
        uv_bin = args.uv.resolve()
    else:
        found = shutil.which("uv")
        if found is None:
            parser.error("could not locate the uv binary; pass --uv")
        uv_bin = Path(found).resolve()

    project_dir = (args.project or repo_root()).resolve()
    unit_dir = args.unit_dir.resolve()
    units = rendered_units(uv_bin, project_dir)
    conflicts = find_conflicts(unit_dir, units)

    if conflicts and not args.force:
        print(
            "Aborting: existing unit files differ from the generated templates:",
            file=sys.stderr,
        )
        for path in conflicts:
            print(f"  {path}", file=sys.stderr)
        print(
            "Pass --force to overwrite them, or remove the files first.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        for name in UNIT_NAMES:
            target = unit_dir / name
            tag = "overwrite" if target.exists() else "write"
            print(f"would {tag}: {target}")
        print()
        print(f"Dry run: no changes made. Target directory: {unit_dir}")
    else:
        unit_dir.mkdir(parents=True, exist_ok=True)
        for name, content in units.items():
            path = unit_dir / name
            path.write_text(content)
            os.chmod(path, 0o644)
            print(f"Wrote: {path}")
        print()

    execute = not (args.dry_run or args.no_systemctl)
    daemon_returncode = systemctl(["daemon-reload"], execute=execute)
    if execute and daemon_returncode != 0:
        print("systemctl --user daemon-reload failed.", file=sys.stderr)
        return daemon_returncode

    if args.enable:
        enable_returncode = systemctl(["enable", "--now", TIMER_NAME], execute=execute)
        if execute and enable_returncode != 0:
            print("systemctl --user enable failed.", file=sys.stderr)
            return enable_returncode

    if args.enable:
        print("Timer enabled: careeros-collect.timer")
    print(
        "Optional: run 'loginctl enable-linger $USER' so the timer also runs "
        "without a login session."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
