"""CLI tests for the profile show/validate commands (careeros profile)."""

import json
from pathlib import Path

import pytest
import yaml

from careeros import cli


def _write_json_profile(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_yaml_profile(path: Path, data: object) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


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

    exit_code = cli.main(["profile", "init", str(target)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Profile already exists" in captured.err
    assert target.read_text(encoding="utf-8") == "desired_titles: [Existing]\n"


def test_cli_profile_init_force_overwrites(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    target = tmp_path / "profile.yaml"
    target.write_text("desired_titles: [Existing]\n", encoding="utf-8")

    exit_code = cli.main(["profile", "init", str(target), "--force"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created candidate profile" in captured.out
    validate = cli.main(["profile", "validate", str(target)])
    capsys.readouterr()
    assert validate == 0


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
