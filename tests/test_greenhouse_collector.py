from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.error import HTTPError

import pytest

from careeros.collectors import greenhouse


@dataclass(frozen=True)
class Board:
    token: str


@dataclass(frozen=True)
class Config:
    boards: tuple[Board, ...]
    present: bool = True
    enabled: bool = True


def _payload(*rows: object) -> dict[str, object]:
    return {"jobs": list(rows), "meta": {"total": len(rows)}}


def _job(identifier: object, title: object = "Role") -> dict[str, object]:
    return {
        "id": identifier,
        "title": title,
        "location": {"name": " Berlin "},
        "content": " <p>Body</p> ",
        "absolute_url": " https://example.test/job ",
        "updated_at": "ignored",
        "internal_job_id": "ignored",
    }


def test_normalize_job_uses_board_name_and_durable_identity() -> None:
    result = greenhouse.normalize_job(
        _job(127817, " Senior Role "),
        board_token="example-company",
        company="Authoritative Name",
        collected_at="2026-09-29T00:00:00+00:00",
    )
    assert result is not None
    assert result.source == "greenhouse"
    assert result.source_job_id == "example-company:127817"
    assert result.company == "Authoritative Name"
    assert result.title == "Senior Role"
    assert result.location == "Berlin"
    assert result.description == "<p>Body</p>"
    assert result.url == "https://example.test/job"
    assert result.employment_type is None
    assert result.remote_status is None
    assert result.published_at is None


def test_contract_constant_and_percent_encoded_identity() -> None:
    assert greenhouse.MAX_REQUESTS_PER_PROVIDER_RUN == 16
    assert greenhouse.source_job_id(board_token="a:/%", job_id=42) == "a%3A%2F%25:42"
    assert greenhouse.source_job_id(board_token="board-a", job_id=44444) != (
        greenhouse.source_job_id(board_token="board-b", job_id=44444)
    )


def test_metadata_name_is_authoritative_and_does_not_change_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = iter([" First Company ", "Renamed Company"])

    def fake(url: str, *, timeout: float) -> object:
        if url.endswith("/board"):
            return {"name": next(names)}
        return _payload(_job(7))

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    first = greenhouse.fetch_result(config=Config((Board("board"),)), limit=1)
    second = greenhouse.fetch_result(config=Config((Board("board"),)), limit=1)
    assert first.jobs[0].company == "First Company"
    assert second.jobs[0].company == "Renamed Company"
    assert first.jobs[0].source_job_id == second.jobs[0].source_job_id == "board:7"


def test_optional_malformed_values_and_ignored_fields() -> None:
    raw = _job(9, " Role ")
    raw.update(
        location="not-an-object",
        content=17,
        absolute_url=[],
        updated_at="must-not-publish",
        internal_job_id="must-not-identify",
        departments=["ignored"],
        offices=["ignored"],
    )
    result = greenhouse.normalize_job(
        raw,
        board_token="board",
        company="Company",
        collected_at="now",
    )
    assert result is not None
    assert result.location is None
    assert result.description is None
    assert result.url is None
    assert result.source_job_id == "board:9"
    assert result.published_at is None


@pytest.mark.parametrize("raw", [None, "row", {}, {"id": 1}, {"id": 1, "title": "  "}])
def test_invalid_rows_are_skipped(raw: object) -> None:
    assert (
        greenhouse.normalize_job(
            raw,
            board_token="board",
            company="Company",
            collected_at="now",
        )
        is None
    )


@pytest.mark.parametrize("identifier", [True, 0, -1, "127817", None])
def test_normalize_job_rejects_invalid_public_id(identifier: object) -> None:
    assert (
        greenhouse.normalize_job(
            _job(identifier),
            board_token="board",
            company="Company",
            collected_at="now",
        )
        is None
    )


def test_fetch_result_divmod_order_and_zero_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        if url.endswith("/boards/a"):
            return {"name": "A"}
        if url.endswith("/boards/b"):
            return {"name": "B"}
        if "/boards/a/jobs?content=true" in url:
            return _payload(_job(1), _job(2))
        if "/boards/b/jobs?content=true" in url:
            return _payload(_job(3))
        raise AssertionError(url)

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    result = greenhouse.fetch_result(
        config=Config((Board("a"), Board("b"), Board("c"))), limit=2
    )
    assert result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["a:1", "b:3"]
    assert all("/boards/c" not in call for call in calls)
    assert len(calls) == 4


def test_board_failure_isolated_and_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(url: str, *, timeout: float) -> object:
        if "/boards/bad" in url:
            raise OSError("down")
        if url.endswith("/boards/good"):
            return {"name": "Good"}
        return _payload(_job(4))

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    result = greenhouse.fetch_result(
        config=Config((Board("bad"), Board("good"))), limit=2
    )
    assert not result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["good:4"]
    assert result.error == "bad: OSError: down"


def test_metadata_failure_skips_jobs_and_later_board_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        if url.endswith("/bad"):
            raise HTTPError(url, 503, "unavailable", {}, None)
        if url.endswith("/good"):
            return {"name": "Good"}
        return _payload(_job(8))

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    result = greenhouse.fetch_result(
        config=Config((Board("bad"), Board("good"))), limit=2
    )
    assert not result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["good:8"]
    assert not any("bad/jobs" in call for call in calls)


@pytest.mark.parametrize("metadata", [[], {}, {"name": None}, {"name": "  "}])
def test_missing_or_empty_metadata_name_is_board_failure(
    monkeypatch: pytest.MonkeyPatch, metadata: object
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        return metadata

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    result = greenhouse.fetch_result(config=Config((Board("board"),)), limit=1)
    assert not result.succeeded and result.jobs == ()
    assert len(calls) == 1


def test_multiple_board_errors_are_deterministically_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(url: str, *, timeout: float) -> object:
        if url.endswith("/one"):
            raise ValueError("bad json")
        if url.endswith("/two"):
            raise OSError("down")
        raise AssertionError(url)

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    result = greenhouse.fetch_result(
        config=Config((Board("one"), Board("two"))), limit=2
    )
    assert result.error == "one: ValueError: bad json; two: OSError: down"
    assert result.jobs == ()


def test_failed_board_discards_jobs_accumulated_before_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = greenhouse.normalize_job

    def fake_fetch(url: str, *, timeout: float) -> object:
        if url.endswith("/board"):
            return {"name": "Company"}
        return _payload(_job(1), _job(2))

    def fail_second(raw: object, **kwargs: object):
        if isinstance(raw, dict) and raw.get("id") == 2:
            raise ValueError("late board failure")
        return original(raw, **kwargs)

    monkeypatch.setattr(greenhouse, "fetch_json", fake_fetch)
    monkeypatch.setattr(greenhouse, "normalize_job", fail_second)
    result = greenhouse.fetch_result(config=Config((Board("board"),)), limit=2)
    assert not result.succeeded and result.jobs == ()
    assert result.error == "board: ValueError: late board failure"


def test_metadata_and_jobs_contract_is_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([{"name": "Name"}, {"jobs": [], "meta": {"total": True}}])
    monkeypatch.setattr(
        greenhouse, "fetch_json", lambda url, *, timeout: next(responses)
    )
    result = greenhouse.fetch_result(config=Config((Board("board"),)), limit=1)
    assert not result.succeeded and result.jobs == ()
    assert result.error == (
        "board: RuntimeError: Greenhouse jobs response has invalid meta.total"
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"meta": {"total": 0}},
        {"jobs": [], "meta": []},
        {"jobs": [], "meta": {"total": "0"}},
        {"jobs": [], "meta": {"total": -1}},
        {"jobs": [_job(1)], "meta": {"total": 0}},
    ],
)
def test_jobs_response_shape_failures(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    responses = iter([{"name": "Name"}, payload])
    monkeypatch.setattr(
        greenhouse, "fetch_json", lambda url, *, timeout: next(responses)
    )
    result = greenhouse.fetch_result(config=Config((Board("board"),)), limit=1)
    assert not result.succeeded and result.jobs == ()


def test_empty_board_is_success_and_response_order_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        greenhouse,
        "fetch_json",
        lambda url, *, timeout: (
            {"name": "Name"}
            if url.endswith("/board")
            else _payload(_job(3, "Third"), _job(1, "First"))
        ),
    )
    result = greenhouse.fetch_result(config=Config((Board("board"),)), limit=5)
    assert result.succeeded
    assert [job.source_job_id for job in result.jobs] == ["board:3", "board:1"]

    empty = iter([{"name": "Name"}, _payload()])
    monkeypatch.setattr(greenhouse, "fetch_json", lambda url, *, timeout: next(empty))
    result = greenhouse.fetch_result(config=Config((Board("board"),)), limit=1)
    assert result.succeeded and result.jobs == ()


def test_eight_boards_make_exactly_sixteen_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        if url.endswith("/jobs?content=true"):
            return _payload(_job(1))
        return {"name": "Name"}

    monkeypatch.setattr(greenhouse, "fetch_json", fake)
    result = greenhouse.fetch_result(
        config=Config(tuple(Board(f"board-{index}") for index in range(8))), limit=8
    )
    assert result.succeeded and len(result.jobs) == 8
    assert len(calls) == greenhouse.MAX_REQUESTS_PER_PROVIDER_RUN


def test_limit_and_config_validation() -> None:
    with pytest.raises(ValueError):
        greenhouse.fetch_result(config=Config((Board("board"),)), limit=0)
    with pytest.raises(ValueError):
        greenhouse.fetch_result(config=Config((), enabled=True), limit=1)
    with pytest.raises(ValueError):
        greenhouse.fetch_result(
            config=Config((Board("board"),), enabled=False), limit=1
        )


def test_collected_at_is_shared_and_duplicate_first_valid_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        greenhouse,
        "fetch_json",
        lambda url, *, timeout: (
            {"name": "Name"}
            if url.endswith("/board")
            else _payload(
                _job(1, ""), _job(1, "First"), _job(1, "Second"), _job(2, "Other")
            )
        ),
    )
    result = greenhouse.fetch_result(config=Config((Board("board"),)), limit=2)
    assert [job.title for job in result.jobs] == ["First", "Other"]
    assert len({job.collected_at for job in result.jobs}) == 1
    captured = datetime.fromisoformat(result.jobs[0].collected_at)
    assert captured.utcoffset() == timedelta(0)


def test_unexpected_programming_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(*args: object, **kwargs: object) -> list[object]:
        raise TypeError("programming bug")

    monkeypatch.setattr(greenhouse, "_fetch_board", crash)
    with pytest.raises(TypeError):
        greenhouse.fetch_result(config=Config((Board("board"),)), limit=1)
