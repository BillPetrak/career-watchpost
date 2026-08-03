"""Tests that regrouping never deletes or orphans application records."""

from datetime import datetime
from pathlib import Path

from careeros import cli
from careeros.applications.service import (
    application_statuses_for_jobs,
    group_tracking_info,
)
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import ensure_grouped, search_grouped
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema

_EPOCH = datetime.fromisoformat("2026-08-03T12:00:00+00:00")


def _make_job(*, source: str, source_job_id: str) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title="Python Engineer",
        company=("Acme GmbH" if source == "arbeitnow" else "Acme Inc"),
        location="Berlin",
        description="Build Python backends.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status="remote",
        published_at="2026-07-29T10:00:00Z",
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _seed(tmp_path: Path) -> Path:
    db_path = tmp_path / "careeros.sqlite3"
    with connect(db_path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [
                _make_job(source="arbeitnow", source_job_id="a-1"),
                _make_job(source="remoteok", source_job_id="r-1"),
            ],
        )
        ensure_grouped(connection)
    return db_path


def _application_rows(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, job_id, current_status, created_at, updated_at "
                "FROM applications ORDER BY id"
            )
        ]


def _event_rows(db_path: Path) -> list[tuple]:
    with connect(db_path) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT id, application_id, old_status, new_status, note, created_at "
                "FROM application_events ORDER BY id"
            )
        ]


def test_regroup_repeatedly_preserves_application_and_history(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with connect(db_path) as connection:
        job_id = connection.execute(
            "SELECT id FROM jobs WHERE source_job_id = ?", ("a-1",)
        ).fetchone()["id"]

    assert (
        cli.main(
            [
                "application",
                "set",
                str(job_id),
                "applied",
                "--note",
                "sent CV",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )
    cli.main(
        [
            "application",
            "set",
            str(job_id),
            "interview",
            "--database",
            str(db_path),
        ]
    )

    before_apps = _application_rows(db_path)
    before_events = _event_rows(db_path)
    assert len(before_apps) == 1
    assert len(before_events) == 2

    for _ in range(4):
        with connect(db_path) as connection:
            ensure_grouped(connection)
        cli.main(["group", "regroup", "--database", str(db_path)])

    assert _application_rows(db_path) == before_apps
    assert _event_rows(db_path) == before_events


def test_grouped_result_stays_tracked_after_regroup(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with connect(db_path) as connection:
        job_id = connection.execute(
            "SELECT id FROM jobs WHERE source_job_id = ?", ("a-1",)
        ).fetchone()["id"]

    assert (
        cli.main(
            [
                "application",
                "set",
                str(job_id),
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )

    for _ in range(3):
        with connect(db_path) as connection:
            ensure_grouped(connection)

    with connect(db_path) as connection:
        groups = search_grouped(connection, "Python Engineer")
        assert len(groups) == 1
        group = groups[0]
        statuses = application_statuses_for_jobs(connection, [job_id])
        info = group_tracking_info(group, statuses)

    assert len(group.members) == 2
    assert info.status == "applied"
    assert info.on_representative is True


def test_regroup_does_not_touch_application_tables(tmp_path: Path) -> None:
    db_path = _seed(tmp_path)
    with connect(db_path) as connection:
        job_id = connection.execute(
            "SELECT id FROM jobs WHERE source_job_id = ?", ("a-1",)
        ).fetchone()["id"]

    assert (
        cli.main(
            [
                "application",
                "set",
                str(job_id),
                "applied",
                "--database",
                str(db_path),
            ]
        )
        == 0
    )

    before_apps = _application_rows(db_path)

    with connect(db_path) as connection:
        ensure_grouped(connection)
        ensure_grouped(connection)

    assert _application_rows(db_path) == before_apps
