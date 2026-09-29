from __future__ import annotations

from dataclasses import dataclass

import pytest

from careeros.collectors import workable
from careeros.collectors.http import ResponseTooLargeError


@dataclass(frozen=True)
class Board:
    subdomain: str


@dataclass(frozen=True)
class Config:
    boards: tuple[Board, ...]
    present: bool = True
    enabled: bool = True


def _job(shortcode: object, title: object = "Role") -> dict[str, object]:
    return {
        "shortcode": shortcode,
        "title": title,
        "city": " Berlin ",
        "state": "",
        "country": " Germany ",
        "description": " Body ",
        "application_url": " https://example.test/job ",
        "url": "https://example.test/form",
        "shortlink": " https://example.test/short ",
        "employment_type": " Full-time ",
        "workplace_type": "remote",
        "telecommuting": False,
        "published_on": "2026-09-29",
        "created_at": "not used",
    }


def test_normalize_public_mapping_and_scoped_identity() -> None:
    result = workable.normalize_job(
        _job("a:/%"),
        subdomain="account",
        company="Authoritative Company",
        collected_at="2026-09-29T00:00:00+00:00",
    )
    assert result is not None
    assert result.source == "workable"
    assert result.source_job_id == "account:a%3A%2F%25"
    assert result.company == "Authoritative Company"
    assert result.location == "Berlin, Germany"
    assert result.url == "https://example.test/job"
    assert result.description == "Body"
    assert result.remote_status == "remote"
    assert result.published_at == "2026-09-29"


@pytest.mark.parametrize(
    "workplace, expected",
    [
        ("on_site", "onsite_or_unspecified"),
        ("hybrid", "onsite_or_unspecified"),
        ("other", "remote"),
    ],
)
def test_workplace_mapping_and_boolean_fallback(workplace: str, expected: str) -> None:
    raw = _job("x")
    raw["workplace_type"] = workplace
    raw["telecommuting"] = workplace == "other"
    result = workable.normalize_job(raw, subdomain="a", company="C", collected_at="now")
    assert result is not None and result.remote_status == expected


def test_optional_malformed_values_and_invalid_rows_are_skipped() -> None:
    raw = _job("x")
    raw.update(
        location={"city": "Nested Must Be Ignored"},
        city=3,
        state=[],
        country=None,
        description=3,
        application_url=[],
        shortlink=" fallback ",
        published_on="2026-9-29",
        created_at="2026-09-29",
    )
    result = workable.normalize_job(raw, subdomain="a", company="C", collected_at="now")
    assert result is not None
    assert result.location is None and result.description is None
    assert result.url == "fallback" and result.published_at is None
    for invalid in (
        None,
        "row",
        {},
        {"shortcode": "x"},
        {"title": "Role"},
        {"shortcode": "x", "title": " "},
    ):
        assert (
            workable.normalize_job(
                invalid, subdomain="a", company="C", collected_at="now"
            )
            is None
        )


def test_quota_order_zero_quota_and_duplicate_first_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        subdomain = url.split("/accounts/")[1].split("?")[0]
        return {
            "name": subdomain,
            "jobs": [
                _job(f"{subdomain}-1"),
                _job(f"{subdomain}-1"),
                _job(f"{subdomain}-2"),
            ],
        }

    monkeypatch.setattr(workable, "fetch_json", fake)
    result = workable.fetch_result(
        config=Config((Board("a"), Board("b"), Board("c"))), limit=2
    )
    assert result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["a:a-1", "b:b-1"]
    assert len(calls) == 2 and all("/accounts/c?" not in call for call in calls)


def test_partial_account_failure_isolated_and_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, *, timeout: float) -> object:
        if "/accounts/bad?" in url:
            raise OSError("down")
        name = url.split("/accounts/")[1].split("?")[0]
        return {"name": name, "jobs": [_job("1")]}

    monkeypatch.setattr(workable, "fetch_json", fake)
    result = workable.fetch_result(
        config=Config((Board("bad"), Board("good"))), limit=4
    )
    assert not result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["good:1"]
    assert result.error == "bad: OSError: down"


def test_invalid_envelope_and_programming_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workable, "fetch_json", lambda url, timeout: {"name": "C", "jobs": {}}
    )
    result = workable.fetch_result(config=Config((Board("bad"),)), limit=1)
    assert not result.succeeded and "unexpected shape" in (result.error or "")
    monkeypatch.setattr(
        workable, "fetch_json", lambda url, timeout: {"name": "C", "jobs": []}
    )
    assert workable.fetch_result(config=Config((Board("ok"),)), limit=1).succeeded
    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: (_ for _ in ()).throw(TypeError("bug")),
    )
    with pytest.raises(TypeError):
        workable.fetch_result(config=Config((Board("bug"),)), limit=1)


def test_identity_is_account_scoped_and_public_endpoint_url() -> None:
    assert workable.source_job_id(
        subdomain="one", shortcode="same"
    ) != workable.source_job_id(subdomain="two", shortcode="same")
    assert workable.source_job_id(
        subdomain="a", shortcode="old"
    ) != workable.source_job_id(subdomain="a", shortcode="new")
    assert (
        workable._account_url("a-b")
        == "https://www.workable.com/api/accounts/a-b?details=true"
    )


@pytest.mark.parametrize("name", [None, 1, "   ", []])
def test_account_name_is_required_and_authoritative(
    monkeypatch: pytest.MonkeyPatch, name: object
) -> None:
    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: {"name": name, "jobs": [_job("1")]},
    )
    result = workable.fetch_result(config=Config((Board("account"),)), limit=1)
    assert not result.succeeded and result.jobs == ()
    assert "account: RuntimeError:" in (result.error or "")

    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: {"name": "  Display Name  ", "jobs": [_job("1")]},
    )
    result = workable.fetch_result(config=Config((Board("account"),)), limit=1)
    assert result.succeeded and result.jobs[0].company == "Display Name"


def test_identity_stability_company_change_and_both_components_encoded() -> None:
    assert workable.source_job_id(subdomain="a:/", shortcode="x y/%") == (
        "a%3A%2F:x%20y%2F%25"
    )
    first = workable.normalize_job(
        _job("same"), subdomain="account", company="Old", collected_at="now"
    )
    second = workable.normalize_job(
        _job("same"), subdomain="account", company="New", collected_at="later"
    )
    assert first is not None and second is not None
    assert first.source_job_id == second.source_job_id
    assert first.company != second.company


def test_all_field_mappings_dates_and_no_prose_inference() -> None:
    raw = _job("fields")
    raw.update(
        city=" City ",
        state=" State ",
        country=" Country ",
        application_url="  ",
        shortlink=" https://short.example/job ",
        employment_type=" Contract ",
        workplace_type="unknown",
        telecommuting=False,
        description="remote role in Berlin",
        published_on="2026-09-29",
        created_at="2026-01-01",
    )
    result = workable.normalize_job(
        raw, subdomain="account", company="Company", collected_at="now"
    )
    assert result is not None
    assert result.location == "City, State, Country"
    assert result.url == "https://short.example/job"
    assert result.employment_type == "Contract"
    assert result.remote_status is None
    assert result.published_at == "2026-09-29"
    assert result.collected_at == "now"
    for invalid in ("2026-9-29", "2026-02-30", "2026-09-29T00:00:00", 20260929):
        raw["published_on"] = invalid
        normalized = workable.normalize_job(
            raw, subdomain="account", company="Company", collected_at="now"
        )
        assert normalized is not None and normalized.published_at is None


def test_one_aware_utc_collected_at_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    collected: list[str] = []
    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: {
            "name": "Company",
            "jobs": [_job("1"), _job("2")],
        },
    )
    result = workable.fetch_result(config=Config((Board("a"), Board("b"))), limit=4)
    assert result.succeeded
    collected.extend(job.collected_at for job in result.jobs)
    assert len(set(collected)) == 1
    assert collected[0].endswith("+00:00")


def test_exact_quota_request_budget_no_redistribution_or_companions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        subdomain = url.split("/accounts/")[1].split("?")[0]
        return {"name": subdomain, "jobs": [] if subdomain == "empty" else [_job("1")]}

    monkeypatch.setattr(workable, "fetch_json", fake)
    result = workable.fetch_result(
        config=Config(tuple(Board(f"b{i}") for i in range(8))), limit=3
    )
    assert result.succeeded and len(calls) == 3
    assert [call.split("/accounts/")[1].split("?")[0] for call in calls] == [
        "b0",
        "b1",
        "b2",
    ]
    calls.clear()
    result = workable.fetch_result(
        config=Config((Board("empty"), Board("one"), Board("two"))), limit=5
    )
    assert result.succeeded and len(calls) == 3
    assert len(result.jobs) == 2
    assert all("?details=true" in call for call in calls)


def test_account_local_accumulation_is_discarded_and_errors_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, *, timeout: float) -> object:
        subdomain = url.split("/accounts/")[1].split("?")[0]
        if subdomain in {"bad-a", "bad-b"}:
            raise OSError(subdomain)
        return {"name": "Good", "jobs": [_job("healthy")]}

    monkeypatch.setattr(workable, "fetch_json", fake)
    result = workable.fetch_result(
        config=Config((Board("bad-a"), Board("good"), Board("bad-b"))), limit=3
    )
    assert not result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["good:healthy"]
    assert result.error == "bad-a: OSError: bad-a; bad-b: OSError: bad-b"


def test_oversized_response_is_account_local_and_programming_attribute_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, *, timeout: float) -> object:
        subdomain = url.split("/accounts/")[1].split("?")[0]
        if subdomain == "large":
            raise ResponseTooLargeError("too large")
        return {"name": "Good", "jobs": [_job("1")]}

    monkeypatch.setattr(workable, "fetch_json", fake)
    result = workable.fetch_result(
        config=Config((Board("large"), Board("good"))), limit=2
    )
    assert not result.succeeded and [job.source_job_id for job in result.jobs] == [
        "good:1"
    ]
    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: (_ for _ in ()).throw(AttributeError("bug")),
    )
    with pytest.raises(AttributeError):
        workable.fetch_result(config=Config((Board("bug"),)), limit=1)


def test_local_accumulation_is_discarded_when_later_row_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = workable.normalize_job
    calls = 0

    def failing_normalize(raw: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if kwargs["subdomain"] == "bad" and calls == 2:
            raise RuntimeError("row failure")
        return original(raw, **kwargs)

    monkeypatch.setattr(workable, "normalize_job", failing_normalize)
    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: {
            "name": "Company",
            "jobs": [_job("first"), _job("second")]
            if "/bad?" in url
            else [_job("healthy")],
        },
    )
    result = workable.fetch_result(
        config=Config((Board("bad"), Board("good"))), limit=4
    )
    assert not result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["good:healthy"]
    assert result.error == "bad: RuntimeError: row failure"


def test_divmod_quota_distribution_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        subdomain = url.split("/accounts/")[1].split("?")[0]
        return {"name": subdomain, "jobs": [_job(f"{subdomain}-{i}") for i in range(5)]}

    monkeypatch.setattr(workable, "fetch_json", fake)
    result = workable.fetch_result(
        config=Config((Board("a"), Board("b"), Board("c"))), limit=5
    )
    assert result.succeeded
    assert [job.source_job_id for job in result.jobs] == [
        "a:a-0",
        "a:a-1",
        "b:b-0",
        "b:b-1",
        "c:c-0",
    ]
    assert len(calls) == 3


def test_limit_eight_boards_uses_exactly_eight_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: calls.append(url) or {"name": "C", "jobs": []},
    )
    result = workable.fetch_result(
        config=Config(tuple(Board(f"account-{i}") for i in range(8))), limit=8
    )
    assert result.succeeded and result.jobs == ()
    assert len(calls) == workable.MAX_REQUESTS_PER_PROVIDER_RUN == 8


def test_all_account_failure_has_no_jobs_and_ordered_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, *, timeout: float) -> object:
        subdomain = url.split("/accounts/")[1].split("?")[0]
        raise OSError(f"failure-{subdomain}")

    monkeypatch.setattr(workable, "fetch_json", fake)
    result = workable.fetch_result(
        config=Config((Board("first"), Board("second"), Board("third"))), limit=3
    )
    assert not result.succeeded and result.jobs == ()
    assert result.error == (
        "first: OSError: failure-first; second: OSError: failure-second; "
        "third: OSError: failure-third"
    )


def test_first_valid_duplicate_wins_and_form_url_is_not_careeros_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _job("duplicate", title=" "),
        _job("duplicate", title="First Valid"),
        _job("duplicate", title="Later Valid"),
    ]
    rows[1]["application_url"] = ""
    rows[1]["shortlink"] = ""
    rows[1]["url"] = "https://example.test/form"
    monkeypatch.setattr(
        workable,
        "fetch_json",
        lambda url, timeout: {"name": "Company", "jobs": rows},
    )
    result = workable.fetch_result(config=Config((Board("account"),)), limit=3)
    assert result.succeeded and len(result.jobs) == 1
    assert result.jobs[0].title == "First Valid"
    assert result.jobs[0].url is None
