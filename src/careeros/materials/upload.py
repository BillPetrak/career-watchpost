"""Strict raw ASGI upload validation primitives."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote_to_bytes

MAX_UPLOAD = 25 * 1024 * 1024
_ADMISSION = threading.Lock()


class UploadHeaderError(ValueError):
    """Malformed/duplicate transport headers (HTTP 400)."""


class UploadMetadataError(ValueError):
    """Malformed encoded metadata (HTTP 422)."""


@contextmanager
def upload_admission() -> Iterator[None]:
    """Nonwaiting process-local admission slot for browser uploads."""
    if not _ADMISSION.acquire(blocking=False):
        raise RuntimeError("upload admission busy")
    try:
        yield
    finally:
        _ADMISSION.release()


_META_LIMITS = {
    "x-careeros-material-name": 1920,
    "x-careeros-material-category": 32,
    "x-careeros-display-filename": 3072,
    "x-careeros-declared-media-type": 127,
    "x-careeros-material-revision": 20,
}


def raw_headers(scope: Mapping[str, Any]) -> dict[str, list[bytes]]:
    result: dict[str, list[bytes]] = {}
    for key, value in scope.get("headers", []):
        name = key.decode("ascii", "strict").lower()
        result.setdefault(name, []).append(value)
    return result


def strict_percent(value: bytes, limit: int) -> str:
    if len(value) > limit or any(byte > 0x7F for byte in value):
        raise UploadHeaderError("bounded metadata required")
    if re.search(rb"%(?![0-9A-Fa-f]{2})", value):
        raise UploadMetadataError("malformed metadata escape")
    try:
        return unquote_to_bytes(value.decode("ascii")).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise UploadMetadataError("invalid metadata encoding") from exc


def _raw_ascii(value: bytes, limit: int) -> str:
    if len(value) > limit or any(byte > 0x7F for byte in value):
        raise UploadHeaderError("bounded metadata required")
    return value.decode("ascii")


def validate_scope(scope: Mapping[str, Any]) -> tuple[dict[str, str], int | None]:
    headers = raw_headers(scope)
    out: dict[str, str] = {}
    for key, limit in _META_LIMITS.items():
        values = headers.get(key, [])
        if len(values) > 1:
            raise UploadHeaderError("duplicate metadata")
        if values:
            if key in {
                "x-careeros-material-name",
                "x-careeros-display-filename",
            }:
                out[key] = strict_percent(values[0], limit)
            else:
                out[key] = _raw_ascii(values[0], limit)
    for transport in ("content-type", "content-encoding"):
        if len(headers.get(transport, [])) > 1:
            raise UploadHeaderError("duplicate transport header")
    lengths = headers.get("content-length", [])
    if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
        raise UploadHeaderError("invalid content length")
    length = int(lengths[0]) if lengths else None
    return out, length
