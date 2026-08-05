"""Collector for the official Himalayas remote-jobs JSON API.

Himalayas publishes a free public browse endpoint (``GET
https://himalayas.app/jobs/api``) that needs no API key or authentication
(see https://himalayas.app/docs/remote-jobs-api). The endpoint accepts
``offset``/``limit`` pagination; the server clamps ``limit`` to a maximum of
20 jobs per request. The feed is cached and refreshed every 24 hours, so the
collector requests a single first page and CareerOS enforces a documented
24-hour polling floor. The API is rate limited: excessive volume returns HTTP
429, which the collection pipeline records as a per-source failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob
from careeros.collectors.normalization import optional_string

API_URL = "https://himalayas.app/jobs/api"
SOURCE_NAME = "himalayas"

# The browse endpoint returns a maximum of 20 jobs per request (verified
# server-side); larger limits are silently clamped rather than paginated.
MAX_API_LIMIT = 20

_JOB_LIST_KEY = "jobs"


def _source_job_id(value: object) -> str | None:
    """Return a stable identifier from Himalayas' canonical job permalink."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _location(value: object) -> str | None:
    """Join Himalayas' country-restriction list into a single string."""
    if not isinstance(value, list):
        return None

    parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return ", ".join(parts) or None


def _published_at(value: object) -> str | None:
    """Convert Himalayas' unix-timestamp publication date into ISO-8601 UTC."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    return None


def _listing_url(raw_job: dict[str, Any]) -> str | None:
    """Return the canonical Himalayas listing URL.

    The API exposes both ``applicationLink`` and ``guid``; the former is the
    apply URL and is used directly, falling back to the permalink when absent.
    """
    return optional_string(raw_job.get("applicationLink")) or optional_string(
        raw_job.get("guid")
    )


def normalize_job(
    raw_job: dict[str, Any],
    *,
    collected_at: str,
) -> CollectedJob | None:
    """Convert a Himalayas API object into a CareerOS job."""
    source_job_id = _source_job_id(raw_job.get("guid"))
    title = optional_string(raw_job.get("title"))

    if source_job_id is None or title is None:
        return None

    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=optional_string(raw_job.get("companyName")),
        location=_location(raw_job.get("locationRestrictions")),
        description=optional_string(raw_job.get("description")),
        url=_listing_url(raw_job),
        employment_type=optional_string(raw_job.get("employmentType")),
        remote_status="remote",
        published_at=_published_at(raw_job.get("pubDate")),
        collected_at=collected_at,
    )


def fetch_jobs(
    *,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    """Fetch and normalize jobs from the Himalayas API."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    count = min(limit, MAX_API_LIMIT)
    payload = fetch_json(f"{API_URL}?limit={count}", timeout=timeout)

    if not isinstance(payload, dict):
        raise RuntimeError("Himalayas returned an unexpected response.")

    raw_jobs = payload.get(_JOB_LIST_KEY)
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Himalayas response does not contain a job list.")

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
