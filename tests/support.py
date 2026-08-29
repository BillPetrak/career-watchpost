"""Trusted local ASGI client for API tests.

Production deliberately rejects Starlette's synthetic ``testclient`` peer;
tests opt into an explicit loopback peer through this helper.
"""

from fastapi.testclient import TestClient as _TestClient


class TestClient(_TestClient):
    def __init__(self, app, *args, **kwargs):
        self._auto_write = kwargs.pop("auto_write", True)
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        kwargs.setdefault("base_url", "http://127.0.0.1")
        super().__init__(app, *args, **kwargs)

    def request(self, method, url, **kwargs):
        if self._auto_write and method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CareerOS-Write", "1")
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)
