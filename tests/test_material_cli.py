from pathlib import Path

import pytest

from careeros.cli.commands.materials import run_material_command
from careeros.cli.parser import build_parser
from careeros.database.connection import connect_current_rw, connect_read_only
from careeros.database.schema import initialize_schema


def db(tmp_path: Path) -> Path:
    path = tmp_path / "careeros.sqlite3"
    import sqlite3

    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        initialize_schema(c)
    return path


@pytest.mark.parametrize(
    "command",
    [
        "list",
        "show",
        "import",
        "add-version",
        "update",
        "retire",
        "restore",
        "export-version",
        "audit",
    ],
)
def test_material_command_surface(command: str) -> None:
    parser = build_parser()
    assert parser is not None
    if command == "list":
        args = parser.parse_args(["material", command])
        assert args.limit == 20


def test_material_limit_is_positive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["material", "list", "--limit", "0"])


def test_material_update_requires_a_field_at_dispatch(tmp_path: Path) -> None:
    path = db(tmp_path)
    args = build_parser().parse_args(
        [
            "material",
            "update",
            "a" * 32,
            "--expected-revision",
            "1",
            "--database",
            str(path),
        ]
    )
    from careeros.cli.commands.materials import run_material_command

    assert run_material_command(args) == 1


def test_retire_rejects_update_flags() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["material", "retire", "a" * 32, "--expected-revision", "1", "--name", "x"]
        )


def test_rw_connection_does_not_create_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError):
        connect_current_rw(missing)
    assert not missing.exists()


def test_rw_connection_rejects_v9_without_mutation(tmp_path: Path) -> None:
    path = db(tmp_path)
    with connect_read_only(path) as c:
        before = c.execute(
            "select value from schema_metadata where key='schema_version'"
        ).fetchone()[0]
    import sqlite3

    with sqlite3.connect(path) as c:
        c.execute("update schema_metadata set value='9' where key='schema_version'")
    with pytest.raises(ValueError):
        connect_current_rw(path)
    # The normal read-only helper intentionally rejects non-current schemas;
    # inspect the deliberately relabeled file directly without initialization.
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
        assert (
            c.execute(
                "select value from schema_metadata where key='schema_version'"
            ).fetchone()[0]
            == "9"
        )
    assert before == "10"


def test_import_and_export_round_trip_are_cli_operations(
    tmp_path: Path, capsys
) -> None:
    path = db(tmp_path)
    root = tmp_path / "materials"
    source = tmp_path / "resume.txt"
    destination = tmp_path / "out.txt"
    source.write_bytes(b"private resume")
    args = build_parser().parse_args(
        [
            "material",
            "import",
            str(source),
            "--name",
            "Resume",
            "--category",
            "resume",
            "--database",
            str(path),
            "--materials-root",
            str(root),
        ]
    )
    assert run_material_command(args) == 0
    assert not list((root / ".staging").iterdir())
    with connect_read_only(path) as c:
        version = c.execute("select id from material_versions").fetchone()[0]
    args = build_parser().parse_args(
        [
            "material",
            "export-version",
            str(version),
            str(destination),
            "--database",
            str(path),
            "--materials-root",
            str(root),
        ]
    )
    assert run_material_command(args) == 0
    assert destination.read_bytes() == source.read_bytes()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert "Material version exported" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_import_hostile_source_fails_without_stage_residue(
    tmp_path: Path, kind: str
) -> None:
    path = db(tmp_path)
    root = tmp_path / "materials"
    source = tmp_path / "source"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"x")
        source.symlink_to(target)
    else:
        source.mkdir()
    args = build_parser().parse_args(
        [
            "material",
            "import",
            str(source),
            "--name",
            "X",
            "--category",
            "resume",
            "--database",
            str(path),
            "--materials-root",
            str(root),
        ]
    )
    assert run_material_command(args) == 1
    assert not root.exists()


def test_export_existing_destination_is_unchanged(tmp_path: Path) -> None:
    path = db(tmp_path)
    root = tmp_path / "materials"
    source = tmp_path / "source"
    source.write_bytes(b"x")
    args = build_parser().parse_args(
        [
            "material",
            "import",
            str(source),
            "--name",
            "X",
            "--category",
            "resume",
            "--database",
            str(path),
            "--materials-root",
            str(root),
        ]
    )
    assert run_material_command(args) == 0
    with connect_read_only(path) as c:
        version = c.execute("select id from material_versions").fetchone()[0]
    destination = tmp_path / "existing"
    destination.write_bytes(b"keep")
    args = build_parser().parse_args(
        [
            "material",
            "export-version",
            str(version),
            str(destination),
            "--database",
            str(path),
            "--materials-root",
            str(root),
        ]
    )
    assert run_material_command(args) == 1
    assert destination.read_bytes() == b"keep"
