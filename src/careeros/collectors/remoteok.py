"""Collector for the official Remote OK JSON job feed."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob
from careeros.collectors.normalization import optional_string

API_URL = "https://remoteok.com/api"
SOURCE_NAME = "remoteok"

# Employment types appear inside the "tags" list. Keep them in a single named
# mapping so recognized keywords are not scattered through the normalization
# logic.
_EMPLOYMENT_TYPE_TAGS = frozenset(
    {
        "full time",
        "part time",
        "contract",
        "freelance",
        "internship",
        "temporary",
    }
)

# The feed's first element is a metadata/header object (last_updated + legal),
# not a job. Detect it before generic job validation.
_HEADER_KEYS = frozenset({"last_updated", "legal"})


def _is_metadata_header(raw_job: dict[str, Any]) -> bool:
    """Return True for Remote OK's feed metadata/header object."""
    return _HEADER_KEYS.issubset(raw_job.keys())


def _employment_type(value: object) -> str | None:
    """Derive an employment type from Remote OK's job tags."""
    if not isinstance(value, list):
        return None

    recognized: list[str] = []
    seen: set[str] = set()

    for item in value:
        if not isinstance(item, str):
            continue

        normalized = item.strip()
        if not normalized:
            continue

        key = normalized.lower()
        if key in _EMPLOYMENT_TYPE_TAGS and key not in seen:
            seen.add(key)
            recognized.append(normalized)

    return ", ".join(recognized) or None


def _published_at(raw_job: dict[str, Any]) -> str | None:
    """Normalize Remote OK's published timestamp, falling back to its epoch."""
    published = optional_string(raw_job.get("date"))
    if published is not None:
        return published

    epoch = raw_job.get("epoch")
    if isinstance(epoch, int) and not isinstance(epoch, bool):
        try:
            return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    return None


def normalize_job(
    raw_job: dict[str, Any],
    *,
    collected_at: str,
) -> CollectedJob | None:
    """Convert a Remote OK feed object into a CareerOS job."""
    source_job_id = optional_string(raw_job.get("id"))
    title = optional_string(raw_job.get("position"))

    if source_job_id is None or title is None:
        return None

    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=optional_string(raw_job.get("company")),
        location=optional_string(raw_job.get("location")),
        description=optional_string(raw_job.get("description")),
        url=optional_string(raw_job.get("url"))
        or optional_string(raw_job.get("apply_url")),
        employment_type=_employment_type(raw_job.get("tags")),
        remote_status="remote",
        published_at=_published_at(raw_job),
        collected_at=collected_at,
    )


def fetch_jobs(
    *,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    """Fetch and normalize jobs from the Remote OK feed."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    payload = fetch_json(API_URL, timeout=timeout)

    if not isinstance(payload, list):
        raise RuntimeError("Remote OK returned an unexpected response.")

    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []

    for raw_job in payload:
        if not isinstance(raw_job, dict):
            continue

        if _is_metadata_header(raw_job):
            continue

        normalized = normalize_job(raw_job, collected_at=collected_at)
        if normalized is not None:
            jobs.append(normalized)

        if len(jobs) >= limit:
            break

    return jobs
