"""Collector for the official Remotive remote-jobs JSON API.

Usage and attribution notes from Remotive's API terms: attribute jobs to
Remotive with a direct link back to the original listing, do not republish
the feed to third-party job boards, and poll no more than four times a day.
Listings are published with an intentional 24-hour delay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob
from careeros.collectors.normalization import optional_string

API_URL = "https://remotive.com/api/remote-jobs"
SOURCE_NAME = "remotive"

# The response is an envelope object: its non-job keys are usage notices
# ("00-warning", "0-legal-notice") and counts ("job-count",
# "total-job-count"), while the actual listings live under "jobs".
_JOB_LIST_KEY = "jobs"


def _source_job_id(value: object) -> str | None:
    """Return a stable identifier from Remotive's numeric job ID field."""
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
    """Convert a Remotive API object into a CareerOS job."""
    source_job_id = _source_job_id(raw_job.get("id"))
    title = optional_string(raw_job.get("title"))

    if source_job_id is None or title is None:
        return None

    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=optional_string(raw_job.get("company_name")),
        location=optional_string(raw_job.get("candidate_required_location")),
        description=optional_string(raw_job.get("description")),
        url=optional_string(raw_job.get("url")),
        employment_type=optional_string(raw_job.get("job_type")),
        remote_status="remote",
        published_at=optional_string(raw_job.get("publication_date")),
        collected_at=collected_at,
    )


def fetch_jobs(
    *,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    """Fetch and normalize jobs from the Remotive API."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    payload = fetch_json(API_URL, timeout=timeout)

    if not isinstance(payload, dict):
        raise RuntimeError("Remotive returned an unexpected response.")

    raw_jobs = payload.get(_JOB_LIST_KEY)
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Remotive response does not contain a job list.")

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
