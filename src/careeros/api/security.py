"""Central deployment-boundary policy for the loopback web application."""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _host(value: str) -> str | None:
    if not value or "," in value or "@" in value:
        return None
    try:
        parsed = urlsplit("//" + value)
        if parsed.path or parsed.query or parsed.fragment:
            return None
        host = parsed.hostname
        if (
            parsed.port is None
            and ":" in value
            and not value.endswith("]")
            and not value.count(":") == 1
        ):
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        if host is None or host.endswith("."):
            return None
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return None
        return host
    except ValueError:
        return None


def _same_origin(origin: str, valid_host: str, valid_port: int | None) -> bool:
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and not (parsed.path or parsed.query or parsed.fragment)
        and _host(parsed.netloc or "") == valid_host
        and (origin_port or 80) == (valid_port or 80)
    )


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        client = request.client.host if request.client else None
        try:
            peer = ipaddress.ip_address(client or "")
        except ValueError:
            peer = None
        raw_hosts = [
            value
            for name, value in request.scope.get("headers", [])
            if name.lower() == b"host"
        ]
        if len(raw_hosts) != 1:
            valid = None
        else:
            valid = _host(raw_hosts[0].decode("latin-1"))
        unsafe = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        preflight = (
            request.method == "OPTIONS" and "access-control-request-method" in headers
        )
        fetch = headers.get("sec-fetch-site")
        origin = headers.get("origin")
        ok = (
            peer is not None
            and peer.is_loopback
            and valid is not None
            and not any(
                k == "forwarded" or k.startswith("x-forwarded-") or k == "x-real-ip"
                for k in headers
            )
            and (fetch is None or fetch in {"same-origin", "none"})
            and (
                origin is None
                or _same_origin(
                    origin,
                    valid,
                    _host_port(raw_hosts[0].decode("latin-1")),
                )
            )
            and not preflight
            and (not unsafe or headers.get("x-careeros-write") == "1")
        )
        if not ok:
            response: Response = JSONResponse(
                {"detail": "Request rejected by local security policy."},
                status_code=403,
            )
        else:
            response = await call_next(request)
        for key, value in HEADERS.items():
            response.headers[key] = value
        return response


def _host_port(value: str) -> int | None:
    try:
        return urlsplit("//" + value).port
    except ValueError:
        return None
