"""Credential-free Greenhouse Job Board API collector."""

from __future__ import annotations

import http.client
import urllib.error
from datetime import UTC, datetime
from urllib.parse import quote

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.source_config import GreenhouseBoard, GreenhouseConfig

SOURCE_NAME = "greenhouse"
MAX_REQUESTS_PER_PROVIDER_RUN = 16
_BASE_URL = "https://boards-api.greenhouse.io/v1/boards/"


def source_job_id(*, board_token: str, job_id: int) -> str:
    """Return the durable, board-scoped identity for a Greenhouse post."""
    encoded_token = quote(board_token, safe="", encoding="utf-8", errors="strict")
    return f"{encoded_token}:{job_id:d}"


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def normalize_job(
    raw: object,
    *,
    board_token: str,
    company: str,
    collected_at: str,
) -> CollectedJob | None:
    """Normalize one Greenhouse public job row.

    Greenhouse's public numeric ``id`` is the only job identity input.  Rows
    that do not meet the required identity/title contract are ignored; the
    containing board operation remains valid.
    """
    if not isinstance(raw, dict):
        return None
    raw_id = raw.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
        return None
    title = _optional_string(raw.get("title"))
    if title is None:
        return None
    location_value = raw.get("location")
    location = (
        _optional_string(location_value.get("name"))
        if isinstance(location_value, dict)
        else None
    )
    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id(board_token=board_token, job_id=raw_id),
        title=title,
        company=company,
        location=location,
        description=_optional_string(raw.get("content")),
        url=_optional_string(raw.get("absolute_url")),
        employment_type=None,
        remote_status=None,
        published_at=None,
        collected_at=collected_at,
    )


def _board_url(token: str, suffix: str = "") -> str:
    return (
        f"{_BASE_URL}{quote(token, safe='', encoding='utf-8', errors='strict')}{suffix}"
    )


def _fetch_board(
    board: GreenhouseBoard,
    quota: int,
    *,
    timeout: float,
    collected_at: str,
) -> list[CollectedJob]:
    """Fetch and normalize one board as one atomic board-local operation."""
    token = board.token
    metadata = fetch_json(_board_url(token), timeout=timeout)
    if not isinstance(metadata, dict):
        raise RuntimeError("Greenhouse board metadata has unexpected shape")
    company = _optional_string(metadata.get("name"))
    if company is None:
        raise RuntimeError("Greenhouse board metadata name is missing or empty")

    payload = fetch_json(_board_url(token, "/jobs?content=true"), timeout=timeout)
    if not isinstance(payload, dict):
        raise RuntimeError("Greenhouse jobs response has unexpected shape")
    rows = payload.get("jobs")
    meta = payload.get("meta")
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise RuntimeError("Greenhouse jobs response has unexpected shape")
    total = meta.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise RuntimeError("Greenhouse jobs response has invalid meta.total")
    if total != len(rows):
        raise RuntimeError("Greenhouse jobs response meta.total mismatch")

    jobs: list[CollectedJob] = []
    seen_ids: set[int] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id <= 0:
            continue
        if raw_id in seen_ids:
            continue
        normalized = normalize_job(
            raw,
            board_token=token,
            company=company,
            collected_at=collected_at,
        )
        if normalized is not None:
            seen_ids.add(raw_id)
            jobs.append(normalized)
            if len(jobs) >= quota:
                break
    return jobs


def fetch_result(
    *, config: GreenhouseConfig, limit: int = 25, timeout: float = DEFAULT_TIMEOUT
) -> CollectorOutcome:
    """Collect configured boards with fixed divmod quotas and isolation."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not config.present or not config.enabled or not config.boards:
        raise ValueError("Greenhouse is not enabled or configured")

    base, remainder = divmod(limit, len(config.boards))
    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []
    errors: list[str] = []
    for index, board in enumerate(config.boards):
        quota = base + (1 if index < remainder else 0)
        if quota == 0:
            continue
        token = board.token
        try:
            jobs.extend(
                _fetch_board(board, quota, timeout=timeout, collected_at=collected_at)
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
            RuntimeError,
            http.client.HTTPException,
        ) as error:
            errors.append(f"{token}: {type(error).__name__}: {error}")
    return CollectorOutcome(
        tuple(jobs), not errors, "; ".join(errors) if errors else None
    )


def fetch_jobs(
    *, config: GreenhouseConfig, limit: int = 25, timeout: float = DEFAULT_TIMEOUT
) -> list[CollectedJob]:
    """Compatibility wrapper used by direct collector callers."""
    outcome = fetch_result(config=config, limit=limit, timeout=timeout)
    if not outcome.succeeded:
        raise RuntimeError(outcome.error or "Greenhouse collection failed")
    return list(outcome.jobs)


__all__ = [
    "MAX_REQUESTS_PER_PROVIDER_RUN",
    "fetch_jobs",
    "fetch_result",
    "normalize_job",
    "source_job_id",
]
