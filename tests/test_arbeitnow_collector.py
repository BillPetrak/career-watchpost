"""Tests for the Arbeitnow collector."""

from typing import Any

from careeros.collectors.arbeitnow import normalize_job


def test_normalize_job() -> None:
    raw_job: dict[str, Any] = {
        "slug": "python-engineer-example-company",
        "title": "Python Engineer",
        "company_name": "Example Company",
        "location": "Athens",
        "description": "<p>Build Python services.</p>",
        "url": "https://example.com/jobs/python-engineer",
        "remote": True,
        "job_types": ["Full-time"],
        "created_at": "2026-07-29T10:00:00Z",
    }

    job = normalize_job(
        raw_job,
        collected_at="2026-07-29T12:00:00+00:00",
    )

    assert job is not None
    assert job.source == "arbeitnow"
    assert job.source_job_id == "python-engineer-example-company"
    assert job.title == "Python Engineer"
    assert job.company == "Example Company"
    assert job.remote_status == "remote"
    assert job.employment_type == "Full-time"


def test_normalize_job_rejects_missing_identifier() -> None:
    raw_job: dict[str, Any] = {
        "title": "Python Engineer",
    }

    job = normalize_job(
        raw_job,
        collected_at="2026-07-29T12:00:00+00:00",
    )

    assert job is None


def test_normalize_job_rejects_missing_title() -> None:
    raw_job: dict[str, Any] = {
        "slug": "missing-title",
    }

    job = normalize_job(
        raw_job,
        collected_at="2026-07-29T12:00:00+00:00",
    )

    assert job is None
