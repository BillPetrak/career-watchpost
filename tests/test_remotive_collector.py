"""Tests for the Remotive collector."""

import io
import json
from typing import Any

import pytest

from careeros.collectors import http as collectors_http
from careeros.collectors import remotive
from careeros.collectors.remotive import normalize_job

_COLLECTED_AT = "2026-08-02T21:00:00+00:00"


def _raw_job(**overrides: Any) -> dict[str, Any]:
    """Build a realistic Remotive API job object."""
    raw: dict[str, Any] = {
        "id": 1749306,
        "url": "https://remotive.com/remote-jobs/writing/freelance-copywriter-1749306",
        "title": "Freelance Copywriter",
        "company_name": "Coalition Technologies",
        "company_logo": "https://remotive.com/job/1749306/logo",
        "category": "Writing",
        "tags": ["copywriting", "content", "seo"],
        "job_type": "full_time",
        "publication_date": "2026-08-02T20:00:46",
        "candidate_required_location": "Worldwide",
        "salary": "$20k - $35k",
        "description": "<p>Write high-quality marketing copy.</p>",
    }
    raw.update(overrides)
    return raw


def test_normalize_job() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.source == "remotive"
    assert job.source_job_id == "1749306"
    assert job.title == "Freelance Copywriter"
    assert job.company == "Coalition Technologies"
    assert job.location == "Worldwide"
    assert job.description == "<p>Write high-quality marketing copy.</p>"
    assert (
        job.url
        == "https://remotive.com/remote-jobs/writing/freelance-copywriter-1749306"
    )
    assert job.employment_type == "full_time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-08-02T20:00:46"
    assert job.collected_at == _COLLECTED_AT


def test_normalize_job_rejects_missing_id() -> None:
    raw = _raw_job()
    del raw["id"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_normalize_job_rejects_missing_title() -> None:
    raw = _raw_job()
    del raw["title"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_boolean_id_rejected() -> None:
    raw = _raw_job(id=True)

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_nullable_fields_handled() -> None:
    raw = _raw_job(
        company_name="",
        candidate_required_location="",
        description="",
        url="",
        job_type="",
        publication_date="",
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


def test_employment_type_preserved_exactly() -> None:
    raw = _raw_job(job_type="part_time")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.employment_type == "part_time"


def test_publication_date_preserved_exactly() -> None:
    raw = _raw_job(publication_date="2026-08-02T20:00:46")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at == "2026-08-02T20:00:46"


def test_remote_status() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.remote_status == "remote"


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


def _envelope(*jobs: object) -> dict[str, object]:
    """Build a Remotive-style envelope object around job rows."""
    return {
        "00-warning": "Remotive Jobs API: you should not poll more than 4x/day.",
        "0-legal-notice": "Remotive Jobs API Terms of Service.",
        "job-count": len(jobs),
        "total-job-count": len(jobs),
        "jobs": list(jobs),
    }


def test_fetch_rejects_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, [{"title": "Wrong shape"}])

    with pytest.raises(RuntimeError):
        remotive.fetch_jobs(limit=5)


def test_fetch_rejects_missing_job_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, {"00-warning": "notice", "jobs": "nope"})

    with pytest.raises(RuntimeError):
        remotive.fetch_jobs(limit=5)


def test_fetch_collection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [_raw_job(id=index, title=f"Role {index}") for index in range(5)]
    _patch_feed(monkeypatch, _envelope(*jobs))

    result = remotive.fetch_jobs(limit=3)

    assert [job.source_job_id for job in result] == ["0", "1", "2"]


def test_fetch_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        remotive.fetch_jobs(limit=0)


def test_fetch_skips_malformed_and_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed: dict[str, Any] = {"title": "No ID"}
    valid = _raw_job(id="42", title="Valid Role")
    duplicate = _raw_job(id="42", title="Valid Role Again")
    _patch_feed(monkeypatch, _envelope(malformed, valid, duplicate))

    result = remotive.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "42"
    assert result[0].title == "Valid Role"


def test_fetch_skips_non_dict_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _envelope("not-a-dict", _raw_job()))

    result = remotive.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "1749306"
