"""Shared HTTP helpers for job-board collectors."""

from __future__ import annotations

import json
from typing import Protocol
from urllib.request import Request, urlopen

USER_AGENT = "CareerOS/0.1"
DEFAULT_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ResponseTooLargeError(RuntimeError):
    """Raised when a collector response exceeds the shared safety ceiling."""


class _ReadableResponse(Protocol):
    def read(self, size: int = -1) -> bytes: ...


def _response_bytes(response: _ReadableResponse) -> bytes:
    """Read at most one byte beyond the ceiling and reject oversized bodies."""
    headers = getattr(response, "headers", None)
    content_length = headers.get("Content-Length") if headers is not None else None
    if content_length is not None:
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            declared = None
        if declared is not None and declared > MAX_RESPONSE_BYTES:
            raise ResponseTooLargeError(
                f"HTTP response exceeds {MAX_RESPONSE_BYTES} byte limit "
                f"(declared {declared})"
            )

    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ResponseTooLargeError(
            f"HTTP response exceeds {MAX_RESPONSE_BYTES} byte limit"
        )
    return bytes(body)


def fetch_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    accept: str = "*/*",
) -> bytes:
    """Fetch a URL and return its raw response body."""
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": USER_AGENT,
        },
    )

    with urlopen(request, timeout=timeout) as response:
        return _response_bytes(response)


def fetch_json(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> object:
    """Fetch a URL and parse its response body as JSON."""
    return json.loads(fetch_bytes(url, timeout=timeout, accept="application/json"))


def fetch_json_post(
    url: str,
    payload: object,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> object:
    """POST a JSON payload to a URL and parse the response body as JSON."""
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    with urlopen(request, timeout=timeout) as response:
        return json.loads(_response_bytes(response))
