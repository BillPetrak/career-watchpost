"""Release metadata must expose one coherent Product 1 v1.2.1 version."""

from __future__ import annotations

import tomllib
from pathlib import Path

from careeros.api.main import create_app

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "1.2.1"


def test_package_lock_and_api_versions_are_coherent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_careeros = [
        package for package in lock["package"] if package["name"] == "careeros"
    ]

    assert project["project"]["version"] == EXPECTED_VERSION
    assert len(locked_careeros) == 1
    assert locked_careeros[0]["version"] == project["project"]["version"]
    assert (
        create_app(ROOT / "unused-release-version.sqlite3").version == EXPECTED_VERSION
    )
