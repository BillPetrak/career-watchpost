"""Tests for the read-only unified source statistics service."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from careeros.collectors import COLLECTORS
from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.jobs import save_jobs
from careeros.database.schema import initialize_schema
from careeros.source_stats.models import MISSING_FIELD_COLUMNS
from careeros.source_stats.service import compute_source_stats


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str = "Python Engineer",
    company: str | None = "Acme GmbH",
    location: str | None = "Berlin",
    description: str | None = "Build things with Python.",
    url: str | None = "https://example.com/job",
    employment_type: str | None = "Full-time",
    remote_status: str | None = "remote",
    published_at: str | None = "2026-07-29T10:00:00Z",
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description=description,
        url=url,
        employment_type=employment_type,
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _open(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "careeros.sqlite3")
    initialize_schema(connection)
    return connection


def _compute(tmp_path: Path) -> tuple[object, ...]:
    with _open(tmp_path) as connection:
        return compute_source_stats(connection)


def _stats_by_source(tmp_path: Path) -> dict[str, object]:
    with _open(tmp_path) as connection:
        return {stats.source: stats for stats in compute_source_stats(connection)}


def _field(stats: object, column: str) -> object:
    return next(
        (field for field in stats.fields if field.field == column),
        None,
    )


def test_tracked_fields_are_the_eight_nullable_columns() -> None:
    assert MISSING_FIELD_COLUMNS == (
        "source_job_id",
        "company",
        "location",
        "description",
        "url",
        "employment_type",
        "remote_status",
        "published_at",
    )


def test_empty_database_has_zero_counts_and_no_coverage(tmp_path: Path) -> None:
    stats = _compute(tmp_path)
    assert len(stats) == len(COLLECTORS)
    assert tuple(entry.source for entry in stats) == tuple(COLLECTORS)
    for entry in stats:
        assert entry.total_jobs == 0
        assert entry.never_groupable_jobs == 0
        assert len(entry.fields) == 8
        for field in entry.fields:
            assert field.missing_count == 0
            assert field.provided_count == 0
            assert field.coverage_pct is None


def test_counts_and_coverage_per_source(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-1",
                    location=None,
                    url="https://example.com/a-1",
                ),
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-2",
                    location="Munich",
                    employment_type=None,
                ),
                _make_job(
                    source="arbeitnow",
                    source_job_id="a-3",
                    location="Berlin",
                    description=None,
                ),
                _make_job(source="arbeitnow", source_job_id="a-4"),
            ],
        )

    by_source = _stats_by_source(tmp_path)
    arbeitnow = by_source["arbeitnow"]
    assert arbeitnow.total_jobs == 4

    location = _field(arbeitnow, "location")
    assert location.missing_count == 1
    assert location.provided_count == 3
    assert location.coverage_pct == 75

    employment_type = _field(arbeitnow, "employment_type")
    assert employment_type.missing_count == 1
    assert employment_type.coverage_pct == 75

    description = _field(arbeitnow, "description")
    assert description.missing_count == 1
    assert description.coverage_pct == 75

    company = _field(arbeitnow, "company")
    assert company.missing_count == 0
    assert company.provided_count == 4
    assert company.coverage_pct == 100

    for entry in by_source.values():
        if entry.source == "arbeitnow":
            continue
        assert entry.total_jobs == 0


def test_empty_string_counts_as_missing(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(source="jobicy", source_job_id="j-1", company=""),
            ],
        )

    by_source = _stats_by_source(tmp_path)
    jobicy = by_source["jobicy"]
    assert jobicy.total_jobs == 1
    company = _field(jobicy, "company")
    assert company.missing_count == 1
    assert company.provided_count == 0
    assert company.coverage_pct == 0


def test_never_groupable_counts_unidentifiable_jobs(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(source="remoteok", source_job_id="r-1", company=None),
                _make_job(source="remoteok", source_job_id="r-2", company="GmbH"),
                _make_job(source="remoteok", source_job_id="r-3", title=""),
                _make_job(source="remoteok", source_job_id="r-4"),
            ],
        )

    by_source = _stats_by_source(tmp_path)
    remoteok = by_source["remoteok"]
    assert remoteok.total_jobs == 4
    assert remoteok.never_groupable_jobs == 3


def test_never_groupable_matches_grouping_identity(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(
                    source="himalayas",
                    source_job_id="h-1",
                    company="Acme GmbH",
                    title="Engineer",
                ),
                _make_job(
                    source="himalayas",
                    source_job_id="h-2",
                    company="Acme Inc",
                    title="Engineer",
                ),
            ],
        )

    by_source = _stats_by_source(tmp_path)
    himalayas = by_source["himalayas"]
    assert himalayas.never_groupable_jobs == 0


def test_order_is_default_registry_order_and_can_be_overridden(
    tmp_path: Path,
) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [_make_job(source="jobicy", source_job_id="j-1")],
        )

    stats = _compute(tmp_path)
    assert [entry.source for entry in stats] == list(COLLECTORS)
    assert stats[0].source == "arbeitnow"
    assert stats[3].source == "jobicy"

    with _open(tmp_path) as connection:
        custom = compute_source_stats(
            connection,
            order=("jobicy", "arbeitnow"),
        )
    assert [entry.source for entry in custom] == ["jobicy", "arbeitnow"]
    assert custom[0].total_jobs == 1


def test_compute_source_stats_does_not_mutate_database(tmp_path: Path) -> None:
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(
            connection,
            [_make_job(source="remotive", source_job_id="m-1")],
        )

        def snapshot() -> list[tuple[object, ...]]:
            return [
                tuple(row)
                for row in connection.execute("SELECT * FROM jobs ORDER BY id")
            ]

        before = snapshot()

    with connect(database_path) as connection:
        compute_source_stats(connection)

    with connect(database_path) as connection:
        assert snapshot() == before


def test_fractional_coverage_rounds(tmp_path: Path) -> None:
    with _open(tmp_path) as connection:
        save_jobs(
            connection,
            [
                _make_job(source="weworkremotely", source_job_id="w-1", url=None),
                _make_job(source="weworkremotely", source_job_id="w-2", url=None),
                _make_job(source="weworkremotely", source_job_id="w-3"),
            ],
        )

    by_source = _stats_by_source(tmp_path)
    weworkremotely = by_source["weworkremotely"]
    url = _field(weworkremotely, "url")
    assert url.missing_count == 2
    assert url.provided_count == 1
    assert url.coverage_pct == 33
