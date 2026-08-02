"""Collector for the We Work Remotely public RSS job feed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_bytes
from careeros.collectors.models import CollectedJob

API_URL = "https://weworkremotely.com/remote-jobs.rss"
SOURCE_NAME = "weworkremotely"

_TITLE_SEPARATOR = ": "


def _text(item: ET.Element, tag: str) -> str | None:
    """Return the stripped text of a named child element, or None."""
    value = item.findtext(tag)
    if value is None:
        return None

    stripped = value.strip()
    return stripped or None


def _split_title(title: str) -> tuple[str | None, str]:
    """Split a "Company: Role" title on the first ": " separator.

    Any additional colons in the role text are preserved. When the separator
    is absent, the full title is retained and company is None.
    """
    company, separator, role = title.partition(_TITLE_SEPARATOR)
    if separator and role.strip():
        return company.strip() or None, role.strip()
    return None, title.strip()


def _employment_type(value: str | None) -> str | None:
    """Normalize We Work Remotely's single employment-type value."""
    if value is None:
        return None

    normalized = value.strip().lower().replace("-", " ").replace("_", " ")
    collapsed = " ".join(normalized.split())
    return collapsed or None


def _published_at(value: str | None) -> str | None:
    """Normalize an RFC 822 publication timestamp to ISO 8601."""
    if value is None:
        return None

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _location(item: ET.Element) -> str | None:
    """Return the first non-empty of region, state, or country."""
    for tag in ("region", "state", "country"):
        value = _text(item, tag)
        if value is not None:
            return value
    return None


def normalize_job(
    item: ET.Element,
    *,
    collected_at: str,
) -> CollectedJob | None:
    """Convert a We Work Remotely RSS item into a CareerOS job."""
    source_job_id = _text(item, "guid") or _text(item, "link")
    title_text = _text(item, "title")

    if source_job_id is None or title_text is None:
        return None

    company, title = _split_title(title_text)

    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id,
        title=title,
        company=company,
        location=_location(item),
        description=_text(item, "description"),
        url=_text(item, "link"),
        employment_type=_employment_type(_text(item, "type")),
        remote_status="remote",
        published_at=_published_at(_text(item, "pubDate")),
        collected_at=collected_at,
    )


def fetch_jobs(
    *,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    """Fetch and normalize jobs from the We Work Remotely RSS feed."""
    if limit < 1:
        raise ValueError("limit must be at least 1")

    payload = fetch_bytes(API_URL, timeout=timeout, accept="application/rss+xml")

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise RuntimeError(
            "We Work Remotely returned an unexpected response."
        ) from error

    if root.tag.split("}")[-1] != "rss":
        raise RuntimeError("We Work Remotely returned an unexpected response.")

    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("We Work Remotely returned an unexpected response.")

    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []
    seen: set[str] = set()

    for item in channel.findall("item"):
        normalized = normalize_job(item, collected_at=collected_at)
        if normalized is None:
            continue

        if normalized.source_job_id in seen:
            continue

        seen.add(normalized.source_job_id)
        jobs.append(normalized)

        if len(jobs) >= limit:
            break

    return jobs
