"""The ``profile`` command family (candidate profile files)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from careeros.cli.common import load_profile_or_error
from careeros.presentation import enum_label
from careeros.ranking.scorer import CandidateProfile
from careeros.settings import default_profile_path

_PROFILE_YAML_SUFFIXES = {".yaml", ".yml"}
_PROFILE_JSON_SUFFIXES = {".json"}

_YAML_PROFILE_TEMPLATE = """\
# CareerOS candidate profile.
#
# Describes the roles you are looking for. All fields are optional and unknown
# top-level fields are ignored. This file is private: it is listed in
# .gitignore and must never be committed to version control.
#
#   desired_titles:             ["Machine Learning Engineer", "Data Scientist"]
#   skills:                     ["Python", "SQL"]
#   required_skills:            ["Python"]
#   preferred_locations:        ["Greece", "Remote"]
#   remote_preference:          "remote" or "onsite_or_unspecified" or null
#   excluded_terms:             ["intern"]
#   hard_excluded_terms:        ["commission"]
#   preferred_employment_types: ["Full-time"]
#   preferred_seniority:        ["Senior"]

desired_titles: []
skills: []
required_skills: []
preferred_locations: []
remote_preference: null
excluded_terms: []
hard_excluded_terms: []
preferred_employment_types: []
preferred_seniority: []
"""

_JSON_PROFILE_TEMPLATE = """\
{
  "desired_titles": [],
  "skills": [],
  "required_skills": [],
  "preferred_locations": [],
  "remote_preference": null,
  "excluded_terms": [],
  "hard_excluded_terms": [],
  "preferred_employment_types": [],
  "preferred_seniority": []
}
"""


def _render_profile(profile: CandidateProfile) -> None:
    """Render a candidate profile as readable labeled lines."""
    print(f"{'Desired titles:':<24}{', '.join(profile.desired_titles) or '-'}")
    print(f"{'Skills:':<24}{', '.join(profile.skills) or '-'}")
    print(f"{'Required skills:':<24}{', '.join(profile.required_skills) or '-'}")
    print(
        f"{'Preferred locations:':<24}{', '.join(profile.preferred_locations) or '-'}"
    )
    print(
        f"{'Remote preference:':<24}{enum_label(profile.remote_preference, empty='-')}"
    )
    print(f"{'Excluded terms:':<24}{', '.join(profile.excluded_terms) or '-'}")
    print(
        f"{'Hard excluded terms:':<24}{', '.join(profile.hard_excluded_terms) or '-'}"
    )
    print(
        f"{'Employment types:':<24}"
        f"{', '.join(profile.preferred_employment_types) or '-'}"
    )
    print(f"{'Seniority:':<24}{', '.join(profile.preferred_seniority) or '-'}")


def run_profile_init(profile_path: Path | None, *, force: bool) -> int:
    """Create a starter candidate profile at a path (default: private path).

    The format is chosen by file extension: ``.yaml``/``.yml`` write a YAML
    starter and ``.json`` a JSON starter. Refuses to overwrite an existing file
    unless ``--force`` is passed.
    """
    path = profile_path if profile_path is not None else default_profile_path()

    suffix = path.suffix.lower()
    if suffix in _PROFILE_YAML_SUFFIXES:
        template = _YAML_PROFILE_TEMPLATE
    elif suffix in _PROFILE_JSON_SUFFIXES:
        template = _JSON_PROFILE_TEMPLATE
    else:
        print(
            f"Unsupported profile file extension {path.suffix!r}; "
            "expected .json, .yaml, or .yml.",
            file=sys.stderr,
        )
        return 2

    if path.exists() and not force:
        print(f"Profile already exists: {path}", file=sys.stderr)
        print("Use --force to overwrite it.", file=sys.stderr)
        return 1

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template, encoding="utf-8")
    except OSError as error:
        print(
            f"Failed to create profile: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print(f"Created candidate profile: {path}")
    return 0


def run_profile_show(profile_path: Path | None) -> int:
    """Show a candidate profile from a JSON or YAML file (read-only, no DB)."""
    path = profile_path if profile_path is not None else default_profile_path()
    profile = load_profile_or_error(path)
    if profile is None:
        return 1

    print(f"Profile: {path}")
    print("-" * len(f"Profile: {path}"))
    _render_profile(profile)
    return 0


def run_profile_validate(profile_path: Path | None) -> int:
    """Validate a candidate profile file without displaying its contents."""
    path = profile_path if profile_path is not None else default_profile_path()
    if load_profile_or_error(path) is None:
        return 1

    print(f"Profile {path} is valid.")
    return 0


def run_profile_command(arguments: argparse.Namespace) -> int:
    """Dispatch a ``profile`` subcommand."""
    if arguments.profile_command == "init":
        return run_profile_init(arguments.path, force=arguments.force)
    if arguments.profile_command == "show":
        return run_profile_show(arguments.path)
    return run_profile_validate(arguments.path)
