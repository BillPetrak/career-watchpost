"""Runtime settings for the CareerOS application layers.

Environment variables are read with the standard library only — there is no
dotenv machinery. ``CAREEROS_DATABASE`` overrides the default gitignored
database path so the API (and future library entry points) can target an
alternative SQLite database without touching CLI defaults.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "database" / "careeros.sqlite3"

ENV_DATABASE = "CAREEROS_DATABASE"
ENV_MATERIALS_ROOT = "CAREEROS_MATERIALS_ROOT"


def database_path() -> Path:
    """Return the configured database path for read endpoints.

    Honors ``CAREEROS_DATABASE`` when set (an empty value falls back to the
    default); otherwise returns the CLI's default gitignored path. ``~`` is
    expanded and relative paths are resolved against the current working
    directory, so the result always names an absolute path.
    """
    value = os.environ.get(ENV_DATABASE)
    if not value:
        return DEFAULT_DATABASE_PATH
    return Path(value).expanduser().resolve()


def materials_root() -> Path:
    value = os.environ.get(ENV_MATERIALS_ROOT)
    if value:
        return Path(value).expanduser().absolute()
    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return (data_home / "careeros" / "materials").absolute()


__all__: tuple[str, ...] = (
    "DEFAULT_DATABASE_PATH",
    "ENV_DATABASE",
    "ENV_MATERIALS_ROOT",
    "PROJECT_ROOT",
    "database_path",
    "materials_root",
)
