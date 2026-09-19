"""Tests for the decomposed ``careeros.cli`` package structure (M2)."""

import ast
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from careeros import cli
from careeros.collectors import COLLECTORS
from careeros.locking import collection_lock

_SRC = Path(__file__).resolve().parents[1] / "src"
_PACKAGE_ROOT = _SRC / "careeros"
_COMMANDS = (
    "diagnose",
    "collect",
    "application",
    "search",
    "show",
    "rank",
    "profile",
    "report",
    "source",
    "group",
    "review",
    "reminder",
    "saved-search",
    "export",
)

_EXPORTED_HANDLERS = (
    "print_diagnostics",
    "run_collect",
    "run_collect_all",
    "run_collect_command",
    "run_collect_due",
    "run_collect_history",
    "run_collect_status",
    "run_application_set",
    "run_application_show",
    "run_application_list",
    "run_application_history",
    "run_application_command",
    "run_search",
    "run_search_command",
    "run_show",
    "run_show_command",
    "run_rank",
    "run_rank_command",
    "run_profile_init",
    "run_profile_show",
    "run_profile_validate",
    "run_profile_command",
    "run_report",
    "run_report_command",
    "run_source_stats",
    "run_source_command",
    "run_group_regroup",
    "run_group_explain",
    "run_group_command",
    "run_review_set",
    "run_review_clear",
    "run_review_show",
    "run_review_list",
    "run_review_history",
    "run_review_next",
    "run_review_command",
    "run_reminder_add",
    "run_reminder_list",
    "run_reminder_show",
    "run_reminder_update",
    "run_reminder_complete",
    "run_reminder_reopen",
    "run_reminder_delete",
    "run_reminder_notify_due",
    "run_reminder_notify_dry_run",
    "run_reminder_notify_history",
    "run_reminder_command",
    "run_saved_search_command",
    "run_export",
    "run_export_command",
)


def _run_in_fresh_interpreter(code: str) -> int:
    """Run Python code in a subprocess using the active interpreter."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SRC),
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def test_main_module_reported_as_package_main() -> None:
    assert cli.main.__module__ == "careeros.cli.main"


def test_entry_point_resolves_to_package_main() -> None:
    entries = {
        entry.name: entry.value
        for entry in metadata.entry_points(group="console_scripts")
    }
    assert entries["careeros"] == "careeros.cli:main"


def test_help_lists_all_commands(
    capsys: pytest.CaptureFixture,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    for name in _COMMANDS:
        assert name in captured.out
    assert captured.out.index("diagnose") < captured.out.index("export")


def test_group_help_lists_subcommands(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["group", "--help"])
    captured = capsys.readouterr()
    assert excinfo.value.code == 0
    assert "regroup" in captured.out
    assert "explain" in captured.out


def test_import_creates_no_files(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    env = dict(
        PYTHONPATH=str(_SRC),
        PYTHONNOUSERSITE="1",
        HOME=str(home),
        XDG_CACHE_HOME=str(tmp_path / "xdg"),
    )
    result = subprocess.run(
        [sys.executable, "-c", "import careeros.cli"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert list(cwd.iterdir()) == []
    assert list(home.iterdir()) == []
    suffixes = {".sqlite3", ".lock", ".yaml", ".yml", ".json", ".log", ".db"}
    assert not {
        child
        for child in (tmp_path / "xdg").rglob("*")
        if child.is_file() and child.suffix in suffixes
    }


def test_all_submodules_import_fresh_without_circular_failure() -> None:
    modules = [
        "careeros.cli",
        "careeros.cli.main",
        "careeros.cli.parser",
        "careeros.cli.render",
        "careeros.cli.common",
        "careeros.cli.commands",
        "careeros.cli.commands.diagnose",
        "careeros.cli.commands.collect",
        "careeros.cli.commands.applications",
        "careeros.cli.commands.reminders",
        "careeros.cli.commands.review",
        "careeros.cli.commands.search",
        "careeros.cli.commands.show",
        "careeros.cli.commands.rank",
        "careeros.cli.commands.profile",
        "careeros.cli.commands.report",
        "careeros.cli.commands.group",
        "careeros.cli.commands.source",
        "careeros.cli.commands.export",
        "careeros.cli.commands.saved_searches",
    ]
    code = "import importlib;" + ";".join(
        f"importlib.import_module({module!r})" for module in modules
    )
    assert _run_in_fresh_interpreter(code) == 0


def test_command_modules_import_before_package_init() -> None:
    code = (
        "import importlib;"
        "importlib.import_module('careeros.cli.commands.reminders');"
        "importlib.import_module('careeros.cli.commands.collect');"
        "from careeros.cli import main;"
        "assert main.__module__ == 'careeros.cli.main'"
    )
    assert _run_in_fresh_interpreter(code) == 0


def test_exit_code_zero() -> None:
    assert cli.main(["diagnose"]) == 0


def test_exit_code_two_on_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["saved-search", "create"])
    assert excinfo.value.code == 2


def test_exit_code_one_on_command_failure(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yaml"
    assert cli.main(["profile", "validate", str(missing)]) == 1


def test_exit_code_seventy_five_on_lock_contention(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with collection_lock(database_path):
        exit_code = cli.main(
            ["collect", "all", "--limit", "1", "--database", str(database_path)]
        )
    captured = capsys.readouterr()
    assert exit_code == 75
    assert "Collection already running for database:" in captured.err


def test_collectors_registry_is_identical_object() -> None:
    assert cli.COLLECTORS is COLLECTORS


def test_package_exports_compatibility_surface() -> None:
    for name in _EXPORTED_HANDLERS:
        assert callable(getattr(cli, name)), name
    assert cli.EXIT_TEMP_FAILURE == 75
    assert cli.DEFAULT_DATABASE_PATH.name == "careeros.sqlite3"
    assert cli.DEFAULT_PROFILE_PATH.name == "candidate_profile.yaml"
    assert cli.clean_description.__name__ == "clean_description"
    assert cli.load_profile_or_error.__name__ == "load_profile_or_error"
    assert cli.no_job_found_message.__name__ == "no_job_found_message"


def test_internal_names_not_reexported() -> None:
    assert not hasattr(cli, "deliver")
    assert not hasattr(cli, "_utc_now")


def _module_paths(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if path.name != "__init__.py" and path.is_file()
    )


def test_no_non_cli_module_imports_careeros_cli() -> None:
    cli_root = _PACKAGE_ROOT / "cli"
    violations: list[str] = []
    for path in _module_paths(_PACKAGE_ROOT):
        if (
            cli_root in path.parents
            or path == cli_root / "__init__.py"
            or path == _PACKAGE_ROOT / "__main__.py"
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name == "careeros.cli" or name.startswith("careeros.cli."):
                    violations.append(f"{path.relative_to(_SRC)}: {name}")
    assert violations == []
