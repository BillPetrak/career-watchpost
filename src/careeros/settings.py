"""Runtime settings and stable per-user paths for CareerOS.

Environment variables are read with the standard library only — there is no
dotenv machinery. Persistent defaults follow the XDG base-directory contract
and therefore never depend on a source checkout, virtual environment, or
installed package location.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATABASE = "CAREEROS_DATABASE"
ENV_MATERIALS_ROOT = "CAREEROS_MATERIALS_ROOT"
ENV_SOURCES_CONFIG = "CAREEROS_SOURCES_CONFIG"


def _xdg_home(variable: str, fallback: str) -> Path:
    """Return one absolute, expanded XDG base directory without creating it."""
    value = os.environ.get(variable) or fallback
    return Path(value).expanduser().absolute()


def default_database_path() -> Path:
    """Return the stable per-user database default without creating it."""
    return _xdg_home("XDG_DATA_HOME", "~/.local/share") / "careeros/careeros.sqlite3"


def default_materials_root() -> Path:
    """Return the stable per-user managed-materials default without creating it."""
    return _xdg_home("XDG_DATA_HOME", "~/.local/share") / "careeros/materials"


def default_profile_path() -> Path:
    """Return the stable per-user private-profile default without creating it."""
    return _xdg_home("XDG_CONFIG_HOME", "~/.config") / "careeros/candidate_profile.yaml"


def default_sources_config_path() -> Path:
    """Return the private source configuration path without creating it."""
    return _xdg_home("XDG_CONFIG_HOME", "~/.config") / "careeros/sources.yaml"


# Compatibility constants reflect the process environment at import time.
# Runtime entry points use the functions above so tests and embedders can safely
# provide an environment before constructing an application or CLI parser.
DEFAULT_DATABASE_PATH = default_database_path()
DEFAULT_MATERIALS_ROOT = default_materials_root()
DEFAULT_PROFILE_PATH = default_profile_path()


def database_path() -> Path:
    """Return the configured database path for read endpoints.

    Honors ``CAREEROS_DATABASE`` when set (an empty value falls back to the
    default); otherwise returns the per-user XDG data path. ``~`` is
    expanded and relative paths are resolved against the current working
    directory, so the result always names an absolute path.
    """
    value = os.environ.get(ENV_DATABASE)
    if not value:
        return default_database_path()
    return Path(value).expanduser().resolve()


def materials_root() -> Path:
    value = os.environ.get(ENV_MATERIALS_ROOT)
    if value:
        return Path(value).expanduser().absolute()
    return default_materials_root()


__all__: tuple[str, ...] = (
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_MATERIALS_ROOT",
    "DEFAULT_PROFILE_PATH",
    "ENV_DATABASE",
    "ENV_MATERIALS_ROOT",
    "ENV_SOURCES_CONFIG",
    "database_path",
    "default_database_path",
    "default_materials_root",
    "default_profile_path",
    "default_sources_config_path",
    "materials_root",
)
