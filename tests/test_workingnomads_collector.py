"""Tests for the Working Nomads collector."""

import io
import json
from typing import Any

import pytest

from careeros.collectors import http as collectors_http
from careeros.collectors import workingnomads
from careeros.collectors.workingnomads import MAX_API_LIMIT, normalize_job

_COLLECTED_AT = "2026-08-04T20:00:00+00:00"


def _raw_job(**overrides: Any) -> dict[str, Any]:
    """Build a realistic Working Nomads API job object."""
    raw: dict[str, Any] = {
        "id": 1709791,
        "title": "Full-Cycle Account Executive",
        "slug": "full-cycle-account-executive-xplor-1709791",
        "company": "Xplor",
        "company_slug": "xplor",
        "category_name": "Sales",
        "description": "<section><p>Build pipelines.</p></section>",
        "position_type": "ft",
        "tags": ["account executive", "saas"],
        "locations": ["Japan"],
        "location_base": "",
        "pub_date": "2026-07-06T01:41:05.192572-04:00",
        "apply_url": "https://jobs.smartrecruiters.com/Xplor/744000130174094",
    }
    raw.update(overrides)
    return raw


def test_normalize_job() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.source == "workingnomads"
    assert job.source_job_id == "1709791"
    assert job.title == "Full-Cycle Account Executive"
    assert job.company == "Xplor"
    assert job.location == "Japan"
    assert job.description == "<section><p>Build pipelines.</p></section>"
    assert (
        job.url
        == "https://www.workingnomads.com/jobs/full-cycle-account-executive-xplor-1709791"
    )
    assert job.employment_type == "full time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-07-06T01:41:05.192572-04:00"
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
        company="",
        slug="",
        apply_url="",
        description="",
        position_type="zz",
        locations=[],
        pub_date="",
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


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ft", "full time"),
        ("pt", "part time"),
        ("fr", "freelance"),
        ("FT", "full time"),
        ("  Pt ", "part time"),
        ("zz", None),
        ("", None),
    ],
)
def test_employment_type_mapping(code: str, expected: str | None) -> None:
    job = normalize_job(_raw_job(position_type=code), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.employment_type == expected


def test_employment_type_missing_is_none() -> None:
    raw = _raw_job()
    del raw["position_type"]

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.employment_type is None


def test_remote_status() -> None:
    job = normalize_job(_raw_job(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.remote_status == "remote"


def test_url_constructed_from_slug() -> None:
    job = normalize_job(
        _raw_job(slug="senior-database-administrator-finalsite-1709794"),
        collected_at=_COLLECTED_AT,
    )

    assert job is not None
    assert (
        job.url
        == "https://www.workingnomads.com/jobs/senior-database-administrator-finalsite-1709794"
    )


def test_url_falls_back_to_apply_url_without_slug() -> None:
    raw = _raw_job(slug="", apply_url="https://jsco.re/hgx7a")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.url == "https://jsco.re/hgx7a"


def test_location_joins_non_blank_values() -> None:
    raw = _raw_job(locations=["Taiwan", "Province of China"])

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location == "Taiwan, Province of China"


def test_location_strips_blank_values() -> None:
    raw = _raw_job(locations=["", "Japan", "  "])

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location == "Japan"


def test_location_none_when_not_a_list() -> None:
    raw = _raw_job(locations="Japan")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location is None


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
    requested: list[tuple[str, bytes]] | None = None,
) -> None:
    """Replace urlopen with a stub that returns the given JSON payload."""

    def fake_urlopen(request: object, timeout: float = 30.0) -> _FakeURLResponse:
        if requested is not None:
            requested.append((request.full_url, request.data))
        return _FakeURLResponse(payload)

    monkeypatch.setattr(collectors_http, "urlopen", fake_urlopen)


def _es_payload(*sources: object) -> dict[str, object]:
    """Build an Elasticsearch-style search response around job _source rows."""
    hits = [{"_index": "jobsapi", "_id": None, "_source": source} for source in sources]
    return {
        "took": 0,
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
        "hits": {
            "total": {"value": len(hits), "relation": "eq"},
            "max_score": 1.0,
            "hits": hits,
        },
    }


def test_fetch_sends_bounded_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[tuple[str, bytes]] = []
    jobs = [_raw_job(id=i, title=f"Role {i}") for i in range(5)]
    _patch_feed(monkeypatch, _es_payload(*jobs), requested)

    workingnomads.fetch_jobs(limit=3)

    assert len(requested) == 1
    url, body = requested[0]
    assert url == "https://www.workingnomads.com/jobsapi/_search"
    assert json.loads(body) == {"from": 0, "size": 3, "sort": [{"id": "desc"}]}


def test_fetch_collection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [_raw_job(id=i, title=f"Role {i}") for i in range(5)]
    _patch_feed(monkeypatch, _es_payload(*jobs))

    result = workingnomads.fetch_jobs(limit=3)

    assert [job.source_job_id for job in result] == ["0", "1", "2"]


def test_fetch_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        workingnomads.fetch_jobs(limit=0)


def test_fetch_clamps_limit_above_api_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [_raw_job(id=i, title=f"Role {i}") for i in range(MAX_API_LIMIT)]
    requested: list[tuple[str, bytes]] = []
    _patch_feed(monkeypatch, _es_payload(*jobs), requested)

    result = workingnomads.fetch_jobs(limit=MAX_API_LIMIT + 50)

    assert len(result) == MAX_API_LIMIT
    assert json.loads(requested[0][1])["size"] == MAX_API_LIMIT


def test_fetch_rejects_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, [{"title": "Wrong shape"}])

    with pytest.raises(RuntimeError):
        workingnomads.fetch_jobs(limit=5)


def test_fetch_rejects_missing_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, {"took": 0})

    with pytest.raises(RuntimeError):
        workingnomads.fetch_jobs(limit=5)


def test_fetch_rejects_missing_hits_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, {"hits": {"total": 0}})

    with pytest.raises(RuntimeError):
        workingnomads.fetch_jobs(limit=5)


def test_fetch_rejects_error_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, {"error": {"type": "index_not_found_exception"}})

    with pytest.raises(RuntimeError):
        workingnomads.fetch_jobs(limit=5)


def test_fetch_empty_result_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _es_payload())

    assert workingnomads.fetch_jobs(limit=10) == []


def test_fetch_skips_malformed_and_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed: dict[str, Any] = {"title": "No ID"}
    valid = _raw_job(id=42, title="Valid Role")
    duplicate = _raw_job(id=42, title="Valid Role Again")
    _patch_feed(monkeypatch, _es_payload(malformed, valid, duplicate))

    result = workingnomads.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "42"
    assert result[0].title == "Valid Role"


def test_fetch_skips_non_dict_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _es_payload("not-a-dict", _raw_job()))

    result = workingnomads.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "1709791"


def test_fetch_skips_rows_without_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(
        monkeypatch,
        _es_payload({"title": "No _source"}, _raw_job()),
    )

    result = workingnomads.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "1709791"
