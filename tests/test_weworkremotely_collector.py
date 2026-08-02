"""Tests for the We Work Remotely collector."""

import io
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from careeros.collectors import http as collectors_http
from careeros.collectors import weworkremotely
from careeros.collectors.weworkremotely import normalize_job

_COLLECTED_AT = "2026-08-02T08:00:00+00:00"

_DEFAULT_FIELDS: dict[str, str] = {
    "title": "Doximity: Data Engineering Manager",
    "region": "Anywhere in the World",
    "country": "",
    "state": "",
    "skills": "",
    "category": "Full-Stack Programming",
    "type": "Full-Time",
    "description": "<p>Build data services.</p>",
    "guid": "https://weworkremotely.com/remote-jobs/doximity-data-engineering-manager",
    "link": "https://weworkremotely.com/remote-jobs/doximity-data-engineering-manager",
    "pubDate": "Sun, 02 Aug 2026 07:31:05 +0000",
    "expires_at": "Tue, 01 Sep 2026 07:31:05 +0000",
}


def _item(**overrides: Any) -> ET.Element:
    """Build a We Work Remotely RSS item element."""
    fields = dict(_DEFAULT_FIELDS)
    fields.update(overrides)

    item = ET.Element("item")
    for tag, text in fields.items():
        child = ET.SubElement(item, tag)
        child.text = text
    return item


def test_normalize_job() -> None:
    job = normalize_job(_item(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.source == "weworkremotely"
    assert (
        job.source_job_id
        == "https://weworkremotely.com/remote-jobs/doximity-data-engineering-manager"
    )
    assert job.title == "Data Engineering Manager"
    assert job.company == "Doximity"
    assert job.location == "Anywhere in the World"
    assert job.description == "<p>Build data services.</p>"
    assert (
        job.url
        == "https://weworkremotely.com/remote-jobs/doximity-data-engineering-manager"
    )
    assert job.employment_type == "full time"
    assert job.remote_status == "remote"
    assert job.published_at == "2026-08-02T07:31:05+00:00"
    assert job.collected_at == _COLLECTED_AT


def test_normalize_job_rejects_missing_identity() -> None:
    raw = _item()
    raw.remove(raw.find("guid"))
    raw.remove(raw.find("link"))

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_source_job_id_falls_back_to_link() -> None:
    raw = _item()
    raw.remove(raw.find("guid"))

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert (
        job.source_job_id
        == "https://weworkremotely.com/remote-jobs/doximity-data-engineering-manager"
    )


def test_normalize_job_rejects_missing_title() -> None:
    raw = _item()
    raw.remove(raw.find("title"))

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is None


def test_title_without_company_prefix() -> None:
    raw = _item(title="Data Engineering Manager")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.company is None
    assert job.title == "Data Engineering Manager"


def test_title_preserves_extra_colons_in_role() -> None:
    raw = _item(title="Flexport: Account Executive: Enterprise")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.company == "Flexport"
    assert job.title == "Account Executive: Enterprise"


def test_nullable_fields_handled() -> None:
    raw = _item(
        region="",
        state="",
        country="",
        description="",
        type="",
        pubDate="",
    )

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.location is None
    assert job.description is None
    assert job.employment_type is None
    assert job.published_at is None
    assert job.remote_status == "remote"
    assert (
        job.url
        == "https://weworkremotely.com/remote-jobs/doximity-data-engineering-manager"
    )


def test_url_none_when_link_missing() -> None:
    raw = _item(link="")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.url is None
    assert (
        job.source_job_id
        == "https://weworkremotely.com/remote-jobs/doximity-data-engineering-manager"
    )


def test_employment_type_normalization() -> None:
    cases = [
        ("Full-Time", "full time"),
        ("Contract", "contract"),
        ("  Part-Time ", "part time"),
        ("", None),
    ]

    for raw, expected in cases:
        job = normalize_job(_item(type=raw), collected_at=_COLLECTED_AT)
        assert job is not None
        assert job.employment_type == expected


def test_remote_status() -> None:
    job = normalize_job(_item(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.remote_status == "remote"


def test_published_at_rfc822_normalization() -> None:
    job = normalize_job(_item(), collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at == "2026-08-02T07:31:05+00:00"


def test_published_at_rejects_invalid_value() -> None:
    raw = _item(pubDate="not-a-date")

    job = normalize_job(raw, collected_at=_COLLECTED_AT)

    assert job is not None
    assert job.published_at is None


def test_location_falls_back_to_state_then_country() -> None:
    job = normalize_job(
        _item(region="", state="California"),
        collected_at=_COLLECTED_AT,
    )

    assert job is not None
    assert job.location == "California"

    job = normalize_job(
        _item(region="", state="", country="Canada"),
        collected_at=_COLLECTED_AT,
    )

    assert job is not None
    assert job.location == "Canada"


def _rss_bytes(items: list[ET.Element]) -> bytes:
    """Serialize items into a We Work Remotely RSS document."""
    channel = ET.Element("channel")
    for item in items:
        channel.append(item)

    rss = ET.Element("rss", {"version": "2.0"})
    rss.append(channel)
    return ET.tostring(rss, encoding="utf-8")


class _FakeURLResponse:
    """Stand-in for urlopen's context-manager response."""

    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def __enter__(self) -> io.BytesIO:
        return self._buffer

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_feed(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    """Replace urlopen with a stub that returns the given RSS bytes."""

    def fake_urlopen(request: object, timeout: float = 30.0) -> _FakeURLResponse:
        return _FakeURLResponse(payload)

    monkeypatch.setattr(collectors_http, "urlopen", fake_urlopen)


def test_fetch_rejects_non_rss_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, b"<html><body>not a feed</body></html>")

    with pytest.raises(RuntimeError):
        weworkremotely.fetch_jobs(limit=5)


def test_fetch_rejects_malformed_xml(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, b"this is not xml")

    with pytest.raises(RuntimeError):
        weworkremotely.fetch_jobs(limit=5)


def test_fetch_collection_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        _item(
            guid=f"https://weworkremotely.com/remote-jobs/job-{i}",
            link=f"https://weworkremotely.com/remote-jobs/job-{i}",
            title=f"Co{i}: Role {i}",
        )
        for i in range(5)
    ]
    _patch_feed(monkeypatch, _rss_bytes(items))

    result = weworkremotely.fetch_jobs(limit=3)

    assert len(result) == 3
    assert result[0].source_job_id == "https://weworkremotely.com/remote-jobs/job-0"


def test_fetch_deduplicates_within_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _rss_bytes([_item(), _item()]))

    result = weworkremotely.fetch_jobs(limit=10)

    assert len(result) == 1


def test_fetch_skips_malformed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    missing_title = _item()
    missing_title.remove(missing_title.find("title"))
    valid = _item(
        guid="https://weworkremotely.com/remote-jobs/job-42",
        link="https://weworkremotely.com/remote-jobs/job-42",
        title="Co: Valid Role",
    )
    _patch_feed(monkeypatch, _rss_bytes([missing_title, valid]))

    result = weworkremotely.fetch_jobs(limit=10)

    assert len(result) == 1
    assert result[0].source_job_id == "https://weworkremotely.com/remote-jobs/job-42"


def test_fetch_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        weworkremotely.fetch_jobs(limit=0)


def test_fetch_empty_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_feed(monkeypatch, _rss_bytes([]))

    assert weworkremotely.fetch_jobs(limit=10) == []
