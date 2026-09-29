from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from careeros import source_config
from careeros.source_config import MAX_BOARDS, SourceConfigError, load_source_config


def _config(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_valid_lever_config_and_exact_schema(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """version: 1
sources:
  lever:
    enabled: true
    boards:
      - id: appfollow
        instance: global
        company: AppFollow
""",
    )
    config = load_source_config(path)
    assert config.lever.enabled is True
    assert config.lever.boards[0].company == "AppFollow"


def test_duplicate_keys_and_bool_version_rejected(tmp_path: Path) -> None:
    duplicate = _config(tmp_path, "version: 1\nsources: {}\nsources: {}\n")
    with pytest.raises(SourceConfigError):
        load_source_config(duplicate)
    duplicate.write_text("version: true\nsources: {}\n", encoding="utf-8")
    with pytest.raises(SourceConfigError):
        load_source_config(duplicate)


def test_missing_explicit_path_is_error_and_default_missing_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(tmp_path / "missing.yaml")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert load_source_config().lever.present is False


def test_environment_precedes_xdg_and_explicit_precedes_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = _config(tmp_path, "version: 1\nsources: {}\n")
    explicit_path = _config(
        tmp_path, "version: 1\nsources: {lever: {enabled: false}}\n"
    )
    monkeypatch.setenv("CAREEROS_SOURCES_CONFIG", str(env_path))
    assert load_source_config().path == env_path
    assert load_source_config(explicit_path).path == explicit_path


def test_mode_and_symlink_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, "version: 1\nsources: {}\n", mode=0o644)
    with pytest.raises(SourceConfigError):
        load_source_config(path)
    path.chmod(0o600)
    link = tmp_path / "link.yaml"
    link.symlink_to(path)
    with pytest.raises(SourceConfigError):
        load_source_config(link)


def test_effective_owner_mismatch_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, "version: 1\nsources: {}\n")
    actual_euid = os.geteuid()
    monkeypatch.setattr(source_config.os, "geteuid", lambda: actual_euid + 1)
    with pytest.raises(SourceConfigError, match="owned by the effective user"):
        load_source_config(path)


def test_mutation_during_descriptor_read_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, "version: 1\nsources: {}\n")
    original_fstat = os.fstat
    calls = 0

    def changing_fstat(fd: int) -> object:
        nonlocal calls
        current = original_fstat(fd)
        calls += 1
        if calls == 1:
            return current
        return SimpleNamespace(
            st_dev=current.st_dev,
            st_ino=current.st_ino,
            st_size=current.st_size,
            st_mtime_ns=current.st_mtime_ns + 1,
            st_ctime_ns=current.st_ctime_ns,
            st_uid=current.st_uid,
            st_mode=current.st_mode,
            st_nlink=current.st_nlink,
        )

    monkeypatch.setattr(source_config.os, "fstat", changing_fstat)
    with pytest.raises(SourceConfigError, match="changed while being read"):
        load_source_config(path)


@pytest.mark.parametrize("version", [0, 2, "1", True, None])
def test_invalid_versions(tmp_path: Path, version: object) -> None:
    path = _config(tmp_path, f"version: {version!r}\nsources: {{}}\n")
    with pytest.raises(SourceConfigError):
        load_source_config(path)


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\nextra: 2\nsources: {}\n",
        "version: 1\nsources: {other: {}}\n",
        "version: 1\nsources: {lever: {enabled: true, extra: 1}}\n",
        (
            "version: 1\nsources: {lever: {enabled: true, boards: "
            "[{id: b, instance: global, company: C, extra: 1}]}}\n"
        ),
        "version: 1\nsources: []\n",
    ],
)
def test_unknown_and_wrong_shape_keys(tmp_path: Path, text: str) -> None:
    with pytest.raises(SourceConfigError):
        load_source_config(_config(tmp_path, text))


def test_malformed_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(SourceConfigError, match="invalid YAML"):
        load_source_config(_config(tmp_path, "version: [\nsources: {}\n"))


@pytest.mark.parametrize(
    "board",
    [
        {"id": "A", "instance": "global", "company": "C"},
        {"id": "b", "instance": "us", "company": "C"},
        {"id": "b", "instance": "global", "company": "x" * 201},
    ],
)
def test_board_validation(tmp_path: Path, board: dict[str, str]) -> None:
    text = (
        "version: 1\nsources:\n  lever:\n    enabled: true\n    boards:\n"
        f"      - id: {board['id']}\n        instance: {board['instance']}\n"
        f"        company: {board['company']!r}\n"
    )
    with pytest.raises(SourceConfigError):
        load_source_config(_config(tmp_path, text))


def test_disabled_without_boards_is_valid(tmp_path: Path) -> None:
    path = _config(tmp_path, "version: 1\nsources: {lever: {enabled: false}}\n")
    assert load_source_config(path).lever.enabled is False


def test_enabled_without_boards_is_invalid(tmp_path: Path) -> None:
    path = _config(tmp_path, "version: 1\nsources: {lever: {enabled: true}}\n")
    with pytest.raises(SourceConfigError):
        load_source_config(path)


def test_board_count_and_canonical_duplicates_rejected(tmp_path: Path) -> None:
    boards = "\n".join(
        f"      - id: b{index}\n        instance: global\n        company: C{index}"
        for index in range(MAX_BOARDS + 1)
    )
    too_many = _config(
        tmp_path,
        f"version: 1\nsources:\n  lever:\n    enabled: true\n    boards:\n{boards}\n",
    )
    with pytest.raises(SourceConfigError, match=f"at most {MAX_BOARDS}"):
        load_source_config(too_many)

    duplicate = _config(
        tmp_path,
        """version: 1
sources:
  lever:
    enabled: true
    boards:
      - id: board
        instance: eu
        company: First
      - id: board
        instance: eu
        company: Second
""",
    )
    with pytest.raises(SourceConfigError, match="duplicate canonical Lever board"):
        load_source_config(duplicate)


def test_hardlink_and_fifo_rejected(tmp_path: Path) -> None:
    path = _config(tmp_path, "version: 1\nsources: {}\n")
    hardlink = tmp_path / "hardlink.yaml"
    hardlink.hardlink_to(path)
    with pytest.raises(SourceConfigError):
        load_source_config(path)
    fifo = tmp_path / "fifo.yaml"
    os.mkfifo(fifo)
    with pytest.raises(SourceConfigError):
        load_source_config(fifo)


def test_exact_size_limit_accepted_and_oversize_rejected(tmp_path: Path) -> None:
    base = "version: 1\nsources: {}\n"
    accepted = _config(tmp_path, base + "#" * (65536 - len(base)))
    assert load_source_config(accepted).version == 1
    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes((base + "#" * (65536 - len(base) + 1)).encode())
    oversized.chmod(0o600)
    with pytest.raises(SourceConfigError):
        load_source_config(oversized)


def test_invalid_utf8_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_bytes(b"version: 1\nsources: {}\n\xff")
    path.chmod(0o600)
    with pytest.raises(SourceConfigError):
        load_source_config(path)


def test_source_reads_do_not_create_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "missing-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    assert load_source_config().lever.present is False
    assert not config_home.exists()
