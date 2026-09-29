"""Credential-free Lever Postings API collector."""

from __future__ import annotations

import http.client
import urllib.error
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.collectors.normalization import optional_string
from careeros.source_config import LeverBoard, LeverConfig

PAGE_SIZE = 100
MAX_PAGES_PER_BOARD = 5
MAX_REQUESTS_PER_PROVIDER_RUN = 40
SOURCE_NAME = "lever"
_BASE_URLS = {
    "global": "https://api.lever.co/v0/postings/",
    "eu": "https://api.eu.lever.co/v0/postings/",
}


def source_job_id(*, instance: str, board_id: str, posting_id: str) -> str:
    encoded_board = quote(board_id, safe="", encoding="utf-8", errors="strict")
    encoded_posting = quote(posting_id, safe="", encoding="utf-8", errors="strict")
    return f"{instance}:{encoded_board}:{encoded_posting}"


def _remote_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in {"remote", "fully_remote", "work_from_home"}:
        return "remote"
    if value in {"onsite", "on_site", "on-site", "hybrid"}:
        return "onsite_or_unspecified"
    return None


def normalize_job(
    raw: dict[str, Any], board: LeverBoard, *, collected_at: str
) -> CollectedJob | None:
    posting_id = optional_string(raw.get("id"))
    title = optional_string(raw.get("text"))
    if posting_id is None or title is None:
        return None
    categories = raw.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id(
            instance=board.instance, board_id=board.id, posting_id=posting_id
        ),
        title=title,
        company=board.company,
        location=optional_string(categories.get("location")),
        description=optional_string(raw.get("descriptionPlain")),
        url=optional_string(raw.get("hostedUrl")),
        employment_type=optional_string(categories.get("commitment")),
        remote_status=_remote_status(raw.get("workplaceType")),
        published_at=None,
        collected_at=collected_at,
    )


def _fetch_board(
    board: LeverBoard, quota: int, *, timeout: float, collected_at: str
) -> tuple[list[CollectedJob], int]:
    jobs: list[CollectedJob] = []
    seen: set[str] = set()
    skip = 0
    requests = 0
    for _ in range(MAX_PAGES_PER_BOARD):
        requested = min(PAGE_SIZE, quota - len(jobs))
        if requested <= 0:
            break
        query = urlencode({"mode": "json", "limit": requested, "skip": skip})
        payload = fetch_json(
            f"{_BASE_URLS[board.instance]}{quote(board.id, safe='')}?{query}",
            timeout=timeout,
        )
        requests += 1
        if not isinstance(payload, list) or len(payload) > requested:
            raise RuntimeError("Lever returned an unexpected response array")
        skip += len(payload)
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            normalized = normalize_job(raw, board, collected_at=collected_at)
            if normalized is None or normalized.source_job_id in seen:
                continue
            seen.add(normalized.source_job_id)
            jobs.append(normalized)
            if len(jobs) >= quota:
                break
        if len(jobs) >= quota or not payload or len(payload) < requested:
            break
    return jobs, requests


def fetch_result(
    *, config: LeverConfig, limit: int = 25, timeout: float = DEFAULT_TIMEOUT
) -> CollectorOutcome:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not config.present or not config.enabled or not config.boards:
        raise ValueError("Lever is not enabled or configured")
    base, remainder = divmod(limit, len(config.boards))
    collected_at = datetime.now(UTC).isoformat()
    jobs: list[CollectedJob] = []
    errors: list[str] = []
    requests = 0
    for index, board in enumerate(config.boards):
        quota = base + (1 if index < remainder else 0)
        if quota == 0:
            continue
        try:
            board_jobs, board_requests = _fetch_board(
                board, quota, timeout=timeout, collected_at=collected_at
            )
            requests += board_requests
            if requests > MAX_REQUESTS_PER_PROVIDER_RUN:
                raise RuntimeError("Lever request budget exceeded")
            jobs.extend(board_jobs)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
            RuntimeError,
            http.client.HTTPException,
        ) as error:
            errors.append(
                f"{board.instance}:{board.id}: {type(error).__name__}: {error}"
            )
    return CollectorOutcome(
        tuple(jobs), not errors, "; ".join(errors) if errors else None
    )


def fetch_jobs(
    *, config: LeverConfig, limit: int = 25, timeout: float = DEFAULT_TIMEOUT
) -> list[CollectedJob]:
    outcome = fetch_result(config=config, limit=limit, timeout=timeout)
    if not outcome.succeeded:
        raise RuntimeError(outcome.error or "Lever collection failed")
    return list(outcome.jobs)


__all__ = [
    "MAX_PAGES_PER_BOARD",
    "MAX_REQUESTS_PER_PROVIDER_RUN",
    "PAGE_SIZE",
    "fetch_jobs",
    "fetch_result",
    "normalize_job",
    "source_job_id",
]
