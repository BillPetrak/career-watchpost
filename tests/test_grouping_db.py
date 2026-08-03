"""Tests for canonical-group persistence and grouped retrieval."""

from pathlib import Path

from careeros.collectors.arbeitnow import CollectedJob
from careeros.database.connection import connect
from careeros.database.groups import (
    ensure_grouped,
    grouping_summary,
    list_group_infos,
    search_grouped,
)
from careeros.database.jobs import list_all_jobs, save_jobs
from careeros.database.schema import initialize_schema


def _make_job(
    *,
    source: str,
    source_job_id: str,
    title: str,
    company: str,
    location: str | None = None,
    remote_status: str | None = "remote",
    published_at: str | None = None,
) -> CollectedJob:
    return CollectedJob(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=location,
        description="Build things.",
        url=f"https://example.com/{source}/{source_job_id}",
        employment_type="Full-time",
        remote_status=remote_status,
        published_at=published_at,
        collected_at="2026-08-01T08:00:00+00:00",
    )


def _seed(tmp_path: Path) -> None:
    jobs = [
        _make_job(
            source="arbeitnow",
            source_job_id="a-1",
            title="Python Engineer",
            company="Acme GmbH",
            location="Berlin",
            published_at="2026-07-29T10:00:00Z",
        ),
        _make_job(
            source="remoteok",
            source_job_id="r-1",
            title="Python Engineer",
            company="Acme Inc",
            location="Berlin, Germany",
            published_at="2026-07-28T10:00:00Z",
        ),
        _make_job(
            source="weworkremotely",
            source_job_id="w-1",
            title="Rust Engineer",
            company="Systems Co",
            remote_status="onsite_or_unspecified",
        ),
    ]
    database_path = tmp_path / "careeros.sqlite3"
    with connect(database_path) as connection:
        initialize_schema(connection)
        save_jobs(connection, jobs)


def test_ensure_grouped_creates_tables_and_persists(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        stats = ensure_grouped(connection)
        summary = grouping_summary(connection)

    assert stats.groups == 1
    assert stats.members == 2
    assert summary.total_jobs == 3
    assert summary.grouped_jobs == 2
    assert summary.groups == 1


def test_ensure_grouped_is_idempotent(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        first = ensure_grouped(connection)
        second = ensure_grouped(connection)

    assert first == second
    assert second.groups == 1
    assert second.members == 2


def test_ensure_grouped_is_deterministic_after_reload(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        ensure_grouped(connection)
        first_ids = [
            row["group_id"]
            for row in connection.execute("SELECT group_id FROM job_group_members")
        ]

    with connect(database_path) as connection:
        ensure_grouped(connection)
        second_ids = [
            row["group_id"]
            for row in connection.execute("SELECT group_id FROM job_group_members")
        ]

    assert first_ids == second_ids
    assert len(set(first_ids)) == 1


def test_search_grouped_collapses_duplicates(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        ensure_grouped(connection)
        results = search_grouped(connection, "Python Engineer")

    assert len(results) == 1
    group = results[0]
    assert group.group_id is not None
    assert len(group.members) == 2
    assert group.sources == ("arbeitnow", "remoteok")


def test_search_grouped_returns_singletons_for_ungrouped(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        results = search_grouped(connection, "Rust")

    assert len(results) == 1
    group = results[0]
    assert group.group_id is None
    assert len(group.members) == 1
    assert group.representative.title == "Rust Engineer"


def test_search_grouped_source_filter_applies_before_grouping(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        ensure_grouped(connection)
        results = search_grouped(connection, "Python Engineer", source="remoteok")

    assert len(results) == 1
    assert results[0].representative.source == "remoteok"


def test_search_grouped_no_match(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        ensure_grouped(connection)
        results = search_grouped(connection, "NonExistentKeyword")

    assert results == []


def test_list_group_infos_reports_groups(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        ensure_grouped(connection)
        infos = list_group_infos(connection)

    assert len(infos) == 1
    info = infos[0]
    assert len(info.members) == 2
    assert info.sources == ("arbeitnow", "remoteok")
    assert "company+title match" in info.reason


def test_list_group_infos_filters_by_group_id(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        ensure_grouped(connection)
        info = list_group_infos(connection)[0]
        filtered = list_group_infos(connection, group_id=info.group_id)
        missing = list_group_infos(connection, group_id="deadbeef")

    assert len(filtered) == 1
    assert filtered[0].group_id == info.group_id
    assert missing == []


def test_grouping_never_mutates_jobs(tmp_path: Path) -> None:
    _seed(tmp_path)
    database_path = tmp_path / "careeros.sqlite3"

    with connect(database_path) as connection:
        before = list_all_jobs(connection)
        ensure_grouped(connection)
        after = list_all_jobs(connection)

    assert before == after
