"""Collector for the official Jobicy remote-jobs JSON API.

Usage and attribution notes from Jobicy's fair-use terms: credit Jobicy with
a direct link to the source, route applications to the original job URL, do
not redistribute listings to competing aggregators, and poll no more than
once per hour. Listings are published with an intentional delay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob
from careeros.collectors.normalization import employment_type, optional_string

API_URL = "https://jobicy.com/api/v2/remote-jobs"
SOURCE_NAME = "jobicy"

# Jobicy documents ``count`` values through 200. The API exposes the latest
# listings without pagination, so one request is deliberately capped there.
MAX_API_LIMIT = 200


def _source_job_id(value: object) -> str | None:
    """Return a stable identifier from Jobicy's numeric job ID field."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def normalize_job(
    raw_job: dict[str, Any],
    *,
    collected_at: str,
) -> CollectedJob | None:
    """Convert a Jobicy API object into a CareerOS job."""
    source_job_id = _source_job_id(raw_job.get("id"))
    title = optional_string(raw_job.get("jobTitle"))

    if source_job_id is None or title is None:
        return None

    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=optional_string(raw_job.get("companyName")),
        location=optional_string(raw_job.get("jobGeo")),
        description=optional_string(raw_job.get("jobDescription")),
        url=optional_string(raw_job.get("url")),
        employment_type=employment_type(raw_job.get("jobType")),
        remote_status="remote",
        published_at=optional_string(raw_job.get("pubDate")),
        collected_at=collected_at,
    )


def fetch_jobs(
    *,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    """Fetch and normalize jobs from the Jobicy API."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    count = min(limit, MAX_API_LIMIT)
    payload = fetch_json(f"{API_URL}?count={count}", timeout=timeout)

    if not isinstance(payload, dict):
        raise RuntimeError("Jobicy returned an unexpected response.")

    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Jobicy response does not contain a job list.")

    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []
    seen: set[str] = set()

    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue

        normalized = normalize_job(raw_job, collected_at=collected_at)
        if normalized is None:
            continue

        if normalized.source_job_id in seen:
            continue

        seen.add(normalized.source_job_id)
        jobs.append(normalized)

        if len(jobs) >= limit:
            break

    return jobs
