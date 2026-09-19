"""Stable per-user default-path contract tests."""

from pathlib import Path

import pytest

from careeros.cli import main
from careeros.cli.parser import build_parser
from careeros.database.connection import connect, connect_read_only
from careeros.settings import (
    database_path,
    default_database_path,
    default_materials_root,
    default_profile_path,
    materials_root,
)


def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CAREEROS_DATABASE", raising=False)
    monkeypatch.delenv("CAREEROS_MATERIALS_ROOT", raising=False)
    return home


def test_defaults_follow_home_without_creating_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    assert default_database_path() == home / ".local/share/careeros/careeros.sqlite3"
    assert default_materials_root() == home / ".local/share/careeros/materials"
    assert default_profile_path() == home / ".config/careeros/candidate_profile.yaml"
    assert list(home.iterdir()) == []


def test_xdg_and_explicit_environment_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", "~/state")
    monkeypatch.setenv("XDG_CONFIG_HOME", "~/configuration")
    assert default_database_path() == home / "state/careeros/careeros.sqlite3"
    assert default_materials_root() == home / "state/careeros/materials"
    assert (
        default_profile_path() == home / "configuration/careeros/candidate_profile.yaml"
    )
    database = tmp_path / "explicit/database.sqlite3"
    materials = tmp_path / "explicit/materials"
    monkeypatch.setenv("CAREEROS_DATABASE", str(database))
    monkeypatch.setenv("CAREEROS_MATERIALS_ROOT", str(materials))
    assert database_path() == database
    assert materials_root() == materials


def test_cli_environment_default_and_explicit_database_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolated_home(monkeypatch, tmp_path)
    environment_database = tmp_path / "environment.sqlite3"
    explicit_database = tmp_path / "explicit.sqlite3"
    monkeypatch.setenv("CAREEROS_DATABASE", str(environment_database))
    parser = build_parser()
    assert parser.parse_args(["search", "python"]).database == environment_database
    assert (
        parser.parse_args(
            ["search", "python", "--database", str(explicit_database)]
        ).database
        == explicit_database
    )


def test_read_only_open_does_not_bootstrap_but_writer_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    database = default_database_path()
    with pytest.raises(FileNotFoundError):
        connect_read_only(database)
    assert list(home.iterdir()) == []
    with connect(database):
        pass
    assert database.is_file()
    assert database.stat().st_mode & 0o777 == 0o600


def test_profile_default_resolves_xdg_at_invocation_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = _isolated_home(monkeypatch, tmp_path)
    config_home = tmp_path / "configuration"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    assert main(["profile", "init"]) == 0

    profile = config_home / "careeros/candidate_profile.yaml"
    assert profile.is_file()
    assert not (home / ".config").exists()
