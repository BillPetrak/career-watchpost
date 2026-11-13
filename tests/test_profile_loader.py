"""Tests for the JSON and YAML candidate profile loader."""

import json
import os
from pathlib import Path

import pytest
import yaml

import careeros.profile_storage as profile_storage
from careeros.ranking.profile import load_profile


def _write_profile(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def _write_yaml_profile(path: Path, data: object) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    path.chmod(0o600)


def test_valid_minimal_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})

    profile = load_profile(profile_path)

    assert profile.desired_titles == ()
    assert profile.skills == ()
    assert profile.preferred_locations == ()
    assert profile.remote_preference is None
    assert profile.excluded_terms == ()


def test_valid_all_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {
            "desired_titles": ["Data Scientist", "ML Engineer"],
            "skills": ["Python", "SQL"],
            "preferred_locations": ["Greece", "Remote"],
            "remote_preference": "remote",
            "excluded_terms": ["intern"],
        },
    )

    profile = load_profile(profile_path)

    assert profile.desired_titles == ("Data Scientist", "ML Engineer")
    assert profile.skills == ("Python", "SQL")
    assert profile.preferred_locations == ("Greece", "Remote")
    assert profile.remote_preference == "remote"
    assert profile.excluded_terms == ("intern",)


def test_accepts_str_path(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Engineer"]})

    profile = load_profile(str(profile_path))

    assert profile.desired_titles == ("Engineer",)


@pytest.mark.parametrize("mode", (0o644, 0o664))
def test_rejects_permissive_modes(tmp_path: Path, mode: int) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})
    profile_path.chmod(mode)
    with pytest.raises(ValueError, match="0600"):
        load_profile(profile_path)


def test_rejects_symlink_hardlink_directory_and_fifo(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})
    link = tmp_path / "link.json"
    link.symlink_to(profile_path)
    with pytest.raises(ValueError):
        load_profile(link)
    hardlink = tmp_path / "hard.json"
    os.link(profile_path, hardlink)
    with pytest.raises(ValueError, match="one link"):
        load_profile(hardlink)
    directory = tmp_path / "directory.json"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular"):
        load_profile(directory)
    fifo = tmp_path / "profile-fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        load_profile(fifo)


def test_missing_profile_read_does_not_create_state(tmp_path: Path) -> None:
    profile_path = tmp_path / "missing" / "profile.json"
    with pytest.raises(FileNotFoundError):
        load_profile(profile_path)
    assert not profile_path.parent.exists()


def test_rejects_foreign_owner_without_chown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {})
    real_euid = os.geteuid()
    monkeypatch.setattr(profile_storage.os, "geteuid", lambda: real_euid + 1)
    with pytest.raises(ValueError, match="owned"):
        load_profile(profile_path)


def test_missing_optional_fields_default(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Engineer"]})

    profile = load_profile(profile_path)

    assert profile.desired_titles == ("Engineer",)
    assert profile.skills == ()
    assert profile.preferred_locations == ()
    assert profile.remote_preference is None
    assert profile.excluded_terms == ()


def test_remote_preference_null(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path, {"remote_preference": None, "desired_titles": ["Engineer"]}
    )

    profile = load_profile(profile_path)

    assert profile.remote_preference is None


def test_unknown_fields_ignored(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path,
        {
            "desired_titles": ["Engineer"],
            "unknown_field": "ignored",
            "another_unknown": [1, 2, 3],
        },
    )

    profile = load_profile(profile_path)

    assert profile.desired_titles == ("Engineer",)
    assert profile.skills == ()


def test_rejects_empty_string_in_list(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Engineer", "", "Scientist"]})

    with pytest.raises(ValueError, match="empty string"):
        load_profile(profile_path)


def test_rejects_whitespace_only_string_in_list(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Engineer", "   "]})

    with pytest.raises(ValueError, match="empty string"):
        load_profile(profile_path)


def test_rejects_non_list_field(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": "Engineer"})

    with pytest.raises(ValueError, match="must be a list"):
        load_profile(profile_path)


def test_rejects_non_string_in_list(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"desired_titles": ["Engineer", 42]})

    with pytest.raises(ValueError, match="must contain only strings"):
        load_profile(profile_path)


def test_rejects_boolean_in_list(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"skills": [True]})

    with pytest.raises(ValueError, match="must contain only strings"):
        load_profile(profile_path)


def test_rejects_non_string_remote_preference(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"remote_preference": 123})

    with pytest.raises(ValueError, match="must be a string or null"):
        load_profile(profile_path)


def test_rejects_boolean_remote_preference(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, {"remote_preference": True})

    with pytest.raises(ValueError, match="must be a string or null"):
        load_profile(profile_path)


def test_malformed_json(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{broken json", encoding="utf-8")
    profile_path.chmod(0o600)

    with pytest.raises(json.JSONDecodeError):
        load_profile(profile_path)


def test_empty_file(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("", encoding="utf-8")
    profile_path.chmod(0o600)

    with pytest.raises(json.JSONDecodeError):
        load_profile(profile_path)


def test_whitespace_only_file(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("   \n\n  ", encoding="utf-8")
    profile_path.chmod(0o600)

    with pytest.raises(json.JSONDecodeError):
        load_profile(profile_path)


def test_non_object_root(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, [1, 2, 3])

    with pytest.raises(ValueError, match="must be a JSON object"):
        load_profile(profile_path)


def test_string_root(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, "hello")

    with pytest.raises(ValueError, match="must be a JSON object"):
        load_profile(profile_path)


def test_null_root(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(profile_path, None)

    with pytest.raises(ValueError, match="must be a JSON object"):
        load_profile(profile_path)


def test_missing_file(tmp_path: Path) -> None:
    profile_path = tmp_path / "nonexistent.json"

    with pytest.raises(FileNotFoundError):
        load_profile(profile_path)


def test_strips_whitespace_from_list_entries(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    _write_profile(
        profile_path, {"desired_titles": ["  Data Scientist  ", "  Engineer  "]}
    )

    profile = load_profile(profile_path)

    assert profile.desired_titles == ("Data Scientist", "Engineer")


# --- YAML loading --------------------------------------------------------------


def test_valid_yaml_minimal(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml_profile(profile_path, {})

    profile = load_profile(profile_path)

    assert profile.desired_titles == ()
    assert profile.skills == ()
    assert profile.remote_preference is None


def test_valid_yaml_all_fields(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yml"
    _write_yaml_profile(
        profile_path,
        {
            "desired_titles": ["Data Scientist", "ML Engineer"],
            "skills": ["Python", "SQL"],
            "preferred_locations": ["Greece", "Remote"],
            "remote_preference": "remote",
            "excluded_terms": ["intern"],
        },
    )

    profile = load_profile(profile_path)

    assert profile.desired_titles == ("Data Scientist", "ML Engineer")
    assert profile.skills == ("Python", "SQL")
    assert profile.preferred_locations == ("Greece", "Remote")
    assert profile.remote_preference == "remote"
    assert profile.excluded_terms == ("intern",)


def test_yaml_unknown_fields_ignored(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml_profile(
        profile_path, {"desired_titles": ["Engineer"], "unknown_field": 123}
    )

    profile = load_profile(profile_path)

    assert profile.desired_titles == ("Engineer",)
    assert profile.skills == ()


def test_yaml_rejects_non_list_field(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml_profile(profile_path, {"desired_titles": "Engineer"})

    with pytest.raises(ValueError, match="must be a list"):
        load_profile(profile_path)


def test_yaml_rejects_malformed(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("desired_titles: [unclosed\n", encoding="utf-8")
    profile_path.chmod(0o600)

    with pytest.raises(ValueError, match="Invalid YAML"):
        load_profile(profile_path)


def test_yaml_empty_file(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("", encoding="utf-8")
    profile_path.chmod(0o600)

    with pytest.raises(ValueError, match="YAML mapping"):
        load_profile(profile_path)


def test_yaml_non_mapping_root(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml_profile(profile_path, ["not", "a", "mapping"])

    with pytest.raises(ValueError, match="YAML mapping"):
        load_profile(profile_path)


def test_yaml_scalar_root(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.yaml"
    _write_yaml_profile(profile_path, "hello")

    with pytest.raises(ValueError, match="YAML mapping"):
        load_profile(profile_path)


# --- extension handling --------------------------------------------------------


def test_unsupported_extension_rejected(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.txt"
    profile_path.write_text('{"desired_titles": ["Engineer"]}', encoding="utf-8")
    profile_path.chmod(0o600)

    with pytest.raises(ValueError, match="Unsupported profile file extension"):
        load_profile(profile_path)
