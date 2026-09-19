"""Human-facing enum label regression tests."""

from pathlib import Path

from careeros import cli
from careeros.collectors.models import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.presentation import enum_label


def test_enum_label_known_and_future_values() -> None:
    assert enum_label("onsite_or_unspecified") == "On-site or unspecified"
    assert enum_label("review_later") == "Review later"
    assert enum_label("follow_up") == "Follow up"
    assert enum_label("future_workflow_state") == "Future workflow state"
    assert enum_label("future-state") == "Future state"
    assert enum_label(None) == "N/A"


def test_show_renders_remote_enum_without_changing_storage(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "careeros.sqlite3"
    with connect(database) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                CollectedJob(
                    source="fixture",
                    source_job_id="1",
                    title="Engineer",
                    company="Example",
                    location="Athens",
                    description=None,
                    url=None,
                    employment_type="full_time",
                    remote_status="onsite_or_unspecified",
                    published_at=None,
                    collected_at="2026-09-18T12:00:00+00:00",
                )
            ],
        )
        job_id = connection.execute("SELECT id FROM jobs").fetchone()[0]

    assert cli.main(["show", str(job_id), "--database", str(database)]) == 0
    output = capsys.readouterr().out
    assert "Remote status:    On-site or unspecified" in output
    assert "Employment type:  Full time" in output
    assert "onsite_or_unspecified" not in output
    assert "full_time" not in output
    with connect(database) as connection:
        stored = connection.execute(
            "SELECT remote_status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
    assert stored == "onsite_or_unspecified"
