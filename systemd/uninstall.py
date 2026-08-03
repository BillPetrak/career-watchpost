"""Uninstall the CareerOS systemd user units installed by install.py.

Only removes unit files whose content matches what install.py currently
renders; foreign or locally edited files are left in place with a warning.
The CareerOS database is never touched. ``--dry-run`` previews the removal.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from install import (
    DEFAULT_UNIT_DIR,
    TIMER_NAME,
    UNIT_NAMES,
    render_unit,
    repo_root,
)


def _render_units(uv_bin: Path, project_dir: Path) -> dict[str, str]:
    return {name: render_unit(name, uv_bin, project_dir) for name in UNIT_NAMES}


def _find_uv(argument: Path | None, parser: argparse.ArgumentParser) -> Path:
    if argument is not None:
        return argument.resolve()
    found = shutil.which("uv")
    if found is None:
        parser.error("could not locate the uv binary; pass --uv")
    return Path(found).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Uninstall the CareerOS systemd user units.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the removal without removing files or running systemctl.",
    )
    parser.add_argument(
        "--unit-dir",
        type=Path,
        default=DEFAULT_UNIT_DIR,
        help="Systemd user unit directory to clean.",
    )
    parser.add_argument(
        "--uv",
        type=Path,
        help="Absolute path to the uv binary used to render expected content.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        help="Absolute path to the CareerOS project root.",
    )
    parser.add_argument(
        "--no-systemctl",
        action="store_true",
        help="Remove files but print systemctl commands instead of running them.",
    )
    return parser


def _timer_is_enabled() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", TIMER_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() not in {"disabled", "static", "indirect"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    uv_bin = _find_uv(args.uv, parser)
    project_dir = (args.project or repo_root()).resolve()
    unit_dir = args.unit_dir.resolve()
    units = _render_units(uv_bin, project_dir)

    removable: list[Path] = []
    for name, content in units.items():
        path = unit_dir / name
        if not path.exists():
            print(f"Not installed: {path}")
            continue
        if path.read_text() == content:
            removable.append(path)
            print(f"Would remove: {path}" if args.dry_run else f"Removing: {path}")
        else:
            print(
                f"Skipping (differs from the installed template): {path}",
                file=sys.stderr,
            )

    if args.dry_run:
        print()
        print("Would run: systemctl --user disable --now careeros-collect.timer")
        print("Would run: systemctl --user daemon-reload")
        print("Dry run: no changes made.")
        return 0

    if not args.no_systemctl and removable and _timer_is_enabled():
        command = ["systemctl", "--user", "disable", "--now", TIMER_NAME]
        print("Running: " + shlex.join(command))
        disable_returncode = subprocess.run(command).returncode
        if disable_returncode != 0:
            print("systemctl --user disable failed.", file=sys.stderr)

    for path in removable:
        path.unlink()
        if args.no_systemctl:
            print(f"Removed: {path}")

    if args.no_systemctl:
        print("Would run: systemctl --user disable --now careeros-collect.timer")
        print("Would run: systemctl --user daemon-reload")
        print("Run without --no-systemctl to apply the systemctl steps.")
    else:
        command = ["systemctl", "--user", "daemon-reload"]
        print("Running: " + shlex.join(command))
        reload_returncode = subprocess.run(command).returncode
        if reload_returncode != 0:
            print("systemctl --user daemon-reload failed.", file=sys.stderr)
            return reload_returncode

    return 0


if __name__ == "__main__":
    sys.exit(main())
