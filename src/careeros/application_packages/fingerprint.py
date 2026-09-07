"""Stable semantic fingerprints for package job snapshots."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping

_FIELDS = (
    "source",
    "source_job_id",
    "title",
    "company",
    "location",
    "description",
    "url",
    "employment_type",
    "remote_status",
    "published_at",
)


def job_content_fingerprint(
    job: Mapping[str, str | None] | None = None, **fields: str | None
) -> str:
    """Return the canonical SHA-256 fingerprint of a semantic job tuple.

    Values are length-prefixed UTF-8 and use an explicit null marker, making
    ``None`` distinct from an empty string while retaining field order.
    """
    values = dict(job or {})
    values.update(fields)
    digest = hashlib.sha256(b"careeros:m3:job-fingerprint\0")
    for name in _FIELDS:
        value = values.get(name)
        if value is None:
            digest.update(b"\xff\xff\xff\xff")
            continue
        encoded = value.encode("utf-8")
        digest.update(struct.pack(">I", len(encoded)))
        digest.update(encoded)
    return digest.hexdigest()
