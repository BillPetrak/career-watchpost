"""Collector for the public Arbeitnow job-board API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob

API_URL = "https://www.arbeitnow.com/api/job-board-api"
SOURCE_NAME = "arbeitnow"


def _optional_string(value: object) -> str | None:
    """Return a stripped string or None."""
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    return stripped or None


def _employment_type(value: object) -> str | None:
    """Normalize Arbeitnow's list of job types."""
    if not isinstance(value, list):
        return None

    job_types = [
        item.strip() for item in value if isinstance(item, str) and item.strip()
    ]
    return ", ".join(job_types) or None


def _remote_status(value: object) -> str | None:
    """Convert the remote flag into a readable status."""
    if value is True:
        return "remote"
    if value is False:
        return "onsite_or_unspecified"
    return None


def normalize_job(
    raw_job: dict[str, Any],
    *,
    collected_at: str,
) -> CollectedJob | None:
    """Convert an Arbeitnow API object into a CareerOS job."""
    source_job_id = _optional_string(raw_job.get("slug"))
    title = _optional_string(raw_job.get("title"))

    if source_job_id is None or title is None:
        return None

    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=_optional_string(raw_job.get("company_name")),
        location=_optional_string(raw_job.get("location")),
        description=_optional_string(raw_job.get("description")),
        url=_optional_string(raw_job.get("url")),
        employment_type=_employment_type(raw_job.get("job_types")),
        remote_status=_remote_status(raw_job.get("remote")),
        published_at=_optional_string(raw_job.get("created_at")),
        collected_at=collected_at,
    )


def fetch_jobs(
    *,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    """Fetch and normalize jobs from Arbeitnow."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    payload = fetch_json(API_URL, timeout=timeout)

    if not isinstance(payload, dict):
        raise RuntimeError("Arbeitnow returned an unexpected response.")

    raw_jobs = payload.get("data")
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Arbeitnow response does not contain a job list.")

    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []

    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue

        normalized = normalize_job(raw_job, collected_at=collected_at)
        if normalized is not None:
            jobs.append(normalized)

        if len(jobs) >= limit:
            break

    return jobs
