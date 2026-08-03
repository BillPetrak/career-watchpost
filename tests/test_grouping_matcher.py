"""Tests for the deterministic duplicate-grouping matcher."""

from careeros.database.jobs import StoredJob
from careeros.grouping.matcher import (
    assign_groups,
    canonical_group_id,
    identity_for,
)


def make_job(
    job_id: int,
    *,
    title: str,
    company: str,
    source: str = "arbeitnow",
    location: str | None = None,
    remote_status: str | None = None,
) -> StoredJob:
    """Create a StoredJob suitable for matcher tests."""
    return StoredJob(
        id=job_id,
        source=source,
        source_job_id=f"{source}-{job_id}",
        title=title,
        company=company,
        location=location,
        description=None,
        url=None,
        employment_type=None,
        remote_status=remote_status,
        published_at=None,
        collected_at="2026-08-01T08:00:00+00:00",
        created_at="2026-08-01T08:00:00+00:00",
        updated_at="2026-08-01T08:00:00+00:00",
    )


def test_identity_normalizes_company_suffix() -> None:
    job = make_job(1, title="Python Engineer", company="Acme GmbH")
    ident = identity_for(job)
    assert ident.company_sig == "acme"
    assert ident.title_sig == "python engineer"


def test_canonical_group_id_is_stable() -> None:
    first = canonical_group_id("acme", "python engineer", "remote", "berlin")
    second = canonical_group_id("acme", "python engineer", "remote", "berlin")
    assert first == second
    assert len(first) == 16


def test_canonical_group_id_changes_with_identity() -> None:
    base = canonical_group_id("acme", "python engineer", "remote", "berlin")
    other = canonical_group_id("acme", "python engineer", "remote", "munich")
    assert base != other


def test_same_job_across_sources_groups() -> None:
    jobs = [
        make_job(
            1,
            title="Python Engineer",
            company="Acme GmbH",
            source="arbeitnow",
            location="Berlin",
        ),
        make_job(
            2,
            title="Python Engineer",
            company="Acme Inc",
            source="remoteok",
            location="Berlin, Germany",
        ),
    ]
    groups = assign_groups(jobs)
    assert len(groups) == 1
    assert [m.id for m in groups[0].members] == [1, 2]


def test_different_companies_do_not_group() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company="Acme"),
        make_job(2, title="Python Engineer", company="Globex"),
    ]
    assert assign_groups(jobs) == []


def test_different_titles_do_not_group() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company="Acme"),
        make_job(2, title="Backend Engineer", company="Acme"),
    ]
    assert assign_groups(jobs) == []


def test_remote_conflict_splits() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company="Acme", remote_status="remote"),
        make_job(
            2,
            title="Python Engineer",
            company="Acme",
            remote_status="onsite_or_unspecified",
        ),
    ]
    assert assign_groups(jobs) == []


def test_location_conflict_splits() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company="Acme", location="Berlin"),
        make_job(2, title="Python Engineer", company="Acme", location="Munich"),
    ]
    assert assign_groups(jobs) == []


def test_empty_locations_group_with_single_located_cluster() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company="Acme", location="Berlin"),
        make_job(2, title="Python Engineer", company="Acme"),
        make_job(3, title="Python Engineer", company="Acme"),
    ]
    groups = assign_groups(jobs)
    assert len(groups) == 1
    assert len(groups[0].members) == 3


def test_ambiguous_locations_keep_empty_group_separate() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company="Acme", location="Berlin"),
        make_job(2, title="Python Engineer", company="Acme", location="Munich"),
        make_job(3, title="Python Engineer", company="Acme"),
    ]
    # Berlin vs Munich is a hard location conflict, so nothing groups and the
    # empty-location member stays a singleton (it has no unique cluster to join).
    assert assign_groups(jobs) == []


def test_unidentifiable_company_or_title_never_groups() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company=None),
        make_job(2, title="Python Engineer", company=None),
        make_job(3, title="", company="Acme"),
        make_job(4, title="", company="Acme"),
    ]
    assert assign_groups(jobs) == []


def test_groups_are_sorted_by_min_id_and_reason_mentions_location() -> None:
    jobs = [
        make_job(1, title="Python Engineer", company="Acme", location="Berlin"),
        make_job(2, title="Python Engineer", company="Acme"),
    ]
    groups = assign_groups(jobs)
    assert len(groups) == 1
    assert "berlin" in groups[0].reason
    assert groups[0].remote_sig is None
