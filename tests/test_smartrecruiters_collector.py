from __future__ import annotations

import urllib.error
import uuid
from typing import Any

import pytest

from careeros.collectors import smartrecruiters
from careeros.source_config import SmartRecruitersCompany, SmartRecruitersConfig

UUID = "123e4567-e89b-12d3-a456-426614174000"


def _config(*identifiers: str) -> SmartRecruitersConfig:
    return SmartRecruitersConfig(
        True, True, tuple(SmartRecruitersCompany(item) for item in identifiers)
    )


def _detail(
    identifier: str, posting_uuid: str = UUID, **overrides: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "uuid": posting_uuid,
        "name": " Role ",
        "company": {"name": " Company ", "identifier": identifier},
        "location": {"city": "Berlin", "region": "", "country": "Germany"},
        "jobAd": {"sections": {"jobDescription": {"text": " Description "}}},
        "postingUrl": " https://example.test/job ",
        "applyUrl": " https://example.test/apply ",
        "typeOfEmployment": {"label": " Full-time "},
        "remote": True,
        "releasedDate": "2026-09-29T12:00:00+02:00",
    }
    result.update(overrides)
    return result


def test_public_list_detail_and_authoritative_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        if "?" in url:
            return {
                "limit": 1,
                "offset": 0,
                "totalFound": 1,
                "content": [{"uuid": UUID.upper()}],
            }
        return _detail("acme")

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("acme"), clock=lambda: 1.0, sleeper=lambda _: None
    )
    assert result.succeeded and len(result.jobs) == 1
    job = result.jobs[0]
    assert job.source == "smartrecruiters"
    assert job.source_job_id == f"acme:{UUID}"
    assert job.location == "Berlin, Germany"
    assert job.description == "Description"
    assert job.url == "https://example.test/job"
    assert job.published_at == "2026-09-29T10:00:00+00:00"
    assert calls[0].endswith("/postings?destination=PUBLIC&limit=25&offset=0")
    assert calls[1].endswith(f"/postings/{UUID}")


def test_candidate_gate_duplicates_and_no_ref_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    other = "123e4567-e89b-12d3-a456-426614174001"

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        if "?" in url:
            return {
                "limit": 3,
                "offset": 0,
                "totalFound": 3,
                "content": [
                    {"uuid": "bad", "ref": "https://evil.invalid"},
                    {"uuid": UUID},
                    {"uuid": UUID, "ref": "https://evil.invalid"},
                ],
            }
        return _detail("acme")

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("acme"), limit=3, clock=lambda: 1.0, sleeper=lambda _: None
    )
    assert result.succeeded and len(result.jobs) == 1
    assert len(calls) == 2 and all("evil.invalid" not in call for call in calls)
    assert other not in result.jobs[0].source_job_id


def test_uuid_parser_programming_attribute_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smartrecruiters,
        "fetch_json",
        lambda url, *, timeout: {
            "limit": 1,
            "offset": 0,
            "totalFound": 1,
            "content": [{"uuid": UUID}],
        },
    )
    monkeypatch.setattr(
        smartrecruiters.uuid,
        "UUID",
        lambda value: (_ for _ in ()).throw(AttributeError("uuid bug")),
    )
    with pytest.raises(AttributeError, match="uuid bug"):
        smartrecruiters.fetch_result(
            config=_config("acme"), clock=lambda: 0.0, sleeper=lambda _: None
        )


def test_fixed_quota_cap_and_zero_quota_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        identifier = url.split("/companies/")[1].split("/")[0]
        if "?" in url:
            limit = int(url.split("limit=")[1].split("&")[0])
            return {
                "limit": limit,
                "offset": 0,
                "totalFound": limit,
                "content": [
                    {"uuid": f"123e4567-e89b-12d3-a456-42661417{n:04d}"}
                    for n in range(limit)
                ],
            }
        posting_uuid = url.rsplit("/", 1)[1]
        return _detail(identifier, posting_uuid)

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("a", "b", "c", "d", "e", "f", "g", "h"),
        limit=101,
        clock=lambda: 1.0,
        sleeper=lambda _: None,
    )
    assert result.succeeded and len(result.jobs) == 100
    assert len([url for url in calls if "?" in url]) == 8
    assert len(calls) == smartrecruiters.MAX_REQUESTS_PER_PROVIDER_RUN
    assert all("limit=13" in url or "limit=12" in url for url in calls if "?" in url)


def test_zero_quota_companies_make_no_request_or_pacing_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    clock_calls = 0
    sleeps: list[float] = []

    def clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        return 0.0

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        return {"limit": 1, "offset": 0, "totalFound": 0, "content": []}

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("a", "b", "c", "d", "e", "f", "g", "h"),
        limit=1,
        clock=clock,
        sleeper=sleeps.append,
    )
    assert result.succeeded
    assert len(calls) == 1 and "/companies/a/" in calls[0]
    assert clock_calls == 2
    assert sleeps == []


def test_default_limit_has_at_most_33_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        if "?" in url:
            identifier = url.split("/companies/")[1].split("/")[0]
            requested = int(url.split("limit=")[1].split("&")[0])
            return {
                "limit": requested,
                "offset": 0,
                "totalFound": requested,
                "content": [
                    {"uuid": str(uuid.UUID(int=(ord(identifier) << 16) + index + 1))}
                    for index in range(requested)
                ],
            }
        identifier = url.split("/companies/")[1].split("/")[0]
        return _detail(identifier, url.rsplit("/", 1)[1])

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("a", "b", "c", "d", "e", "f", "g", "h"),
        sleeper=lambda _: None,
    )
    assert result.succeeded and len(result.jobs) == 25
    assert len(calls) == 33


def test_request_budget_failure_is_expected_company_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smartrecruiters, "MAX_REQUESTS_PER_PROVIDER_RUN", 1)
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        return {"limit": 1, "offset": 0, "totalFound": 1, "content": [{"uuid": UUID}]}

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(config=_config("a"), sleeper=lambda _: None)
    assert not result.succeeded and len(calls) == 1
    assert result.error and "request budget exceeded" in result.error


def test_request_109_is_rejected_before_network() -> None:
    budget = smartrecruiters._RequestBudget()
    for _ in range(smartrecruiters.MAX_REQUESTS_PER_PROVIDER_RUN):
        budget.before_request()
    with pytest.raises(RuntimeError, match="request budget exceeded"):
        budget.before_request()
    assert budget.count == smartrecruiters.MAX_REQUESTS_PER_PROVIDER_RUN


def test_envelope_short_and_empty_pages_are_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smartrecruiters,
        "fetch_json",
        lambda url, *, timeout: {
            "limit": 1,
            "offset": 0,
            "totalFound": 99,
            "content": [],
        },
    )
    result = smartrecruiters.fetch_result(
        config=_config("acme"), sleeper=lambda _: None
    )
    assert result.succeeded and result.jobs == ()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"limit": True, "offset": 0, "totalFound": 0, "content": []},
        {"limit": 1, "offset": 1, "totalFound": 0, "content": []},
        {"limit": 1, "offset": 0, "totalFound": 0, "content": [{}] * 26},
    ],
)
def test_invalid_list_envelope_fails_company(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(smartrecruiters, "fetch_json", lambda url, *, timeout: payload)
    result = smartrecruiters.fetch_result(
        config=_config("acme"), sleeper=lambda _: None
    )
    assert not result.succeeded and result.jobs == ()


def test_detail_failure_is_company_atomic_and_later_company_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "123e4567-e89b-12d3-a456-426614174000"
    second = "123e4567-e89b-12d3-a456-426614174001"

    def fake(url: str, *, timeout: float) -> object:
        identifier = url.split("/companies/")[1].split("/")[0]
        if "?" in url:
            return {
                "limit": 2,
                "offset": 0,
                "totalFound": 2,
                "content": [{"uuid": first}, {"uuid": second}],
            }
        if identifier == "bad" and url.endswith(second):
            return {"uuid": first}
        return _detail(identifier, url.rsplit("/", 1)[1])

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("bad", "good"), limit=4, sleeper=lambda _: None
    )
    assert not result.succeeded
    assert all(job.company == "Company" for job in result.jobs)
    assert all("bad" not in job.source_job_id for job in result.jobs)
    assert result.error and result.error.startswith("bad: RuntimeError:")


def test_company_errors_are_joined_in_configured_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, *, timeout: float) -> object:
        identifier = url.split("/companies/")[1].split("/")[0]
        raise OSError(f"{identifier}-offline")

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("first", "second"),
        limit=2,
        clock=lambda: 0.0,
        sleeper=lambda _: None,
    )
    assert result.error == (
        "first: OSError: first-offline; second: OSError: second-offline"
    )


def test_pacing_uses_one_injected_provider_clock() -> None:
    starts: list[float] = []
    now = [0.0]
    sleeps: list[float] = []
    pacer = smartrecruiters._RequestPacer(
        clock=lambda: now[0],
        sleeper=lambda delay: (
            sleeps.append(delay),
            now.__setitem__(0, now[0] + delay),
        ),
    )
    pacer.before_request()
    starts.append(now[0])
    pacer.before_request()
    starts.append(now[0])
    assert sleeps == [0.125]
    assert starts[1] - starts[0] >= 0.125


def test_same_uuid_is_company_scoped_and_display_name_does_not_change_identity() -> (
    None
):
    first = smartrecruiters.source_job_id(identifier="one", posting_uuid=UUID)
    second = smartrecruiters.source_job_id(identifier="two", posting_uuid=UUID)
    assert first != second
    a = smartrecruiters.normalize_job(
        _detail("one", company={"name": "Old", "identifier": "one"}),
        identifier="one",
        selected_uuid=UUID,
        collected_at="now",
    )
    b = smartrecruiters.normalize_job(
        _detail("one", company={"name": "New", "identifier": "one"}),
        identifier="one",
        selected_uuid=UUID,
        collected_at="later",
    )
    assert a.source_job_id == b.source_job_id
    assert a.company != b.company


@pytest.mark.parametrize("key", ["limit", "offset", "totalFound"])
@pytest.mark.parametrize("value", [True, "1", -1])
def test_list_envelope_rejects_bool_noninteger_and_negative(
    monkeypatch: pytest.MonkeyPatch, key: str, value: object
) -> None:
    payload: dict[str, object] = {
        "limit": 1,
        "offset": 0,
        "totalFound": 0,
        "content": [],
    }
    payload[key] = value
    monkeypatch.setattr(smartrecruiters, "fetch_json", lambda url, *, timeout: payload)
    result = smartrecruiters.fetch_result(
        config=_config("acme"), sleeper=lambda _: None
    )
    assert not result.succeeded


def test_response_limit_mismatch_and_short_page_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smartrecruiters,
        "fetch_json",
        lambda url, *, timeout: (
            {
                "limit": 999,
                "offset": 0,
                "totalFound": 1,
                "content": [{"uuid": UUID}],
            }
            if "?" in url
            else _detail("acme")
        ),
    )
    result = smartrecruiters.fetch_result(
        config=_config("acme"), limit=5, sleeper=lambda _: None
    )
    assert result.succeeded and len(result.jobs) == 1


def test_total_found_below_content_and_nonlist_content_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for payload in (
        {"limit": 1, "offset": 0, "totalFound": 0, "content": [{"uuid": UUID}]},
        {"limit": 1, "offset": 0, "totalFound": 1, "content": {}},
    ):
        monkeypatch.setattr(
            smartrecruiters,
            "fetch_json",
            lambda url, *, timeout, payload=payload: payload,
        )
        result = smartrecruiters.fetch_result(
            config=_config("acme"), sleeper=lambda _: None
        )
        assert not result.succeeded


def test_detail_required_authority_failures_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _detail("acme")
    variants = [
        {"uuid": "not-a-uuid"},
        {"uuid": UUID, "name": " "},
        {"uuid": UUID, "company": None},
        {"uuid": UUID, "company": {"name": "Company"}},
        {"uuid": UUID, "company": {"name": "Company", "identifier": "other"}},
    ]
    for variant in variants:
        detail = dict(base)
        detail.update(variant)
        monkeypatch.setattr(
            smartrecruiters,
            "fetch_json",
            lambda url, *, timeout, detail=detail: (
                {
                    "limit": 1,
                    "offset": 0,
                    "totalFound": 1,
                    "content": [{"uuid": UUID}],
                }
                if "?" in url
                else detail
            ),
        )
        result = smartrecruiters.fetch_result(
            config=_config("acme"), sleeper=lambda _: None
        )
        assert not result.succeeded and result.jobs == ()


@pytest.mark.parametrize(
    ("remote", "hybrid", "expected"),
    [
        (True, True, "onsite_or_unspecified"),
        (True, False, "remote"),
        (False, True, "onsite_or_unspecified"),
        (False, False, "onsite_or_unspecified"),
        (None, None, None),
    ],
)
def test_remote_truth_table(
    remote: object, hybrid: object, expected: str | None
) -> None:
    raw = _detail("acme", remote=remote, hybrid=hybrid)
    result = smartrecruiters.normalize_job(
        raw, identifier="acme", selected_uuid=UUID, collected_at="now"
    )
    assert result.remote_status == expected


@pytest.mark.parametrize(
    "released, expected",
    [
        ("2026-01-01T00:00:00+02:00", "2025-12-31T22:00:00+00:00"),
        ("bad", None),
        ("2026-01-01T00:00:00", None),
    ],
)
def test_published_date_contract(released: str, expected: str | None) -> None:
    result = smartrecruiters.normalize_job(
        _detail("acme", releasedDate=released),
        identifier="acme",
        selected_uuid=UUID,
        collected_at="now",
    )
    assert result.published_at == expected


def test_optional_malformed_values_are_none_and_apply_url_is_ignored() -> None:
    result = smartrecruiters.normalize_job(
        _detail(
            "acme",
            location="bad",
            jobAd={
                "sections": {
                    "jobDescription": {"text": 3},
                    "qualifications": {"text": "must not use"},
                }
            },
            postingUrl=[],
            applyUrl="https://apply.example",
            typeOfEmployment={"label": []},
        ),
        identifier="acme",
        selected_uuid=UUID,
        collected_at="fixed",
    )
    assert result.location is None
    assert result.description is None
    assert result.url is None
    assert result.employment_type is None
    assert result.collected_at == "fixed"


def test_pacing_wraps_every_request_and_never_retries_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    starts: list[float] = []
    sleeps: list[float] = []

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    def fake(url: str, *, timeout: float) -> object:
        starts.append(now[0])
        if "?" in url:
            return {
                "limit": 2,
                "offset": 0,
                "totalFound": 2,
                "content": [{"uuid": UUID}, {"uuid": UUID.replace("000", "001")}],
            }
        return _detail(url.split("/companies/")[1].split("/")[0], url.rsplit("/", 1)[1])

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("a"), limit=2, clock=lambda: now[0], sleeper=sleeper
    )
    assert result.succeeded and len(starts) == 3
    assert all(b - a >= 0.125 for a, b in zip(starts, starts[1:], strict=False))
    assert len(sleeps) == 2

    def throttled(url: str, *, timeout: float) -> object:
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(smartrecruiters, "fetch_json", throttled)
    result = smartrecruiters.fetch_result(
        config=_config("a"), clock=lambda: 0.0, sleeper=lambda _: None
    )
    assert (
        not result.succeeded and result.error and result.error.count("HTTPError") == 1
    )


def test_pacing_is_provider_wide_across_companies_and_detail_429_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    starts: list[tuple[str, float]] = []

    def sleeper(delay: float) -> None:
        now[0] += delay

    def fake(url: str, *, timeout: float) -> object:
        starts.append((url, now[0]))
        identifier = url.split("/companies/")[1].split("/")[0]
        if "?" in url:
            return {
                "limit": 1,
                "offset": 0,
                "totalFound": 1,
                "content": [{"uuid": UUID}],
            }
        if identifier == "bad":
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return _detail(identifier)

    monkeypatch.setattr(smartrecruiters, "fetch_json", fake)
    result = smartrecruiters.fetch_result(
        config=_config("bad", "good"),
        limit=2,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    assert not result.succeeded and len(result.jobs) == 1
    assert len(starts) == 4
    assert all(
        later - earlier >= smartrecruiters.MIN_REQUEST_START_INTERVAL_SECONDS
        for (_, earlier), (_, later) in zip(starts, starts[1:], strict=False)
    )
    assert sum("/companies/bad/" in url for url, _ in starts) == 2
