"""CLI tests for the profile show/validate commands (careeros profile)."""

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

import careeros.profile_storage as profile_storage
from careeros import cli


def _write_json_profile(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def _write_yaml_profile(path: Path, data: object) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    path.chmod(0o600)


_ALL_FIELDS = {
    "desired_titles": ["Machine Learning Engineer", "Data Scientist"],
    "skills": ["Python", "SQL"],
    "required_skills": ["Python"],
    "preferred_locations": ["Greece", "Remote"],
    "remote_preference": "remote",
    "excluded_terms": ["intern"],
    "hard_excluded_terms": ["commission"],
    "preferred_employment_types": ["Full-time"],
    "preferred_seniority": ["Senior"],
}


def test_cli_profile_show_valid_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.json"
    _write_json_profile(profile_path, _ALL_FIELDS)

    exit_code = cli.main(["profile", "show", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Profile: {profile_path}" in captured.out
    assert (
        "Desired titles:         Machine Learning Engineer, Data Scientist"
        in captured.out
    )
    assert "Skills:                 Python, SQL" in captured.out
    assert "Required skills:        Python" in captured.out
    assert "Preferred locations:    Greece, Remote" in captured.out
    assert "Remote preference:      Remote" in captured.out
    assert "Excluded terms:         intern" in captured.out
    assert "Hard excluded terms:    commission" in captured.out
    assert "Employment types:       Full-time" in captured.out
    assert "Seniority:              Senior" in captured.out


def test_cli_profile_show_valid_yaml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml_profile(profile_path, _ALL_FIELDS)

    exit_code = cli.main(["profile", "show", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Machine Learning Engineer" in captured.out
    assert "Remote preference:      Remote" in captured.out


def test_cli_profile_show_minimal_profile(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.json"
    _write_json_profile(profile_path, {})

    exit_code = cli.main(["profile", "show", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Desired titles:         -" in captured.out
    assert "Skills:                 -" in captured.out
    assert "Remote preference:      -" in captured.out


def test_cli_profile_show_missing_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "nonexistent.json"

    exit_code = cli.main(["profile", "show", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Failed to load profile" in captured.err


def test_cli_profile_show_malformed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{broken json", encoding="utf-8")
    profile_path.chmod(0o600)

    exit_code = cli.main(["profile", "show", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Failed to load profile" in captured.err


def test_cli_profile_show_unsupported_extension(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.txt"
    profile_path.write_text('{"desired_titles": ["Engineer"]}', encoding="utf-8")
    profile_path.chmod(0o600)

    exit_code = cli.main(["profile", "show", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Unsupported profile file extension" in captured.err


def test_cli_profile_validate_valid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml_profile(profile_path, _ALL_FIELDS)

    exit_code = cli.main(["profile", "validate", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Profile {profile_path} is valid." in captured.out


def test_cli_profile_validate_invalid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("desired_titles: [unclosed\n", encoding="utf-8")
    profile_path.chmod(0o600)

    exit_code = cli.main(["profile", "validate", str(profile_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Failed to load profile" in captured.err


def test_cli_profile_validate_does_not_touch_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.json"
    _write_json_profile(profile_path, {})

    exit_code = cli.main(["profile", "validate", str(profile_path)])
    capsys.readouterr()

    assert exit_code == 0
    assert not (tmp_path / "careeros.sqlite3").exists()


def test_cli_profile_missing_subcommand(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["profile"])
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_profile_unknown_subcommand(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    profile_path = tmp_path / "profile.json"
    _write_json_profile(profile_path, {})
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["profile", "inspect", str(profile_path)])
    assert exc_info.value.code == 2
    capsys.readouterr()


def test_cli_profile_init_creates_valid_yaml(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    target = tmp_path / "private" / "profile.yaml"

    exit_code = cli.main(["profile", "init", str(target)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Created candidate profile: {target}" in captured.out
    assert target.exists()
    validate = cli.main(["profile", "validate", str(target)])
    capsys.readouterr()
    assert validate == 0


@pytest.mark.parametrize("suffix", (".yaml", ".json"))
def test_cli_profile_init_private_modes_under_permissive_umask(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    suffix: str,
) -> None:
    target = tmp_path / "nested" / "private" / f"profile{suffix}"
    previous_umask = os.umask(0)
    try:
        assert cli.main(["profile", "init", str(target)]) == 0
    finally:
        os.umask(previous_umask)
    capsys.readouterr()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700


def test_cli_profile_init_creates_valid_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    target = tmp_path / "profile.json"

    exit_code = cli.main(["profile", "init", str(target)])
    capsys.readouterr()

    assert exit_code == 0
    assert target.exists()
    validate = cli.main(["profile", "validate", str(target)])
    capsys.readouterr()
    assert validate == 0


def test_cli_profile_init_defaults_to_private_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    target = config_home / "careeros" / "candidate_profile.yaml"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    exit_code = cli.main(["profile", "init"])
    capsys.readouterr()

    assert exit_code == 0
    assert target.exists()


@pytest.mark.parametrize("command", ("init", "show", "validate"))
def test_cli_profile_help_names_private_xdg_default(
    command: str,
    capsys: pytest.CaptureFixture,
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["profile", command, "--help"])

    assert exit_info.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "private XDG configuration path" in output
    assert "${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml" in output


def test_cli_profile_init_refuses_existing_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    target = tmp_path / "profile.yaml"
    target.write_text("desired_titles: [Existing]\n", encoding="utf-8")
    target.chmod(0o600)

    exit_code = cli.main(["profile", "init", str(target)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Profile already exists" in captured.err
    assert target.read_text(encoding="utf-8") == "desired_titles: [Existing]\n"


def test_cli_profile_init_existing_target_preserves_inode_and_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    target = tmp_path / "profile.yaml"
    _write_yaml_profile(target, {"desired_titles": ["Existing"]})
    before = target.stat().st_ino
    data = target.read_bytes()
    assert cli.main(["profile", "init", str(target)]) == 1
    capsys.readouterr()
    assert target.stat().st_ino == before
    assert target.read_bytes() == data


def test_cli_profile_init_force_overwrites(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    target = tmp_path / "profile.yaml"
    target.write_text("desired_titles: [Existing]\n", encoding="utf-8")
    target.chmod(0o600)
    before = target.stat().st_ino

    exit_code = cli.main(["profile", "init", str(target), "--force"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created candidate profile" in captured.out
    assert target.stat().st_ino != before
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_nlink == 1
    validate = cli.main(["profile", "validate", str(target)])
    capsys.readouterr()
    assert validate == 0


@pytest.mark.parametrize("kind", ("mode", "symlink", "hardlink", "directory", "fifo"))
def test_cli_profile_init_force_refuses_unsafe_target(
    tmp_path: Path, capsys: pytest.CaptureFixture, kind: str
) -> None:
    target = tmp_path / "profile.json"
    original = tmp_path / "original.json"
    _write_json_profile(original, {"desired_titles": ["Keep"]})
    if kind == "mode":
        _write_json_profile(target, {"desired_titles": ["Unsafe"]})
        target.chmod(0o644)
        before = target.read_bytes()
        inode = target.stat().st_ino
    elif kind == "symlink":
        target.symlink_to(original)
        before = original.read_bytes()
        inode = original.stat().st_ino
    elif kind == "hardlink":
        os.link(original, target)
        before = original.read_bytes()
        inode = original.stat().st_ino
    elif kind == "directory":
        target.mkdir()
        before = b""
        inode = target.stat().st_ino
    else:
        os.mkfifo(target)
        before = b""
        inode = target.stat().st_ino
    assert cli.main(["profile", "init", str(target), "--force"]) == 1
    capsys.readouterr()
    assert target.exists() or target.is_symlink()
    if kind in {"mode", "symlink", "hardlink"}:
        assert (original if kind != "mode" else target).read_bytes() == before
        assert (original if kind != "mode" else target).stat().st_ino == inode
    else:
        assert target.stat().st_ino == inode
    assert not list(tmp_path.glob(".profile.json.*.tmp"))


def test_cli_profile_init_race_target_appearance_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "profile.json"
    original_entry = profile_storage._entry
    calls = 0

    def racing_entry(fd: int, name: str) -> os.stat_result | None:
        nonlocal calls
        calls += 1
        result = original_entry(fd, name)
        if calls == 2 and result is None:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd
            )
            os.write(descriptor, b'{"desired_titles": ["Raced"]}')
            os.close(descriptor)
            return original_entry(fd, name)
        return result

    monkeypatch.setattr(profile_storage, "_entry", racing_entry)
    assert cli.main(["profile", "init", str(target)]) == 1
    capsys.readouterr()
    assert target.read_text(encoding="utf-8") == '{"desired_titles": ["Raced"]}'
    assert not list(tmp_path.glob(".profile.json.*.tmp"))


def test_cli_profile_init_write_failure_cleans_temp(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "profile.json"
    calls = 0
    real_write = profile_storage.os.write

    def failing_write(fd: int, payload: bytes) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_write(fd, payload[:1])
            raise OSError("injected write failure")
        return real_write(fd, payload)

    monkeypatch.setattr(profile_storage.os, "write", failing_write)
    assert cli.main(["profile", "init", str(target)]) == 1
    capsys.readouterr()
    assert not target.exists()
    assert not list(tmp_path.glob(".profile.json.*.tmp"))


def test_cli_profile_init_prepublication_fsync_failure_preserves_target(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "profile.json"
    _write_json_profile(target, {"desired_titles": ["Keep"]})
    before = target.read_bytes()
    calls = 0
    real_fsync = profile_storage.os.fsync

    def failing_directory_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(profile_storage.os, "fsync", failing_directory_fsync)
    assert cli.main(["profile", "init", str(target), "--force"]) == 1
    capsys.readouterr()
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".profile.json.*.tmp"))


def test_cli_profile_init_unlink_failure_rolls_back_publication(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "profile.json"
    real_unlink = profile_storage.os.unlink
    failed = False

    def failing_temp_unlink(name: str, *, dir_fd: int = -1) -> None:
        nonlocal failed
        if name.startswith(".profile.json.") and not failed:
            failed = True
            raise OSError("injected unlink failure")
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(profile_storage.os, "unlink", failing_temp_unlink)
    assert cli.main(["profile", "init", str(target)]) == 1
    capsys.readouterr()
    assert not target.exists()
    assert not list(tmp_path.glob(".profile.json.*.tmp"))


def test_cli_profile_init_postpublication_fsync_failure_still_succeeds(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "profile.json"
    _write_json_profile(target, {"desired_titles": ["Old"]})
    old_inode = target.stat().st_ino
    calls = 0
    real_fsync = profile_storage.os.fsync

    def failing_postpublication_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected postpublication fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(profile_storage.os, "fsync", failing_postpublication_fsync)
    assert cli.main(["profile", "init", str(target), "--force"]) == 0
    capsys.readouterr()
    assert target.stat().st_ino != old_inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.stat().st_nlink == 1
    assert cli.main(["profile", "validate", str(target)]) == 0
    capsys.readouterr()
    assert not list(tmp_path.glob(".profile.json.*.tmp"))


def test_cli_profile_init_rejects_unsupported_extension(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    target = tmp_path / "profile.txt"

    exit_code = cli.main(["profile", "init", str(target)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Unsupported profile file extension" in captured.err
    assert not target.exists()


def test_cli_profile_show_defaults_to_private_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    target = config_home / "careeros" / "candidate_profile.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml_profile(target, _ALL_FIELDS)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    exit_code = cli.main(["profile", "show"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Profile: {target}" in captured.out
    assert "Machine Learning Engineer" in captured.out


def test_cli_profile_validate_defaults_to_private_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    target = config_home / "careeros" / "candidate_profile.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml_profile(target, _ALL_FIELDS)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    exit_code = cli.main(["profile", "validate"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Profile {target} is valid." in captured.out
