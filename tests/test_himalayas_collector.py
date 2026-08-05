"""Tests for the Himalayas collector."""

import io
import json
from typing import Any

import pytest

from careeros.collectors import himalayas
from careeros.collectors import http as collectors_http
from careeros.collectors.himalayas import MAX_API_LIMIT, normalize_job

_COLLECTED_AT = "2026-08-05T01:00:00+00:00"


def _raw_job(**overrides: Any) -> dict[str, Any]:
    """Build a realistic Himalayas API job object."""
    raw: dict[str, Any] = {
        "guid": "https://himalayas.app/companies/micro1/jobs/talent-operations-manager",
        "title": "Talent Operations Manager",
        "excerpt": "Talent Operations Manager (Remote).",
        "companyName": "micro1",
        "companySlug": "micro1",
        "companyLogo": "https://cdn-images.himalayas.app/91oa56akgt3qjqc1nh3uoruub40c",
        "employmentType": "Full Time",
        "minSalary": 70000,
        "maxSalary": 110000,
        "salaryPeriod": "annual",
        "currency": "USD",
        "locationRestrictions": [],
        "timezoneRestrictions": [-11, -10, -9],
        "categories": ["Talent-Acquisition-Operations-Manager"],
        "parentCategories": ["Human Resources"],
        "description": "<p><strong>Job Title:</strong> Talent Operations Manager</p>",
        "pubDate": 1785888558,
        "expiryDate": 1791072557,
        "applicationLink": (
            "https://himalayas.app/companies/micro1/jobs/talent-operations-manager"
        ),
    }
    raw.update(overrides)
    return raw


def test_normalize_job() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.source == "himalayas"
    assert (
        job.source_job_id
        == "https://himalayas.app/companies/micro1/jobs/talent-operations-manager"
    )
    assert job.title == "Talent Operations Manager"
    assert job.company == "micro1"
    assert job.location is None
    assert job.description == _raw_job()["description"]
    assert (
        job.url
        == "https://himalayas.app/companies/micro1/jobs/talent-operations-manager"
    )
    assert job.employment_type == "Full Time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-08-05T00:09:18+00:00"
    assert job.collected_at == _COLLECTED_AT


def test_normalize_job_rejects_missing_guid() -> None:
    raw = _raw_job()
    del raw["guid"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_normalize_job_rejects_missing_title() -> None:
    raw = _raw_job()
    del raw["title"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_boolean_guid_rejected() -> None:
    raw = _raw_job(guid=True)

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_nullable_fields_handled() -> None:
    raw = _raw_job(
        companyName="",
        companySlug="",
        description="",
        employmentType="",
        locationRestrictions=[],
        pubDate=None,
        applicationLink="",
    )

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.company is None
    assert job.location is None
    assert job.description is None
    assert (
        job.url
        == "https://himalayas.app/companies/micro1/jobs/talent-operations-manager"
    )
    assert job.employment_type is None
    assert job.published_at is None
    assert job.remote_status == "remote"


def test_location_joins_non_blank_values() -> None:
    raw = _raw_job(locationRestrictions=["United States", "Canada"])

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location == "United States, Canada"


def test_location_strips_blank_values() -> None:
    raw = _raw_job(locationRestrictions=["", "United States", "  "])

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location == "United States"


def test_location_none_when_empty_list() -> None:
    raw = _raw_job(locationRestrictions=[])

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location is None


def test_location_none_when_not_a_list() -> None:
    raw = _raw_job(locationRestrictions="United States")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location is None


def test_remote_status() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.remote_status == "remote"


def test_url_falls_back_to_guid_without_application_link() -> None:
    raw = _raw_job(applicationLink="")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert (
        job.url
        == "https://himalayas.app/companies/micro1/jobs/talent-operations-manager"
    )


def test_published_at_converts_unix_timestamp() -> None:
    raw = _raw_job(pubDate=1600000000)

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at == "2020-09-13T12:26:40+00:00"


def test_published_at_boolean_is_none() -> None:
    raw = _raw_job(pubDate=True)

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at is None


def test_published_at_float_is_none() -> None:
    raw = _raw_job(pubDate=1785888558.5)

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at is None


def test_published_at_string_is_none() -> None:
    raw = _raw_job(pubDate="2026-08-05T00:09:18Z")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at is None


class _FakeURLResponse:
    """Stand-in for urlopen's context-manager response."""

    def __init__(self, payload: object) -> None:
        self._buffer = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> io.BytesIO:
        return self._buffer

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_feed(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    requested: list[str] | None = None,
) -> None:
    """Replace urlopen with a stub that returns the given JSON payload."""

    def fake_urlopen(request: object, timeout: float = 30.0) -> _FakeURLResponse:
        if requested is not None:
            requested.append(request.full_url)
        return _FakeURLResponse(payload)

    monkeypatch.setattr(collectors_http, "urlopen", fake_urlopen)


def _envelope(*jobs: object) -> dict[str, object]:
    """Build a Himalayas-style envelope object around job rows."""
    return {
        "comments": "13/03/2026: API updated to add companySlug.",
        "updatedAt": 1785888558,
        "offset": 0,
        "limit": 20,
        "totalCount": len(jobs),
        "jobs": list(jobs),
    }


def test_fetch_sends_bounded_request(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []
    jobs = [_raw_job(guid=f"g-{i}", title=f"Role {i}") for i in range(5)]
    _patch_feed(monkeypatch, _envelope(*jobs), requested)

    himalayas.fetch_jobs(limit=3)

    assert len(requested) == 1
    assert requested[0] == "https://himalayas.app/jobs/api?limit=3"


def test_fetch_collection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [_raw_job(guid=f"g-{i}", title=f"Role {i}") for i in range(5)]
    _patch_feed(monkeypatch, _envelope(*jobs))

    result = himalayas.fetch_jobs(limit=3)

    assert [job.source_job_id for job in result] == ["g-0", "g-1", "g-2"]


def test_fetch_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        himalayas.fetch_jobs(limit=0)


def test_fetch_clamps_limit_above_api_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [_raw_job(guid=f"g-{i}", title=f"Role {i}") for i in range(MAX_API_LIMIT)]
    requested: list[str] = []
    _patch_feed(monkeypatch, _envelope(*jobs), requested)

    result = himalayas.fetch_jobs(limit=MAX_API_LIMIT + 50)

    assert len(result) == MAX_API_LIMIT
    assert requested[0] == f"https://himalayas.app/jobs/api?limit={MAX_API_LIMIT}"


def test_fetch_rejects_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, [{"title": "Wrong shape"}])

    with pytest.raises(RuntimeError):
        himalayas.fetch_jobs(limit=5)


def test_fetch_rejects_missing_job_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, {"totalCount": 1, "jobs": "nope"})

    with pytest.raises(RuntimeError):
        himalayas.fetch_jobs(limit=5)


def test_fetch_empty_result_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _envelope())

    assert himalayas.fetch_jobs(limit=10) == []


def test_fetch_skips_malformed_and_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed: dict[str, Any] = {"title": "No GUID"}
    valid = _raw_job(guid="g-42", title="Valid Role")
    duplicate = _raw_job(guid="g-42", title="Valid Role Again")
    _patch_feed(monkeypatch, _envelope(malformed, valid, duplicate))

    result = himalayas.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "g-42"
    assert result[0].title == "Valid Role"


def test_fetch_skips_non_dict_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _envelope("not-a-dict", _raw_job()))

    result = himalayas.fetch_jobs(limit=10)

    assert len(result) == 1
    assert (
        result[0].source_job_id
        == "https://himalayas.app/companies/micro1/jobs/talent-operations-manager"
    )
