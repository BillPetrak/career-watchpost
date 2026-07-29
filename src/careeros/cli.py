"""CareerOS command-line interface."""

from __future__ import annotations

import argparse
import platform
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def print_diagnostics() -> int:
    """Print basic runtime and project diagnostics."""
    database_directory = PROJECT_ROOT / "data" / "database"

    print("CareerOS diagnostics")
    print("--------------------")
    print(f"Project root:      {PROJECT_ROOT}")
    print(f"Python version:    {platform.python_version()}")
    print(f"Python executable: {sys.executable}")
    print(f"SQLite library:    {sqlite3.sqlite_version}")
    print(f"Database directory exists: {database_directory.is_dir()}")

    if sys.version_info < (3, 11):
        print()
        print("ERROR: CareerOS requires Python 3.11 or newer.")
        return 1

    print()
    print("Environment check passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="careeros",
        description="CareerOS career intelligence platform",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "diagnose",
        help="Check the CareerOS runtime environment",
    )

    return parser


def main() -> int:
    """Run the CareerOS command-line interface."""
    arguments = build_parser().parse_args()

    if arguments.command == "diagnose":
        return print_diagnostics()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
