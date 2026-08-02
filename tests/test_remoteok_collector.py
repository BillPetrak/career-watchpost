"""Tests for the Remote OK collector."""

import io
import json
from typing import Any

import pytest

from careeros.collectors import http as collectors_http
from careeros.collectors import remoteok
from careeros.collectors.remoteok import normalize_job

_COLLECTED_AT = "2026-08-01T17:00:00+00:00"


def _raw_job(**overrides: Any) -> dict[str, Any]:
    """Build a realistic Remote OK feed job object."""
    raw: dict[str, Any] = {
        "id": "1135789",
        "slug": "remote-patient-outreach-specialist-grapefruit-health-1135789",
        "epoch": 1785600013,
        "date": "2026-08-01T16:00:13+00:00",
        "company": "Grapefruit Health",
        "position": "Patient Outreach Specialist",
        "tags": ["hr", "Full Time"],
        "description": "&lt;p&gt;Build services.&lt;/p&gt;",
        "location": "Worldwide",
        "apply_url": "https://remoteok.com/remote-jobs/remote-patient-outreach-specialist-grapefruit-health-1135789",
        "url": "https://remoteok.com/remote-jobs/remote-patient-outreach-specialist-grapefruit-health-1135789",
        "verified": True,
        "salary_min": 0,
        "salary_max": 0,
    }
    raw.update(overrides)
    return raw


def test_normalize_job() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.source == "remoteok"
    assert job.source_job_id == "1135789"
    assert job.title == "Patient Outreach Specialist"
    assert job.company == "Grapefruit Health"
    assert job.location == "Worldwide"
    assert job.description == "&lt;p&gt;Build services.&lt;/p&gt;"
    assert (
        job.url
        == "https://remoteok.com/remote-jobs/remote-patient-outreach-specialist-grapefruit-health-1135789"
    )
    assert job.employment_type == "Full Time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-08-01T16:00:13+00:00"
    assert job.collected_at == _COLLECTED_AT


def test_normalize_job_skips_metadata_header() -> None:
    header: dict[str, Any] = {
        "last_updated": 1785659889,
        "legal": "API Terms of Service.",
    }

    job = normalize_job(header, collected_at=_COLLECTED_AT)

    assert job is None


def test_normalize_job_rejects_missing_id() -> None:
    raw = _raw_job()
    del raw["id"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_normalize_job_rejects_missing_title() -> None:
    raw = _raw_job()
    del raw["position"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_nullable_fields_handled() -> None:
    raw = _raw_job(
        company="",
        location="",
        description="",
        url="",
        apply_url="",
        tags=[],
        date=None,
        epoch=None,
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
    raw = _raw_job(
        tags=["full stack", "Full Time", "part time", "internship", "Full Time"],
    )

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.employment_type == "Full Time, part time, internship"


def test_employment_type_absent_when_no_matching_tags() -> None:
    raw = _raw_job(tags=["python", "full stack"])

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.employment_type is None


def test_remote_status() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.remote_status == "remote"


def test_published_at_fallback_from_epoch() -> None:
    raw = _raw_job(date=None, epoch=1785600013)

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at == "2026-08-01T16:00:13+00:00"


def test_published_at_rejects_invalid_epoch() -> None:
    raw = _raw_job(date=None, epoch="not-an-int")

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


def _patch_feed(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """Replace urlopen with a stub that returns the given JSON payload."""

    def fake_urlopen(request: object, timeout: float = 30.0) -> _FakeURLResponse:
        return _FakeURLResponse(payload)

    monkeypatch.setattr(collectors_http, "urlopen", fake_urlopen)


def test_fetch_rejects_non_list_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, {"data": []})

    with pytest.raises(RuntimeError):
        remoteok.fetch_jobs(limit=5)


def test_fetch_collection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    header: dict[str, Any] = {"last_updated": 1785659889, "legal": "terms"}
    jobs = [_raw_job(id=str(i), position=f"Role {i}") for i in range(5)]
    _patch_feed(monkeypatch, [header, *jobs])

    result = remoteok.fetch_jobs(limit=3)

    assert [job.source_job_id for job in result] == ["0", "1", "2"]


def test_fetch_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        remoteok.fetch_jobs(limit=0)


def test_fetch_skips_metadata_and_malformed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header: dict[str, Any] = {"last_updated": 1785659889, "legal": "terms"}
    malformed: dict[str, Any] = {"position": "No ID"}
    valid = _raw_job(id="42", position="Valid Role")
    _patch_feed(monkeypatch, [header, malformed, valid])

    result = remoteok.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "42"


def test_fetch_skips_non_dict_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, ["not-a-dict", _raw_job()])

    result = remoteok.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "1135789"
