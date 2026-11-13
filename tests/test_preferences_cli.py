"""R4 M3 persistent preference CLI contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from careeros import cli
from careeros.cli.commands import preferences as preference_commands
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped
from careeros.database.jobs import save_jobs
from careeros.database.preferences import list_preference_evidence, list_suppressions
from careeros.database.schema import initialize_schema
from careeros.locking import CollectionLockConflict
from careeros.preferences.service import inspect_preferences
from careeros.review.service import set_decision


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "cli-preferences.sqlite3"
    with connect(path) as connection:
        initialize_schema(connection)
    return path


def _seed_learned(path: Path) -> str:
    with connect(path) as connection:
        save_jobs(
            connection,
            [
                CollectedJob(
                    source=f"fixture-{index}",
                    source_job_id=str(index),
                    title="Backend Engineer",
                    company=f"Company {index}",
                    location=f"City {index}",
                    description="private-description",
                    url=f"https://private.example/{index}",
                    employment_type="Full-time",
                    remote_status="remote",
                    published_at=None,
                    collected_at="2026-09-16T12:00:00+00:00",
                )
                for index in range(2)
            ],
        )
        ensure_grouped(connection)
        ids = [int(row[0]) for row in connection.execute("SELECT id FROM jobs")]
        for job_id in ids:
            set_decision(connection, job_id=job_id, decision="shortlist")
        return next(
            item.preference_key
            for item in inspect_preferences(connection).learned
            if item.dimension == "title"
        )


def test_show_text_and_json_distinguish_absent_and_configured_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _database(tmp_path)
    assert cli.main(["preference", "show", "--database", str(path)]) == 0
    assert "Stored explicit configured: no" in capsys.readouterr().out

    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    empty.chmod(0o600)
    assert (
        cli.main(
            [
                "preference",
                "explicit",
                "replace",
                str(empty),
                "--database",
                str(path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli.main(["preference", "show", "--format", "json", "--database", str(path)])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert list(payload) == [
        "explicit",
        "learned",
        "learning_reset_at",
        "learning_reset_review_event_id",
    ]
    assert payload["explicit"]["configured"] is True
    assert all(
        value == []
        for key, value in payload["explicit"]["profile"].items()
        if key != "remote_preference"
    )
    assert payload["explicit"]["profile"]["remote_preference"] is None


def test_replace_round_trip_clear_suppress_restore_and_reset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _database(tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "desired_titles": ["Backend", "Backend", "Platform"],
                "skills": ["Python", "SQL", "Python"],
                "required_skills": ["Git", "Git"],
                "preferred_locations": ["Athens", "Berlin", "Athens"],
                "remote_preference": "hybrid",
                "excluded_terms": ["crypto", "crypto"],
                "hard_excluded_terms": ["unpaid", "unpaid"],
                "preferred_employment_types": ["full-time", "contract"],
                "preferred_seniority": ["senior", "lead"],
            }
        ),
        encoding="utf-8",
    )
    profile.chmod(0o600)
    assert (
        cli.main(
            ["preference", "explicit", "replace", str(profile), "--database", str(path)]
        )
        == 0
    )
    capsys.readouterr()
    key = _seed_learned(path)
    assert cli.main(["preference", "suppress", key, "--database", str(path)]) == 0
    assert cli.main(["preference", "restore", key, "--database", str(path)]) == 0
    assert cli.main(["preference", "suppress", key, "--database", str(path)]) == 0
    assert (
        cli.main(["preference", "reset-learned", "--confirm", "--database", str(path)])
        == 0
    )
    with connect(path) as connection:
        inspection = inspect_preferences(connection)
        assert inspection.explicit.profile.desired_titles == (
            "Backend",
            "Backend",
            "Platform",
        )
        assert inspection.learned == ()
        assert list_preference_evidence(connection) == ()
        assert list_suppressions(connection) == ()
    assert cli.main(["preference", "explicit", "clear", "--database", str(path)]) == 0
    with connect(path) as connection:
        assert inspect_preferences(connection).explicit.configured is False


def test_unknown_key_and_lock_contention_fail_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path)
    assert cli.main(["preference", "suppress", "bad", "--database", str(path)]) == 2
    assert "invalid learned preference key" in capsys.readouterr().err

    def blocked(*args, **kwargs):
        raise CollectionLockConflict(path, Path(str(path) + ".lock"), {})

    monkeypatch.setattr(preference_commands, "collection_lock", blocked)
    assert cli.main(["preference", "explicit", "clear", "--database", str(path)]) == 75
    assert "already running" in capsys.readouterr().err


def test_reset_requires_confirm_and_never_mutates_without_it(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    _seed_learned(path)
    with connect(path) as connection:
        before = list_preference_evidence(connection)
    with pytest.raises(SystemExit) as raised:
        cli.main(["preference", "reset-learned", "--database", str(path)])
    assert raised.value.code == 2
    with connect(path) as connection:
        assert list_preference_evidence(connection) == before


def test_show_json_is_allow_listed_and_private_fields_are_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _database(tmp_path)
    _seed_learned(path)
    assert (
        cli.main(["preference", "show", "--format", "json", "--database", str(path)])
        == 0
    )
    output = capsys.readouterr().out
    item = json.loads(output)["learned"][0]
    assert set(item) == {
        "key",
        "dimension",
        "display_value",
        "normalized_value",
        "polarity",
        "positive_count",
        "negative_count",
        "net_evidence",
        "strength_level",
        "active",
        "activation_reason",
        "suppressed",
        "positive_examples",
        "negative_examples",
    }
    assert set(item["positive_examples"][0]) == {"job_id", "title", "company"}
    for private in (
        "private-description",
        "private.example",
        "review note",
        "url",
        "source",
    ):
        assert private not in output
