"""Credential-free SmartRecruiters public Posting API collector."""

from __future__ import annotations

import http.client
import time
import urllib.error
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from careeros.collectors.http import DEFAULT_TIMEOUT, fetch_json
from careeros.collectors.models import CollectedJob, CollectorOutcome
from careeros.collectors.normalization import optional_string
from careeros.source_config import SmartRecruitersCompany, SmartRecruitersConfig

SOURCE_NAME = "smartrecruiters"
MAX_RETAINED_JOBS_PER_PROVIDER_RUN = 100
MAX_REQUESTS_PER_PROVIDER_RUN = 108
MIN_REQUEST_START_INTERVAL_SECONDS = 0.125
_BASE_URL = "https://api.smartrecruiters.com/v1/companies/"


def _encoded(value: str) -> str:
    return quote(value, safe="", encoding="utf-8", errors="strict")


def source_job_id(*, identifier: str, posting_uuid: str) -> str:
    """Return the company-scoped canonical SmartRecruiters identity."""
    return f"{_encoded(identifier)}:{_encoded(posting_uuid)}"


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def _aware_utc(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).isoformat()


def _location(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = [
        item.strip()
        for key in ("city", "region", "country")
        if isinstance(item := value.get(key), str) and item.strip()
    ]
    return ", ".join(parts) or None


def _remote_status(value: dict[str, Any]) -> str | None:
    if value.get("hybrid") is True:
        return "onsite_or_unspecified"
    if value.get("remote") is True:
        return "remote"
    if value.get("remote") is False:
        return "onsite_or_unspecified"
    return None


def normalize_job(
    raw: object,
    *,
    identifier: str,
    selected_uuid: str,
    collected_at: str,
) -> CollectedJob:
    """Normalize one authoritative SmartRecruiters PostingDetails object."""
    if not isinstance(raw, dict):
        raise RuntimeError("SmartRecruiters detail response has unexpected shape")
    detail_uuid = _canonical_uuid(raw.get("uuid"))
    if detail_uuid is None or detail_uuid != selected_uuid:
        raise RuntimeError("SmartRecruiters detail UUID does not match list UUID")
    title = optional_string(raw.get("name"))
    company = raw.get("company")
    if not isinstance(company, dict):
        raise RuntimeError("SmartRecruiters detail company is missing or invalid")
    company_name = optional_string(company.get("name"))
    company_identifier = optional_string(company.get("identifier"))
    if company_name is None or company_identifier is None:
        raise RuntimeError("SmartRecruiters detail company authority is missing")
    if company_identifier != identifier:
        raise RuntimeError("SmartRecruiters detail company identifier mismatch")
    if title is None:
        raise RuntimeError("SmartRecruiters detail name is missing or empty")

    job_ad = raw.get("jobAd")
    job_ad = job_ad if isinstance(job_ad, dict) else {}
    sections = job_ad.get("sections")
    sections = sections if isinstance(sections, dict) else {}
    description_section = sections.get("jobDescription")
    description_section = (
        description_section if isinstance(description_section, dict) else {}
    )
    employment = raw.get("typeOfEmployment")
    employment = employment if isinstance(employment, dict) else {}
    return CollectedJob(
        source=SOURCE_NAME,
        source_job_id=source_job_id(identifier=identifier, posting_uuid=selected_uuid),
        title=title,
        company=company_name,
        location=_location(raw.get("location")),
        description=optional_string(description_section.get("text")),
        url=optional_string(raw.get("postingUrl")),
        employment_type=optional_string(employment.get("label")),
        remote_status=_remote_status(raw),
        published_at=_aware_utc(raw.get("releasedDate")),
        collected_at=collected_at,
    )


def _list_url(identifier: str, page_size: int) -> str:
    encoded_identifier = _encoded(identifier)
    query = urlencode({"destination": "PUBLIC", "limit": page_size, "offset": 0})
    return f"{_BASE_URL}{encoded_identifier}/postings?{query}"


def _detail_url(identifier: str, posting_uuid: str) -> str:
    return f"{_BASE_URL}{_encoded(identifier)}/postings/{_encoded(posting_uuid)}"


class _RequestPacer:
    def __init__(
        self,
        *,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
        interval: float = MIN_REQUEST_START_INTERVAL_SECONDS,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._interval = interval
        self._last_start: float | None = None

    def before_request(self) -> None:
        now = self._clock()
        if self._last_start is not None:
            delay = self._last_start + self._interval - now
            if delay > 0:
                self._sleeper(delay)
        self._last_start = self._clock()


class _RequestBudget:
    def __init__(self) -> None:
        self.count = 0

    def before_request(self) -> None:
        if self.count >= MAX_REQUESTS_PER_PROVIDER_RUN:
            raise RuntimeError("SmartRecruiters request budget exceeded")
        self.count += 1


def _request(
    url: str,
    *,
    timeout: float,
    pacer: _RequestPacer,
    budget: _RequestBudget,
) -> object:
    budget.before_request()
    pacer.before_request()
    return fetch_json(url, timeout=timeout)


def _validate_list(payload: object, requested: int) -> list[object]:
    if not isinstance(payload, dict):
        raise RuntimeError("SmartRecruiters list response has unexpected shape")
    for key in ("limit", "offset", "totalFound"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"SmartRecruiters list {key} is invalid")
    content = payload.get("content")
    if not isinstance(content, list):
        raise RuntimeError("SmartRecruiters list content is invalid")
    if payload["offset"] != 0:
        raise RuntimeError("SmartRecruiters list offset must be zero")
    if len(content) > requested:
        raise RuntimeError("SmartRecruiters list page exceeds requested limit")
    if payload["totalFound"] < len(content):
        raise RuntimeError("SmartRecruiters list totalFound is too small")
    return content


def _fetch_company(
    company: SmartRecruitersCompany,
    quota: int,
    *,
    timeout: float,
    collected_at: str,
    pacer: _RequestPacer,
    budget: _RequestBudget,
) -> list[CollectedJob]:
    payload = _request(
        _list_url(company.identifier, quota),
        timeout=timeout,
        pacer=pacer,
        budget=budget,
    )
    rows = _validate_list(payload, quota)
    jobs: list[CollectedJob] = []
    seen: set[str] = set()
    for row in rows:
        selected_uuid = (
            _canonical_uuid(row.get("uuid")) if isinstance(row, dict) else None
        )
        if selected_uuid is None or selected_uuid in seen:
            continue
        seen.add(selected_uuid)
        detail = _request(
            _detail_url(company.identifier, selected_uuid),
            timeout=timeout,
            pacer=pacer,
            budget=budget,
        )
        jobs.append(
            normalize_job(
                detail,
                identifier=company.identifier,
                selected_uuid=selected_uuid,
                collected_at=collected_at,
            )
        )
        if len(jobs) >= quota:
            break
    return jobs


def fetch_result(
    *,
    config: SmartRecruitersConfig,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> CollectorOutcome:
    """Collect configured companies with fixed quotas and company isolation."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if not config.present or not config.enabled or not config.companies:
        raise ValueError("SmartRecruiters is not enabled or configured")
    effective_limit = min(limit, MAX_RETAINED_JOBS_PER_PROVIDER_RUN)
    base, remainder = divmod(effective_limit, len(config.companies))
    collected_at = datetime.now(UTC).isoformat()
    pacer = _RequestPacer(
        clock=time.monotonic if clock is None else clock,
        sleeper=time.sleep if sleeper is None else sleeper,
    )
    budget = _RequestBudget()
    jobs: list[CollectedJob] = []
    errors: list[str] = []
    for index, company in enumerate(config.companies):
        quota = base + (1 if index < remainder else 0)
        if quota == 0:
            continue
        try:
            jobs.extend(
                _fetch_company(
                    company,
                    quota,
                    timeout=timeout,
                    collected_at=collected_at,
                    pacer=pacer,
                    budget=budget,
                )
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
            RuntimeError,
            http.client.HTTPException,
        ) as error:
            errors.append(f"{company.identifier}: {type(error).__name__}: {error}")
    return CollectorOutcome(tuple(jobs), not errors, "; ".join(errors) or None)


def fetch_jobs(
    *,
    config: SmartRecruitersConfig,
    limit: int = 25,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CollectedJob]:
    outcome = fetch_result(config=config, limit=limit, timeout=timeout)
    if not outcome.succeeded:
        raise RuntimeError(outcome.error or "SmartRecruiters collection failed")
    return list(outcome.jobs)


__all__ = [
    "MAX_REQUESTS_PER_PROVIDER_RUN",
    "MAX_RETAINED_JOBS_PER_PROVIDER_RUN",
    "MIN_REQUEST_START_INTERVAL_SECONDS",
    "fetch_jobs",
    "fetch_result",
    "normalize_job",
    "source_job_id",
]
