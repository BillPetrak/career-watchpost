from __future__ import annotations

import pytest

from careeros.collectors import COLLECTORS, configured_collectors, lever
from careeros.source_config import LeverBoard, LeverConfig, SourceConfig
from careeros.sources import BUILT_IN_SOURCES, resolve_source_catalog


def _config(*boards: LeverBoard, enabled: bool = True) -> LeverConfig:
    return LeverConfig(True, enabled, boards)


def _row(identifier: str, title: str = "Role") -> dict[str, object]:
    return {"id": identifier, "text": title, "categories": {"location": "Berlin"}}


def test_builtin_registry_remains_frozen() -> None:
    assert tuple(COLLECTORS) == BUILT_IN_SOURCES
    assert len(COLLECTORS) == 7


def test_catalog_enabled_and_disabled_sets() -> None:
    enabled = resolve_source_catalog(
        config=SourceConfig(1, _config(LeverBoard("a", "global", "A")))
    )
    assert enabled.active == (*BUILT_IN_SOURCES, "lever")
    assert enabled.status == (*BUILT_IN_SOURCES, "lever")
    disabled = resolve_source_catalog(
        config=SourceConfig(1, _config(LeverBoard("a", "global", "A"), enabled=False))
    )
    assert disabled.active == BUILT_IN_SOURCES
    assert disabled.statistics == (*BUILT_IN_SOURCES, "lever")


def test_catalog_historical_jobs_are_visible() -> None:
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute("create table jobs(source text)")
    connection.execute("insert into jobs values ('lever')")
    catalog = resolve_source_catalog(
        connection, config=SourceConfig(1, LeverConfig(False, False, ()))
    )
    assert catalog.status[-1] == "lever"
    assert catalog.statistics[-1] == "lever"


def test_identity_encodes_reserved_and_unicode() -> None:
    assert (
        lever.source_job_id(instance="global", board_id="a:/%", posting_id="ü/x%")
        == "global:a%3A%2F%25:%C3%BC%2Fx%25"
    )


def test_normalization_uses_configured_company_and_no_publication() -> None:
    job = lever.normalize_job(
        _row("p"), LeverBoard("board", "eu", "Configured"), collected_at="now"
    )
    assert job is not None
    assert job.company == "Configured"
    assert job.published_at is None
    assert job.source_job_id == "eu:board:p"


@pytest.mark.parametrize(
    ("workplace", "expected"),
    [
        ("remote", "remote"),
        ("onsite", "onsite_or_unspecified"),
        ("on-site", "onsite_or_unspecified"),
        ("hybrid", "onsite_or_unspecified"),
        ("unknown", None),
    ],
)
def test_documented_workplace_variants(workplace: str, expected: str | None) -> None:
    job = lever.normalize_job(
        {**_row("p"), "workplaceType": workplace},
        LeverBoard("b", "global", "C"),
        collected_at="x",
    )
    assert job is not None and job.remote_status == expected


def test_all_documented_fields_normalize() -> None:
    job = lever.normalize_job(
        {
            "id": "p",
            "text": "T",
            "categories": {"location": "L", "commitment": "full-time"},
            "descriptionPlain": "D",
            "hostedUrl": "U",
            "workplaceType": "remote",
        },
        LeverBoard("b", "global", "C"),
        collected_at="x",
    )
    assert job is not None
    assert (job.location, job.description, job.url, job.employment_type) == (
        "L",
        "D",
        "U",
        "full-time",
    )


@pytest.mark.parametrize("raw", [{}, {"id": "x"}, {"text": "T"}, "not-object", None])
def test_malformed_rows_skipped(raw: object) -> None:
    if isinstance(raw, dict):
        assert (
            lever.normalize_job(raw, LeverBoard("b", "global", "C"), collected_at="x")
            is None
        )


def test_all_boards_failed_and_empty_board_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lever, "fetch_json", lambda url, timeout: (_ for _ in ()).throw(OSError("down"))
    )
    result = lever.fetch_result(config=_config(LeverBoard("a", "global", "A")), limit=2)
    assert not result.succeeded and result.jobs == ()
    monkeypatch.setattr(lever, "fetch_json", lambda url, timeout: [])
    result = lever.fetch_result(config=_config(LeverBoard("a", "global", "A")), limit=2)
    assert result.succeeded and result.jobs == ()


def test_five_page_bound_is_successful_underfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, timeout: float) -> object:
        calls.append(url)
        requested = int(url.split("limit=")[1].split("&")[0])
        return [_row("same") for _ in range(requested)]

    monkeypatch.setattr(lever, "fetch_json", fake)
    result = lever.fetch_result(
        config=_config(LeverBoard("b", "global", "C")), limit=10
    )
    assert result.succeeded and len(result.jobs) == 1 and len(calls) == 5


def test_invalid_response_shape_is_board_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lever, "fetch_json", lambda url, timeout: {})
    result = lever.fetch_result(config=_config(LeverBoard("b", "global", "C")), limit=1)
    assert not result.succeeded and result.jobs == ()


def test_provider_request_budget_is_exactly_forty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(url: str, timeout: float) -> object:
        calls.append(url)
        requested = int(url.split("limit=")[1].split("&")[0])
        board = url.split("postings/")[1].split("?")[0]
        return [_row(board) for _ in range(requested)]

    monkeypatch.setattr(lever, "fetch_json", fake)
    boards = tuple(LeverBoard(f"b{i}", "global", "C") for i in range(8))
    result = lever.fetch_result(config=_config(*boards), limit=800)
    assert result.succeeded and len(calls) == lever.MAX_REQUESTS_PER_PROVIDER_RUN


def test_undocumented_title_is_rejected() -> None:
    assert (
        lever.normalize_job(
            {"id": "p", "title": "No"}, LeverBoard("b", "global", "C"), collected_at="x"
        )
        is None
    )


def test_quotas_and_zero_quota_board(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(url: str, *, timeout: float) -> object:
        calls.append(url)
        return [_row(url)]

    monkeypatch.setattr(lever, "fetch_json", fake)
    boards = tuple(LeverBoard(str(i), "global", str(i)) for i in range(3))
    result = lever.fetch_result(config=_config(*boards), limit=2)
    assert result.succeeded and len(result.jobs) == 2
    assert len(calls) == 2


def test_duplicate_ids_first_valid_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(url: str, timeout: float) -> object:
        if "skip=0" in url:
            return [_row("x", "first"), _row("x", "second"), _row("y")]
        return []

    monkeypatch.setattr(lever, "fetch_json", fake)
    result = lever.fetch_result(config=_config(LeverBoard("b", "global", "C")), limit=3)
    assert [job.title for job in result.jobs] == ["first", "Role"]


def test_empty_page_is_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lever, "fetch_json", lambda url, timeout: [])
    result = lever.fetch_result(config=_config(LeverBoard("b", "global", "C")), limit=3)
    assert result.succeeded and result.jobs == ()


def test_short_page_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        lever, "fetch_json", lambda url, timeout: calls.append(url) or [_row("x")]
    )
    lever.fetch_result(config=_config(LeverBoard("b", "global", "C")), limit=10)
    assert len(calls) == 1


def test_board_failure_isolated_and_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(url: str, *, timeout: float) -> object:
        if "/bad?" in url:
            raise OSError("down")
        return [_row("good")]

    monkeypatch.setattr(lever, "fetch_json", fake)
    result = lever.fetch_result(
        config=_config(
            LeverBoard("bad", "global", "B"), LeverBoard("good", "global", "G")
        ),
        limit=2,
    )
    assert not result.succeeded and len(result.jobs) == 1
    assert result.error is not None and result.error.startswith("global:bad:")


def test_configured_collector_mapping_is_copy() -> None:
    mapping = configured_collectors(
        SourceConfig(1, _config(LeverBoard("b", "global", "C")))
    )
    assert tuple(mapping) == (*BUILT_IN_SOURCES, "lever")
    assert "lever" not in COLLECTORS
    assert callable(mapping["lever"])
