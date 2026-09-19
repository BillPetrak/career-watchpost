"""Run the CareerOS CLI through the active Python environment."""

from __future__ import annotations

import sys

from careeros.cli import main

if __name__ == "__main__":
    sys.exit(main())
