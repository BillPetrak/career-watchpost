"""Compatibility wrapper for ``careeros systemd uninstall``."""

from __future__ import annotations

import sys

from careeros.cli import main as careeros_main


def main(argv: list[str] | None = None) -> int:
    return careeros_main(["systemd", "uninstall", *(argv or sys.argv[1:])])


if __name__ == "__main__":
    sys.exit(main())
