"""Bounded, local extraction of verified résumé Materials.

This module is deliberately orchestration-only: source verification happens
before parsing, parsing happens before the M2 repository transaction, and the
repository remains the sole owner of extraction persistence.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, cast

from pypdf import Configuration, PdfReader, apply_configuration
from pypdf.errors import LimitReachedError, PdfReadError

from careeros.application_packages.service import open_verified_material
from careeros.database import resume_intelligence as repository
from careeros.resume_intelligence.coordinates import sha256_slice
from careeros.resume_intelligence.models import ResumeExtractionRun, ResumeTextSegment

SOURCE_MAX_BYTES = 8 * 1024 * 1024
TEXT_MAX_BYTES = 4 * 1024 * 1024
PDF_MAX_PAGES = 25
PDF_STREAM_MAX_BYTES = 8 * 1024 * 1024
PDF_PAGE_TREE_MAX_ENTRIES = 25
PDF_PAGE_TREE_MAX_DEPTH = 64
PDF_XFORM_MAX_INVOCATIONS = 1000
PDF_ROOT_RECOVERY_LIMIT = 1000
PARSER_VERSION = importlib.metadata.version("pypdf")
PDF_EXTRACTOR_IMPLEMENTATION = "careeros-m3-pdf-text-v1"
NORMALIZATION_POLICY = "lf-page-boundary-v1"
PDF_CONFIGURATION = {
    "maximum_declared_stream_length": PDF_STREAM_MAX_BYTES,
    "array_based_stream_maximum_output_length": PDF_STREAM_MAX_BYTES,
    "jbig2_maximum_output_length": PDF_STREAM_MAX_BYTES,
    "lzw_maximum_output_length": PDF_STREAM_MAX_BYTES,
    "run_length_maximum_output_length": PDF_STREAM_MAX_BYTES,
    "zlib_maximum_output_length": PDF_STREAM_MAX_BYTES,
    "image_maximum_buffer_size": PDF_STREAM_MAX_BYTES,
    "page_tree_maximum_entries": PDF_PAGE_TREE_MAX_ENTRIES,
    "page_tree_maximum_depth": PDF_PAGE_TREE_MAX_DEPTH,
    "xform_maximum_invocations_per_extraction": PDF_XFORM_MAX_INVOCATIONS,
}
POLICY_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "source_max_bytes": SOURCE_MAX_BYTES,
            "text_max_bytes": TEXT_MAX_BYTES,
            "pdf_max_pages": PDF_MAX_PAGES,
            "pdf_stream_max_bytes": PDF_STREAM_MAX_BYTES,
            "pdf_root_recovery_limit": PDF_ROOT_RECOVERY_LIMIT,
            "pdf_configuration": PDF_CONFIGURATION,
            "normalization": NORMALIZATION_POLICY,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


class ExtractionError(Exception):
    """Privacy-safe extraction failure with a bounded public code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VerifiedMaterialSource:
    material_id: str
    material_version_id: str
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    retired: bool
    data: bytes


@dataclass(frozen=True)
class ExtractionResult:
    source_format: Literal["pdf", "docx"]
    detected_format: Literal["pdf", "docx"]
    text: str
    segments: tuple[tuple[int, int, str], ...]
    warnings: tuple[str, ...]
    extractor_fingerprint: str
    policy_fingerprint: str
    cache_fingerprint: str

    @property
    def utf8_bytes(self) -> bytes:
        """The final immutable UTF-8 artifact addressed by segment coordinates."""
        return self.text.encode("utf-8")

    @property
    def text_sha256(self) -> str:
        """SHA-256 of the exact final UTF-8 artifact."""
        return hashlib.sha256(self.utf8_bytes).hexdigest()


def _error(code: str, message: str) -> ExtractionError:
    return ExtractionError(code, message)


def _format(filename: str, media_type: str, data: bytes) -> Literal["pdf", "docx"]:
    suffix = Path(filename).suffix.lower()
    media = media_type.lower().split(";", 1)[0].strip()
    if data.startswith(b"%PDF-"):
        if suffix not in ("", ".pdf") or media not in (
            "application/pdf",
            "application/octet-stream",
            "",
        ):
            raise _error("unsupported_format", "source metadata is not coherent")
        return "pdf"
    if (
        data.startswith(b"PK")
        and suffix == ".docx"
        and media
        in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/octet-stream",
            "",
        )
    ):
        return "docx"
    raise _error("unsupported_format", "source format is not supported")


def _read_fd(fd: int, expected_size: int) -> bytes:
    try:
        st = os.fstat(fd)
        if st.st_size != expected_size:
            raise _error("source_integrity_failure", "source object size is invalid")
        chunks: list[bytes] = []
        total = 0
        while total <= expected_size:
            chunk = os.read(fd, min(1024 * 1024, expected_size - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise _error("source_integrity_failure", "source object size changed")
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != expected_size:
            raise _error("source_integrity_failure", "source object is incomplete")
        return data
    except ExtractionError:
        raise
    except OSError as exc:
        raise _error(
            "source_integrity_failure", "source object is unavailable"
        ) from exc


def verify_material_source(
    connection: sqlite3.Connection,
    materials_root: Path,
    material_id: str,
    material_version_id: str,
) -> VerifiedMaterialSource:
    """Select one exact resume Material version and verify its immutable bytes."""
    try:
        row = connection.execute(
            """SELECT m.id AS material_id,m.category,m.status,v.id AS version_id,
               v.display_filename,v.declared_media_type,v.byte_size,v.sha256
               FROM material_versions v JOIN materials m ON m.id=v.material_id
               WHERE m.id=? AND v.id=?""",
            (material_id, material_version_id),
        ).fetchone()
    except Exception as exc:
        raise _error("source_not_found", "source selection failed") from exc
    if row is None:
        raise _error("source_not_found", "résumé source was not found")
    if row["category"] != "resume":
        raise _error("unsupported_source_type", "selected Material is not a résumé")
    filename = str(row["display_filename"])
    media_type = str(row["declared_media_type"])
    if not 1 <= len(filename) <= 160 or not 3 <= len(media_type) <= 127:
        raise _error("invalid_source_metadata", "source metadata is invalid")
    size = int(row["byte_size"])
    if not 1 <= size <= SOURCE_MAX_BYTES:
        raise _error("document_too_large", "source document exceeds the size limit")
    handle = open_verified_material(connection, materials_root, material_version_id)
    if handle is None:
        raise _error("source_integrity_failure", "source object could not be verified")
    try:
        data = _read_fd(handle.fd, size)
        if not handle.is_still_valid():
            raise _error("source_integrity_failure", "source object changed")
        handle.close()
    except ExtractionError:
        handle.close()
        raise
    except Exception as exc:
        handle.close()
        raise _error(
            "source_integrity_failure", "source object could not be verified"
        ) from exc
    if hashlib.sha256(data).hexdigest() != str(row["sha256"]):
        raise _error("source_integrity_failure", "source object hash does not match")
    _format(filename, media_type, data)
    return VerifiedMaterialSource(
        str(row["material_id"]),
        str(row["version_id"]),
        filename,
        media_type,
        size,
        str(row["sha256"]),
        str(row["status"]) == "retired",
        data,
    )


def _extract_pdf(
    data: bytes,
) -> tuple[str, tuple[tuple[int, int, str], ...], tuple[str, ...]]:
    try:
        configuration = Configuration(**cast(dict[str, Any], PDF_CONFIGURATION))
        with apply_configuration(configuration):
            reader = PdfReader(
                BytesIO(data),
                strict=True,
                root_object_recovery_limit=PDF_ROOT_RECOVERY_LIMIT,
            )
            if reader.is_encrypted:
                raise _error("encrypted_document", "encrypted PDF is not supported")
            if len(reader.pages) > PDF_MAX_PAGES:
                raise _error("document_too_large", "PDF page count exceeds the limit")
            pages: list[tuple[int, str]] = []
            text_bytes = 0
            for page_number, page in enumerate(reader.pages, 1):
                contents = page.get_contents()
                if (
                    contents is not None
                    and len(contents.get_data()) > PDF_STREAM_MAX_BYTES
                ):
                    raise _error(
                        "document_too_large", "PDF content stream exceeds the limit"
                    )
                try:
                    value = page.extract_text() or ""
                except Exception as exc:
                    raise _error(
                        "parser_failure", "PDF text extraction failed"
                    ) from exc
                value = value.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
                if value:
                    encoded_size = len(value.encode("utf-8"))
                    separator_size = 2 if pages else 0
                    if text_bytes + separator_size + encoded_size > TEXT_MAX_BYTES:
                        raise _error(
                            "document_too_large", "extracted text exceeds the limit"
                        )
                    text_bytes += separator_size + encoded_size
                    pages.append((page_number, value))
            text = "\n\n".join(value for _, value in pages)
    except ExtractionError:
        raise
    except LimitReachedError as exc:
        raise _error("document_too_large", "PDF resource limit exceeded") from exc
    except (PdfReadError, ValueError, TypeError, OSError) as exc:
        raise _error("malformed_document", "PDF structure is malformed") from exc
    except Exception as exc:
        raise _error("parser_failure", "PDF parsing failed") from exc
    raw = text.encode("utf-8")
    if not raw or not text.strip():
        raise _error(
            "no_extractable_text",
            "document has no extractable text; OCR is required",
        )
    if len(raw) > TEXT_MAX_BYTES:
        raise _error("document_too_large", "extracted text exceeds the limit")
    segments: list[tuple[int, int, str]] = []
    cursor = 0
    for index, (page_no, page_text) in enumerate(pages):
        encoded = page_text.encode("utf-8")
        locator = json.dumps(
            {"page": page_no, "type": "pdf_page"}, separators=(",", ":")
        )
        segments.append((cursor, cursor + len(encoded), locator))
        cursor += len(encoded) + (2 if index < len(pages) - 1 else 0)
    return text, tuple(segments), ()


def extract_verified_source(source: VerifiedMaterialSource) -> ExtractionResult:
    """Parse verified bytes in memory; no filesystem or network access occurs."""
    fmt = _format(source.filename, source.media_type, source.data)
    if fmt == "docx":
        from careeros.resume_intelligence.docx_extraction import (
            DOCX_EXTRACTOR_FINGERPRINT,
            DOCX_POLICY_FINGERPRINT,
            extract_docx,
        )

        result = extract_docx(source.data)
        cache_payload = {
            "source_version": source.material_version_id,
            "source_sha256": source.sha256,
            "declared_format": fmt,
            "detected_format": fmt,
            "extractor": DOCX_EXTRACTOR_FINGERPRINT,
            "policy": DOCX_POLICY_FINGERPRINT,
        }
        cache = hashlib.sha256(
            json.dumps(cache_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ExtractionResult(
            fmt,
            fmt,
            result.text,
            result.segments,
            result.warnings,
            DOCX_EXTRACTOR_FINGERPRINT,
            DOCX_POLICY_FINGERPRINT,
            cache,
        )
    text, pieces, warnings = _extract_pdf(source.data)
    extractor = (
        f"{PDF_EXTRACTOR_IMPLEMENTATION};pypdf={PARSER_VERSION};"
        f"normalization={NORMALIZATION_POLICY}"
    )
    cache_payload = {
        "source_version": source.material_version_id,
        "source_sha256": source.sha256,
        "declared_format": fmt,
        "detected_format": fmt,
        "extractor": extractor,
        "policy": POLICY_FINGERPRINT,
    }
    cache = hashlib.sha256(
        json.dumps(cache_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExtractionResult(
        fmt, fmt, text, pieces, warnings, extractor, POLICY_FINGERPRINT, cache
    )


def extract_material(
    connection: sqlite3.Connection,
    materials_root: Path,
    material_id: str,
    material_version_id: str,
) -> ResumeExtractionRun:
    """Verify, cache-check, parse, then atomically persist one extraction."""
    source = verify_material_source(
        connection, materials_root, material_id, material_version_id
    )
    cached = repository.find_successful_extraction(
        connection, _cache_for_source(source)
    )
    if cached is not None:
        return cached
    result = extract_verified_source(source)
    raw = result.utf8_bytes
    run_id = uuid.uuid4().hex
    run = ResumeExtractionRun(
        run_id,
        source.material_version_id,
        source.filename,
        source.media_type,
        source.byte_size,
        source.sha256,
        result.source_format,
        result.detected_format,
        "succeeded",
        result.text,
        result.text_sha256,
        len(result.segments),
        result.extractor_fingerprint,
        result.cache_fingerprint,
        datetime.now(UTC).isoformat(),
    )
    segments = tuple(
        ResumeTextSegment(run_id, i, start, end, locator, sha256_slice(raw, start, end))
        for i, (start, end, locator) in enumerate(result.segments)
    )
    try:
        return repository.persist_extraction(connection, run, segments)
    except repository.ResumeConflictError:
        cached = repository.find_successful_extraction(
            connection, result.cache_fingerprint
        )
        if cached is None:
            raise _error("persistence_conflict", "extraction cache conflict") from None
        return cached
    except repository.ResumeRepositoryError as exc:
        raise _error(
            "persistence_failure", "extraction could not be persisted"
        ) from exc


def _cache_for_source(source: VerifiedMaterialSource) -> str:
    fmt = _format(source.filename, source.media_type, source.data)
    if fmt == "docx":
        from careeros.resume_intelligence.docx_extraction import (
            DOCX_EXTRACTOR_FINGERPRINT,
            DOCX_POLICY_FINGERPRINT,
        )

        extractor = DOCX_EXTRACTOR_FINGERPRINT
        policy = DOCX_POLICY_FINGERPRINT
    else:
        extractor = (
            f"{PDF_EXTRACTOR_IMPLEMENTATION};pypdf={PARSER_VERSION};"
            f"normalization={NORMALIZATION_POLICY}"
        )
        policy = POLICY_FINGERPRINT
    payload = {
        "source_version": source.material_version_id,
        "source_sha256": source.sha256,
        "declared_format": fmt,
        "detected_format": fmt,
        "extractor": extractor,
        "policy": policy,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
