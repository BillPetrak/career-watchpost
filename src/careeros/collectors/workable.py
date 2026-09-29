"""Credential-free Workable public account jobs collector."""

from __future__ import annotations

import http.client
import urllib.error
from datetime import UTC, date, datetime
from urllib.parse import quote

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.source_config import WorkableBoard, WorkableConfig

SOURCE_NAME = "workable"
MAX_REQUESTS_PER_PROVIDER_RUN = 8
_BASE_URL = "https://www.workable.com/api/accounts/"


def source_job_id(*, subdomain: str, shortcode: str) -> str:
    """Return the durable account-scoped identity for a Workable job."""
    encoded_subdomain = quote(subdomain, safe="", encoding="utf-8", errors="strict")
    encoded_shortcode = quote(shortcode, safe="", encoding="utf-8", errors="strict")
    return f"{encoded_subdomain}:{encoded_shortcode}"


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _published_at(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if value == parsed.isoformat() else None


def _location(raw: dict[object, object]) -> str | None:
    parts = [
        value.strip()
        for key in ("city", "state", "country")
        if isinstance(value := raw.get(key), str) and value.strip()
    ]
    return ", ".join(parts) or None


def _remote_status(raw: dict[object, object]) -> str | None:
    workplace_type = raw.get("workplace_type")
    if workplace_type == "remote":
        return "remote"
    if workplace_type in {"on_site", "hybrid"}:
        return "onsite_or_unspecified"
    return "remote" if raw.get("telecommuting") is True else None


def normalize_job(
    raw: object,
    *,
    subdomain: str,
    company: str,
    collected_at: str,
) -> CollectedJob | None:
    """Normalize one documented public Workable job row."""
    if not isinstance(raw, dict):
        return None
    shortcode = _optional_string(raw.get("shortcode"))
    title = _optional_string(raw.get("title"))
    if shortcode is None or title is None:
        return None
    application_url = _optional_string(raw.get("application_url"))
    shortlink = _optional_string(raw.get("shortlink"))
    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id(subdomain=subdomain, shortcode=shortcode),
        title=title,
        company=company,
        location=_location(raw),
        description=_optional_string(raw.get("description")),
        url=application_url or shortlink,
        employment_type=_optional_string(raw.get("employment_type")),
        remote_status=_remote_status(raw),
        published_at=_published_at(raw.get("published_on")),
        collected_at=collected_at,
    )


def _account_url(subdomain: str) -> str:
    return (
        f"{_BASE_URL}{quote(subdomain, safe='', encoding='utf-8', errors='strict')}"
        "?details=true"
    )


def _fetch_account(
    board: WorkableBoard,
    quota: int,
    *,
    timeout: float,
    collected_at: str,
) -> list[CollectedJob]:
    payload = fetch_json(_account_url(board.subdomain), timeout=timeout)
    if not isinstance(payload, dict):
        raise RuntimeError("Workable account response has unexpected shape")
    company = _optional_string(payload.get("name"))
    if company is None:
        raise RuntimeError("Workable account name is missing or empty")
    rows = payload.get("jobs")
    if not isinstance(rows, list):
        raise RuntimeError("Workable account jobs response has unexpected shape")
    jobs: list[CollectedJob] = []
    seen: set[str] = set()
    for raw in rows:
        normalized = normalize_job(
            raw,
            subdomain=board.subdomain,
            company=company,
            collected_at=collected_at,
        )
        if normalized is None or normalized.source_job_id in seen:
            continue
        seen.add(normalized.source_job_id)
        jobs.append(normalized)
        if len(jobs) >= quota:
            break
    return jobs


def fetch_result(
    *, config: WorkableConfig, limit: int = 25, timeout: float = DEFAULT_TIMEOUT
) -> CollectorOutcome:
    """Collect configured accounts with fixed quotas and account isolation."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not config.present or not config.enabled or not config.boards:
        raise ValueError("Workable is not enabled or configured")
    base, remainder = divmod(limit, len(config.boards))
    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []
    errors: list[str] = []
    for index, board in enumerate(config.boards):
        quota = base + (1 if index < remainder else 0)
        if quota == 0:
            continue
        try:
            jobs.extend(
                _fetch_account(board, quota, timeout=timeout, collected_at=collected_at)
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
            RuntimeError,
            http.client.HTTPException,
        ) as error:
            errors.append(f"{board.subdomain}: {type(error).__name__}: {error}")
    return CollectorOutcome(
        tuple(jobs), not errors, "; ".join(errors) if errors else None
    )


def fetch_jobs(
    *, config: WorkableConfig, limit: int = 25, timeout: float = DEFAULT_TIMEOUT
) -> list[CollectedJob]:
    """Compatibility wrapper used by direct collector callers."""
    outcome = fetch_result(config=config, limit=limit, timeout=timeout)
    if not outcome.succeeded:
        raise RuntimeError(outcome.error or "Workable collection failed")
    return list(outcome.jobs)


__all__ = [
    "MAX_REQUESTS_PER_PROVIDER_RUN",
    "fetch_jobs",
    "fetch_result",
    "normalize_job",
    "source_job_id",
]
