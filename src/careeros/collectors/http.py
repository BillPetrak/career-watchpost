"""Shared HTTP helpers for job-board collectors."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

USER_AGENT = "CareerOS/0.1"
DEFAULT_TIMEOUT = 30.0


def fetch_json(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> object:
    """Fetch a URL and parse its response body as JSON."""
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    with urlopen(request, timeout=timeout) as response:
        return json.load(response)
