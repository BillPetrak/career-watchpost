"""Tests for the initial CareerOS repository structure."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_required_directories_exist() -> None:
    required_directories = [
        "config",
        "data/database",
        "data/exports",
        "data/raw",
        "docs",
        "logs",
        "scripts",
        "src/careeros",
        "tests",
    ]

    for relative_path in required_directories:
        assert (PROJECT_ROOT / relative_path).is_dir()


def test_private_files_are_not_created_initially() -> None:
    assert not (PROJECT_ROOT / ".env").exists()
    assert not (PROJECT_ROOT / "config/candidate_profile.yaml").exists()
    assert not (PROJECT_ROOT / "config/settings.yaml").exists()
