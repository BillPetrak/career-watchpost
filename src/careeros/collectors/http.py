"""Shared HTTP helpers for job-board collectors."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

USER_AGENT = "CareerOS/0.1"
DEFAULT_TIMEOUT = 30.0


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
        return bytes(response.read())


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
        return json.loads(bytes(response.read()))
