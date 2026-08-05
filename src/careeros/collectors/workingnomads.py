"""Collector for the Working Nomads job search endpoint.

Working Nomads does not publish a documented public API. This collector uses
the same internal Elasticsearch ``_search`` route that powers the public job
listing pages (``POST https://www.workingnomads.com/jobsapi/_search``). The
endpoint is UNDOCUMENTED and may change without notice; the collector sends
only a minimal query and treats any unexpected response shape as a hard
failure rather than a silently empty result. Working Nomads is a remote-only
job board, so every collected job is marked remote.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json_post
from careeros.collectors.models import CollectedJob
from careeros.collectors.normalization import optional_string

API_URL = "https://www.workingnomads.com/jobsapi/_search"
SOURCE_NAME = "workingnomads"

# The endpoint accepts Elasticsearch from/size offset pagination. Requests are
# bounded to a single page whose size never exceeds this cap (verified
# server-side); larger limits are clamped rather than paginated.
MAX_API_LIMIT = 1000

# Working Nomads encodes the employment type as a terse position code. Keep the
# mapping in one named table so recognized codes are not scattered through the
# normalization logic; unrecognized or missing codes are dropped.
_POSITION_TYPE_MAP = {
    "ft": "full time",
    "pt": "part time",
    "fr": "freelance",
}


def _source_job_id(value: object) -> str | None:
    """Return a stable identifier from Working Nomads' numeric job ID field."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _employment_type(value: object) -> str | None:
    """Translate Working Nomads' position code into a readable value."""
    if not isinstance(value, str):
        return None
    return _POSITION_TYPE_MAP.get(value.strip().lower())


def _location(value: object) -> str | None:
    """Join Working Nomads' location list into a single readable string."""
    if not isinstance(value, list):
        return None

    parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return ", ".join(parts) or None


def _listing_url(raw_job: dict[str, Any]) -> str | None:
    """Return the canonical Working Nomads listing URL.

    The API only exposes an external ``apply_url``; the canonical listing URL
    on the source site is constructed from the slug. ``apply_url`` is kept as
    a fallback when no usable slug is available.
    """
    slug = optional_string(raw_job.get("slug"))
    if slug is not None:
        return f"https://www.workingnomads.com/jobs/{slug}"
    return optional_string(raw_job.get("apply_url"))


def normalize_job(
    raw_job: dict[str, Any],
    *,
    collected_at: str,
) -> CollectedJob | None:
    """Convert a Working Nomads API object into a CareerOS job."""
    source_job_id = _source_job_id(raw_job.get("id"))
    title = optional_string(raw_job.get("title"))

    if source_job_id is None or title is None:
        return None

    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=optional_string(raw_job.get("company")),
        location=_location(raw_job.get("locations")),
        description=optional_string(raw_job.get("description")),
        url=_listing_url(raw_job),
        employment_type=_employment_type(raw_job.get("position_type")),
        remote_status="remote",
        published_at=optional_string(raw_job.get("pub_date")),
        collected_at=collected_at,
    )


def fetch_jobs(
    *,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    """Fetch and normalize jobs from Working Nomads."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    size = min(limit, MAX_API_LIMIT)
    payload = fetch_json_post(
        API_URL,
        {"from": 0, "size": size, "sort": [{"id": "desc"}]},
        timeout=timeout,
    )

    if not isinstance(payload, dict):
        raise RuntimeError("Working Nomads returned an unexpected response.")

    hits = payload.get("hits")
    if not isinstance(hits, dict):
        raise RuntimeError("Working Nomads response does not contain hits.")

    raw_jobs = hits.get("hits")
    if not isinstance(raw_jobs, list):
        raise RuntimeError("Working Nomads response does not contain a job list.")

    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []
    seen: set[str] = set()

    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue

        source = raw_job.get("_source")
        if not isinstance(source, dict):
            continue

        normalized = normalize_job(source, collected_at=collected_at)
        if normalized is None:
            continue

        if normalized.source_job_id in seen:
            continue

        seen.add(normalized.source_job_id)
        jobs.append(normalized)

        if len(jobs) >= limit:
            break

    return jobs
