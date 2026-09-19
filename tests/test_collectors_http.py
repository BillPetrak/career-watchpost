"""Bounded response-body tests for shared collector HTTP helpers."""

from __future__ import annotations

import io
import json
from email.message import Message
from typing import Any

import pytest

from careeros.collectors import http


class _Response:
    def __init__(self, body: bytes, *, content_length: str | None = None) -> None:
        self._body = io.BytesIO(body)
        self.headers = Message()
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body.read(size)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_response(monkeypatch: pytest.MonkeyPatch, response: _Response) -> list[Any]:
    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: float = 30.0) -> _Response:
        requests.append(request)
        return response

    monkeypatch.setattr(http, "urlopen", fake_urlopen)
    return requests


def test_declared_oversized_body_rejected_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"not read", content_length=str(http.MAX_RESPONSE_BYTES + 1))
    _patch_response(monkeypatch, response)
    with pytest.raises(http.ResponseTooLargeError, match="declared"):
        http.fetch_bytes("https://example.invalid/jobs")
    assert response.read_sizes == []


def test_undeclared_oversized_body_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"x" * (http.MAX_RESPONSE_BYTES + 1))
    _patch_response(monkeypatch, response)
    with pytest.raises(http.ResponseTooLargeError):
        http.fetch_bytes("https://example.invalid/jobs")
    assert response.read_sizes == [http.MAX_RESPONSE_BYTES + 1]


def test_incorrect_small_content_length_cannot_bypass_actual_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b"x" * (http.MAX_RESPONSE_BYTES + 1), content_length="1")
    _patch_response(monkeypatch, response)
    with pytest.raises(http.ResponseTooLargeError):
        http.fetch_bytes("https://example.invalid/jobs")
    assert response.read_sizes == [http.MAX_RESPONSE_BYTES + 1]


def test_exactly_at_limit_and_normal_get_body_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact = b"x" * http.MAX_RESPONSE_BYTES
    response = _Response(exact, content_length=str(len(exact)))
    _patch_response(monkeypatch, response)
    assert http.fetch_bytes("https://example.invalid/jobs") == exact

    normal = _Response(b"normal")
    _patch_response(monkeypatch, normal)
    assert http.fetch_bytes("https://example.invalid/jobs") == b"normal"


def test_post_uses_same_ceiling_and_preserves_json_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = _Response(b"x" * (http.MAX_RESPONSE_BYTES + 1))
    requests = _patch_response(monkeypatch, oversized)
    with pytest.raises(http.ResponseTooLargeError):
        http.fetch_json_post("https://example.invalid/search", {"query": "python"})
    assert requests[0].get_method() == "POST"
    assert oversized.read_sizes == [http.MAX_RESPONSE_BYTES + 1]

    payload = {"hits": [1, 2]}
    response = _Response(json.dumps(payload).encode())
    _patch_response(monkeypatch, response)
    assert http.fetch_json_post("https://example.invalid/search", {}) == payload
