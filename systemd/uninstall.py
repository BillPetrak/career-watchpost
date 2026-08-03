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
    render_unit,
    repo_root,
    unit_names_for,
)


def _render_units(uv_bin: Path, project_dir: Path, unit_names: tuple[str, ...]) -> dict[str, str]:
    return {name: render_unit(name, uv_bin, project_dir) for name in unit_names}


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
        "--units",
        choices=("collect", "notify", "all"),
        default="collect",
        help="Which unit set to uninstall (default: collect; 'notify' removes "
        "the reminder-notification units independently, 'all' removes both).",
    )
    parser.add_argument(
        "--no-systemctl",
        action="store_true",
        help="Remove files but print systemctl commands instead of running them.",
    )
    return parser


def _timer_is_enabled(timer_name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-enabled", timer_name],
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
    unit_names = unit_names_for(args.units)
    timer_names = [name for name in unit_names if name.endswith(".timer")]
    units = _render_units(uv_bin, project_dir, unit_names)

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
        for timer in timer_names:
            print(f"Would run: systemctl --user disable --now {timer}")
        print("Would run: systemctl --user daemon-reload")
        print("Dry run: no changes made.")
        return 0

    if not args.no_systemctl and removable:
        for timer in timer_names:
            if _timer_is_enabled(timer):
                command = ["systemctl", "--user", "disable", "--now", timer]
                print("Running: " + shlex.join(command))
                disable_returncode = subprocess.run(command).returncode
                if disable_returncode != 0:
                    print(f"systemctl --user disable failed for {timer}.", file=sys.stderr)

    for path in removable:
        path.unlink()
        if args.no_systemctl:
            print(f"Removed: {path}")

    if args.no_systemctl:
        for timer in timer_names:
            print(f"Would run: systemctl --user disable --now {timer}")
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
