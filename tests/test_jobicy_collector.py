"""Tests for the Jobicy collector."""

import io
import json
from typing import Any

import pytest

from careeros.cli import clean_description
from careeros.collectors import http as collectors_http
from careeros.collectors import jobicy
from careeros.collectors.jobicy import MAX_API_LIMIT, normalize_job

_COLLECTED_AT = "2026-08-02T13:00:00+00:00"

_DESCRIPTION_HTML = "<p>Build <strong>great</strong> things.</p>"


def _raw_job(**overrides: Any) -> dict[str, Any]:
    """Build a realistic Jobicy API job object."""
    raw: dict[str, Any] = {
        "id": 150110,
        "url": "https://jobicy.com/jobs/150110-customer-service-representative-4",
        "jobSlug": "150110-customer-service-representative-4",
        "jobTitle": "Customer Service Representative",
        "companyName": "NTT DATA",
        "companyLogo": "https://jobicy.com/data/logo.png",
        "jobIndustry": ["Customer Support &amp; Success"],
        "jobType": ["Full-Time"],
        "jobGeo": "Canada",
        "jobLevel": "Midweight",
        "jobExcerpt": "Short excerpt.",
        "jobDescription": _DESCRIPTION_HTML,
        "pubDate": "2026-08-02T12:10:14+00:00",
        "salaryMin": 38000,
        "salaryMax": 38000,
        "salaryCurrency": "CAD",
        "salaryPeriod": "yearly",
    }
    raw.update(overrides)
    return raw


def _payload(jobs: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """Build a realistic Jobicy API response payload."""
    payload: dict[str, Any] = {
        "apiVersion": "2.2.15",
        "jobCount": len(jobs),
        "jobs": jobs,
        "statusCode": 200,
        "success": True,
    }
    payload.update(overrides)
    return payload


def test_normalize_job() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.source == "jobicy"
    assert job.source_job_id == "150110"
    assert job.title == "Customer Service Representative"
    assert job.company == "NTT DATA"
    assert job.location == "Canada"
    assert job.description == _DESCRIPTION_HTML
    assert job.url == "https://jobicy.com/jobs/150110-customer-service-representative-4"
    assert job.employment_type == "Full-Time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-08-02T12:10:14+00:00"
    assert job.collected_at == _COLLECTED_AT


def test_normalize_job_rejects_missing_id() -> None:
    raw = _raw_job()
    del raw["id"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_normalize_job_rejects_missing_title() -> None:
    raw = _raw_job()
    del raw["jobTitle"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_nullable_fields_handled() -> None:
    raw = _raw_job(
        companyName="",
        jobGeo="",
        jobDescription="",
        url="",
        jobType=[],
        pubDate="",
    )

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.company is None
    assert job.location is None
    assert job.description is None
    assert job.url is None
    assert job.employment_type is None
    assert job.published_at is None
    assert job.remote_status == "remote"


def test_employment_type_normalization() -> None:
    raw = _raw_job(jobType=["Full-Time", "Contract", "Part-Time"])

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.employment_type == "Full-Time, Contract, Part-Time"


def test_remote_status() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.remote_status == "remote"


def test_description_html_stored_unchanged_and_cleaned() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.description == _DESCRIPTION_HTML
    assert clean_description(job.description) == "Build great things."


class _FakeURLResponse:
    """Stand-in for urlopen's context-manager response."""

    def __init__(self, payload: object) -> None:
        self._buffer = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> io.BytesIO:
        return self._buffer

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_feed(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """Replace urlopen with a stub that returns the given JSON payload."""

    def fake_urlopen(request: object, timeout: float = 30.0) -> _FakeURLResponse:
        return _FakeURLResponse(payload)

    monkeypatch.setattr(collectors_http, "urlopen", fake_urlopen)


def test_fetch_rejects_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, [])

    with pytest.raises(RuntimeError):
        jobicy.fetch_jobs(limit=5)


def test_fetch_rejects_missing_jobs_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, {"apiVersion": "2.2.15"})

    with pytest.raises(RuntimeError):
        jobicy.fetch_jobs(limit=5)


def test_fetch_collection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [_raw_job(id=i, jobTitle=f"Role {i}") for i in range(5)]
    _patch_feed(monkeypatch, _payload(jobs))

    result = jobicy.fetch_jobs(limit=3)

    assert [job.source_job_id for job in result] == ["0", "1", "2"]


def test_fetch_skips_malformed_while_collecting_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed: dict[str, Any] = {"jobTitle": "No ID"}
    valid = _raw_job(id=99, jobTitle="Valid Role")
    _patch_feed(monkeypatch, _payload([malformed, valid]))

    result = jobicy.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "99"
    assert result[0].title == "Valid Role"


def test_fetch_skips_non_dict_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _payload(["not-a-dict", _raw_job()]))

    result = jobicy.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "150110"


def test_fetch_deduplicates_within_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _payload([_raw_job(), _raw_job()]))

    result = jobicy.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "150110"


def test_fetch_empty_jobs_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _payload([], message="No results"))

    assert jobicy.fetch_jobs(limit=10) == []


def test_fetch_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        jobicy.fetch_jobs(limit=0)


def test_fetch_clamps_limit_above_api_max(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [_raw_job(id=i, jobTitle=f"Role {i}") for i in range(MAX_API_LIMIT)]
    requested_urls: list[str] = []

    def fake_urlopen(request: object, timeout: float = 30.0) -> _FakeURLResponse:
        requested_urls.append(request.full_url)
        return _FakeURLResponse(_payload(jobs))

    monkeypatch.setattr(collectors_http, "urlopen", fake_urlopen)

    result = jobicy.fetch_jobs(limit=150)

    assert len(result) == MAX_API_LIMIT
    assert requested_urls == ["https://jobicy.com/api/v2/remote-jobs?count=100"]
