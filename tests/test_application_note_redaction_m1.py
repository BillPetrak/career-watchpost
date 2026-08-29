from pathlib import Path

from careeros.cli.commands.applications import (
    run_application_history,
    run_application_redact_note,
    run_application_set,
)
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema


def seeded(path: Path):
    with connect(path) as c:
        initialize_schema(c)
        c.execute(
            "INSERT INTO jobs(source,title,collected_at) VALUES "
            "('x','Synthetic','2026-01-01T00:00:00+00:00')"
        )
        c.commit()


def test_redaction_preserves_facts_and_is_idempotent(tmp_path: Path, capsys):
    path = tmp_path / "notes.sqlite3"
    seeded(path)
    assert run_application_set(1, "interested", "PRIVATE-SENTINEL", path) == 0
    row_before = (
        connect(path)
        .execute(
            "SELECT id,application_id,old_status,new_status,created_at "
            "FROM application_events"
        )
        .fetchone()
    )
    assert run_application_redact_note(1, path) == 0
    row = (
        connect(path)
        .execute(
            "SELECT note,note_redacted_at,id,application_id,old_status,"
            "new_status,created_at FROM application_events"
        )
        .fetchone()
    )
    assert row["note"] is None and row["note_redacted_at"] is not None
    assert tuple(
        row[k]
        for k in ("id", "application_id", "old_status", "new_status", "created_at")
    ) == tuple(
        row_before[k]
        for k in ("id", "application_id", "old_status", "new_status", "created_at")
    )
    marker = row["note_redacted_at"]
    assert run_application_redact_note(1, path) == 0
    assert (
        connect(path)
        .execute("SELECT note_redacted_at FROM application_events")
        .fetchone()[0]
        == marker
    )
    assert "PRIVATE-SENTINEL" not in capsys.readouterr().out


def test_no_note_is_not_marked_and_history_redacts(tmp_path: Path, capsys):
    path = tmp_path / "none.sqlite3"
    seeded(path)
    assert run_application_set(1, "interested", None, path) == 0
    assert run_application_redact_note(1, path) == 0
    assert (
        connect(path)
        .execute("SELECT note_redacted_at FROM application_events")
        .fetchone()[0]
        is None
    )
    with connect(path) as c:
        c.execute("UPDATE application_events SET note='PRIVATE-SENTINEL' WHERE id=1")
        c.commit()
    assert run_application_redact_note(1, path) == 0
    run_application_history(1, path)
    assert "[redacted]" in capsys.readouterr().out


def test_cli_note_length_rejected(tmp_path: Path, capsys):
    path = tmp_path / "bounds.sqlite3"
    seeded(path)
    assert run_application_set(1, "interested", "x" * 1001, path) == 2
    assert "x" * 1001 not in capsys.readouterr().err
