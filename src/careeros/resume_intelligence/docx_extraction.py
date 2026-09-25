"""Bounded, deterministic extraction of text from ordinary DOCX packages.

The reader intentionally implements only the small OOXML surface useful for a
resume.  It never extracts members to disk and performs all validation before
interpreting document XML.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import posixpath
import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import unquote

from defusedxml import ElementTree  # type: ignore[import-untyped]

from careeros.resume_intelligence.extraction import TEXT_MAX_BYTES, ExtractionError

DOCX_MAX_MEMBERS = 256
DOCX_MAX_TOTAL_UNCOMPRESSED = 16 * 1024 * 1024
DOCX_MAX_MEMBER_UNCOMPRESSED = 4 * 1024 * 1024
DOCX_MAX_COMPRESSION_RATIO = 100
DOCX_MAX_XML_ELEMENTS = 100_000
DOCX_MAX_XML_DEPTH = 64
DOCX_NORMALIZATION_POLICY = "docx-lf-block-v2"
DOCX_EXTRACTOR_IMPLEMENTATION = "careeros-m3-docx-text-v1"
DOCX_POLICY = {
    "max_members": DOCX_MAX_MEMBERS,
    "max_total_uncompressed": DOCX_MAX_TOTAL_UNCOMPRESSED,
    "max_member_uncompressed": DOCX_MAX_MEMBER_UNCOMPRESSED,
    "max_compression_ratio": DOCX_MAX_COMPRESSION_RATIO,
    "max_xml_elements": DOCX_MAX_XML_ELEMENTS,
    "max_xml_depth": DOCX_MAX_XML_DEPTH,
    "compression_methods": [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED],
    "normalization": DOCX_NORMALIZATION_POLICY,
    "part_order": "unique-headers-main-unique-footers",
    "tables": "paragraph-lf-cell-tab-row-lf",
    "external_hyperlinks": "display-text-only-never-fetch",
    "tracked_changes": "include-insertions-omit-deletions",
    "unsupported_constructs": "omit-with-bounded-warning",
}
DOCX_POLICY_FINGERPRINT = hashlib.sha256(
    json.dumps(DOCX_POLICY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
DOCX_EXTRACTOR_FINGERPRINT = (
    f"{DOCX_EXTRACTOR_IMPLEMENTATION};"
    f"defusedxml={importlib.metadata.version('defusedxml')};"
    f"python={platform.python_version()};zipfile=stdlib;"
    f"policy={DOCX_POLICY_FINGERPRINT}"
)

_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
_REL_NS = _NS["pr"]
_OFFICE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_HYPERLINK_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)
_EXT = re.compile(r"[\x00\\]|(^/)|(^[A-Za-z]:)|(^|/)(?:\.|\.\.)($|/)")


@dataclass(frozen=True)
class DocxExtraction:
    text: str
    segments: tuple[tuple[int, int, str], ...]
    warnings: tuple[str, ...]


class _Package:
    def __init__(self, data: bytes) -> None:
        try:
            self.zip = zipfile.ZipFile(BytesIO(data), "r")
            infos = self.zip.infolist()
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            raise ExtractionError(
                "malformed_document", "DOCX package is malformed"
            ) from exc
        if not infos or len(infos) > DOCX_MAX_MEMBERS:
            raise ExtractionError(
                "document_too_large", "DOCX package member limit exceeded"
            )
        self.infos: dict[str, zipfile.ZipInfo] = {}
        self.folded_names: set[str] = set()
        self.warnings: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            folded = name.casefold()
            if (
                name in self.infos
                or folded in self.folded_names
                or _EXT.search(name)
                or not name
                or name.endswith("/")
            ):
                raise ExtractionError(
                    "document_structurally_unsafe", "DOCX package member is unsafe"
                )
            if info.flag_bits & 0x1:
                raise ExtractionError(
                    "encrypted_document", "encrypted DOCX is not supported"
                )
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ExtractionError(
                    "document_structurally_unsafe", "DOCX compression is unsupported"
                )
            if info.file_size > DOCX_MAX_MEMBER_UNCOMPRESSED:
                raise ExtractionError(
                    "document_too_large", "DOCX member exceeds the size limit"
                )
            if info.file_size and (
                not info.compress_size
                or info.file_size > info.compress_size * DOCX_MAX_COMPRESSION_RATIO
            ):
                raise ExtractionError(
                    "document_too_large", "DOCX compression ratio exceeds the limit"
                )
            total += info.file_size
            if total > DOCX_MAX_TOTAL_UNCOMPRESSED:
                raise ExtractionError(
                    "document_too_large", "DOCX package exceeds the size limit"
                )
            self.infos[name] = info
            self.folded_names.add(folded)
        self._validate_package()

    def read(self, name: str) -> bytes:
        try:
            with self.zip.open(self.infos[name], "r") as handle:
                data = handle.read(DOCX_MAX_MEMBER_UNCOMPRESSED + 1)
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ExtractionError(
                "malformed_document", "DOCX member cannot be read"
            ) from exc
        if len(data) > DOCX_MAX_MEMBER_UNCOMPRESSED:
            raise ExtractionError(
                "document_too_large", "DOCX member exceeds the size limit"
            )
        return data

    def xml(self, name: str) -> Any:
        try:
            root = ElementTree.fromstring(self.read(name))
        except Exception as exc:
            raise ExtractionError(
                "malformed_document", "DOCX XML is malformed"
            ) from exc
        count = 0
        stack = [(root, 1)]
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > DOCX_MAX_XML_ELEMENTS or depth > DOCX_MAX_XML_DEPTH:
                raise ExtractionError("document_too_large", "DOCX XML limit exceeded")
            stack.extend((child, depth + 1) for child in node)
        for node in root.iter():
            local = node.tag.rsplit("}", 1)[-1]
            if local == "ins":
                self.warnings.add("tracked_insertions_included")
            elif local == "del":
                self.warnings.add("tracked_deletions_omitted")
            elif local in {"commentReference", "comments"}:
                self.warnings.add("comments_omitted")
            elif local in {
                "footnoteReference",
                "footnotes",
                "endnoteReference",
                "endnotes",
            }:
                self.warnings.add("footnotes_endnotes_omitted")
            elif local in {"drawing", "pict", "object"}:
                self.warnings.add("images_and_objects_omitted")
            elif local == "txbxContent":
                self.warnings.add("text_boxes_omitted")
        return root

    def _validate_package(self) -> None:
        required = {"[Content_Types].xml", "_rels/.rels"}
        if not required <= self.infos.keys():
            raise ExtractionError(
                "unsupported_format", "DOCX package structure is unsupported"
            )
        types = self.xml("[Content_Types].xml")
        overrides = types.findall("ct:Override", _NS)
        for override in overrides:
            lowered_content = override.attrib.get("ContentType", "").lower()
            if any(
                marker in lowered_content
                for marker in ("macroenabled", "vba", "oleobject", "activex")
            ):
                raise ExtractionError(
                    "document_structurally_unsafe", "macro or OLE DOCX is unsupported"
                )
        main_types = {
            item.attrib.get("ContentType", "")
            for item in overrides
            if item.attrib.get("PartName", "").lstrip("/") == "word/document.xml"
        }
        if main_types != {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        }:
            raise ExtractionError(
                "unsupported_format", "DOCX main document content type is invalid"
            )
        for override in overrides:
            part = override.attrib.get("PartName", "")
            if _EXT.search(part.lstrip("/")):
                raise ExtractionError(
                    "document_structurally_unsafe", "DOCX part name is unsafe"
                )
        for name in self.infos:
            lower = name.lower()
            if "customxml" in lower:
                self.warnings.add("unsupported_custom_xml_omitted")
            if any(token in lower for token in ("vba", "embeddings/", "activex")):
                raise ExtractionError(
                    "document_structurally_unsafe",
                    "DOCX package contains unsupported content",
                )
        self._rels("_rels/.rels", allow_hyperlinks=False)
        rels = self._relationships("_rels/.rels")
        office = [r for r in rels if r["type"] == _OFFICE_REL]
        if len(office) != 1:
            raise ExtractionError(
                "unsupported_format", "DOCX office document relationship is invalid"
            )
        self.document_part = self._resolve("", office[0]["target"])
        if (
            self.document_part not in self.infos
            or self.document_part != "word/document.xml"
        ):
            raise ExtractionError("unsupported_format", "DOCX main document is missing")
        # Parse every XML and relationship part so hostile parts fail closed.
        for name in self.infos:
            if name.endswith(".xml") or name.endswith(".rels"):
                self.xml(name)
        self._validate_all_relationships()

    def _validate_all_relationships(self) -> None:
        for name in self.infos:
            if not name.endswith(".rels"):
                continue
            for rel in self._relationships(name):
                if rel["target_mode"] == "External" and rel["type"] != _HYPERLINK_REL:
                    raise ExtractionError(
                        "document_structurally_unsafe",
                        "external DOCX resource is unsupported",
                    )
                if rel["target_mode"] != "External":
                    if (
                        rel["type"] == _HYPERLINK_REL
                        and len(rel["target"]) > 1
                        and rel["target"].startswith("#")
                    ):
                        continue
                    base = "" if name == "_rels/.rels" else name.rsplit("/_rels/", 1)[0]
                    self._resolve(base, rel["target"])

    def _relationships(self, name: str) -> list[dict[str, str]]:
        root = self.xml(name)
        result: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for rel in root.findall(f"{{{_REL_NS}}}Relationship"):
            values = {
                "id": rel.attrib.get("Id", ""),
                "type": rel.attrib.get("Type", ""),
                "target": rel.attrib.get("Target", ""),
                "target_mode": rel.attrib.get("TargetMode", "Internal"),
            }
            if (
                not values["id"]
                or values["id"] in seen_ids
                or not values["type"].startswith("http")
                or not values["target"]
                or values["target_mode"] not in {"Internal", "External"}
            ):
                raise ExtractionError(
                    "document_structurally_unsafe", "DOCX relationship is invalid"
                )
            seen_ids.add(values["id"])
            result.append(values)
        return result

    def _rels(self, name: str, *, allow_hyperlinks: bool) -> list[dict[str, str]]:
        rels = self._relationships(name)
        if not allow_hyperlinks and any(r["target_mode"] == "External" for r in rels):
            raise ExtractionError(
                "document_structurally_unsafe",
                "external package resource is unsupported",
            )
        return rels

    def _resolve(self, base: str, target: str) -> str:
        target = unquote(target)
        if not target or _EXT.search(target):
            raise ExtractionError(
                "document_structurally_unsafe", "DOCX relationship target is unsafe"
            )
        target = target.lstrip("/")
        path = posixpath.normpath(posixpath.join(base, target))
        if (
            path == "."
            or path.startswith("../")
            or "/../" in path
            or path not in self.infos
        ):
            raise ExtractionError(
                "document_structurally_unsafe", "DOCX relationship target is invalid"
            )
        return path


def _text(
    node: Any, warnings: set[str], package: _Package, relmap: dict[str, dict[str, str]]
) -> str:
    pieces: list[str] = []
    for child in node.iter():
        if child.tag == f"{{{_NS['w']}}}t":
            pieces.append(child.text or "")
        elif child.tag == f"{{{_NS['w']}}}tab":
            pieces.append("\t")
        elif child.tag == f"{{{_NS['w']}}}br":
            pieces.append("\n")
        elif child.tag == f"{{{_NS['w']}}}ins":
            warnings.add("tracked_insertions_included")
        elif child.tag == f"{{{_NS['w']}}}del":
            warnings.add("tracked_deletions_omitted")
        elif child.tag == f"{{{_NS['w']}}}hyperlink":
            rid = child.attrib.get(f"{{{_NS['r']}}}id")
            if rid and rid in relmap and relmap[rid]["target_mode"] == "External":
                warnings.add("external_hyperlink_not_fetched")
        elif child.tag in {
            f"{{{_NS['w']}}}commentReference",
            f"{{{_NS['w']}}}footnoteReference",
            f"{{{_NS['w']}}}endnoteReference",
        }:
            warnings.add("unsupported_reference_omitted")
        elif child.tag.rsplit("}", 1)[-1] in {
            "txbxContent",
            "drawing",
            "object",
            "pict",
        }:
            warnings.add("unsupported_construct_omitted")

    # Remove deletions and unsupported containers while preserving inline order.
    def walk(parent: Any) -> str:
        out = ""
        for child in parent:
            local = child.tag.rsplit("}", 1)[-1]
            if local in {"del", "drawing", "object", "pict", "txbxContent"}:
                continue
            if local == "t":
                out += child.text or ""
            elif local == "tab":
                out += "\t"
            elif local == "br":
                out += "\n"
            else:
                out += walk(child)
        return out

    return walk(node)


def extract_docx(data: bytes) -> DocxExtraction:
    package = _Package(data)
    warnings: set[str] = set(package.warnings)
    document = package.xml("word/document.xml")
    doc_rels = (
        {r["id"]: r for r in package._relationships("word/_rels/document.xml.rels")}
        if "word/_rels/document.xml.rels" in package.infos
        else {}
    )
    ordered_parts: list[tuple[str, str]] = []
    sect = document.findall(".//w:sectPr", _NS)
    headers = [
        x.attrib.get(f"{{{_NS['r']}}}id")
        for s in sect
        for x in s.findall("w:headerReference", _NS)
    ]
    footers = [
        x.attrib.get(f"{{{_NS['r']}}}id")
        for s in sect
        for x in s.findall("w:footerReference", _NS)
    ]
    seen: set[str] = set()
    for rid in headers + [None] + footers:
        if rid is None:
            ordered_parts.append(("word/document.xml", "main"))
            continue
        rel = doc_rels.get(rid)
        expected = _NS["r"] + "/" + ("header" if rid in headers else "footer")
        if rel is None or rel["target_mode"] != "Internal" or rel["type"] != expected:
            raise ExtractionError(
                "document_structurally_unsafe", "DOCX section relationship is invalid"
            )
        part = package._resolve("word", rel["target"])
        if part not in seen:
            seen.add(part)
            ordered_parts.insert(
                len([p for p, _ in ordered_parts if p != "word/document.xml"]),
                (part, "header" if rid in headers else "footer"),
            )
    # Ensure main sits between headers and footers, independent of section order.
    header_parts = [(p, k) for p, k in ordered_parts if k == "header"]
    footer_parts = [(p, k) for p, k in ordered_parts if k == "footer"]
    ordered_parts = header_parts + [("word/document.xml", "main")] + footer_parts
    blocks: list[tuple[str, str, int, str]] = []
    for part, role in ordered_parts:
        root = document if part == "word/document.xml" else package.xml(part)
        rels_name = posixpath.join(
            posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
        )
        relmap = (
            {r["id"]: r for r in package._relationships(rels_name)}
            if rels_name in package.infos
            else {}
        )
        index = 0
        body = root.find("w:body", _NS) if role == "main" else root
        if body is None:
            continue
        for child in body:
            local = child.tag.rsplit("}", 1)[-1]
            if local == "p":
                value = (
                    _text(child, warnings, package, relmap)
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                    .strip("\n")
                )
                if value.strip():
                    blocks.append((part, "paragraph", index, value))
            elif local == "tbl":
                rows: list[str] = []
                for row in child.findall("w:tr", _NS):
                    cells = []
                    for cell in row.findall("w:tc", _NS):
                        if any(
                            nested.tag.rsplit("}", 1)[-1] not in {"tcPr", "p"}
                            for nested in cell
                        ):
                            warnings.add("unsupported_construct_omitted")
                        paragraphs = [
                            _text(p, warnings, package, relmap)
                            .replace("\r\n", "\n")
                            .replace("\r", "\n")
                            .strip("\n")
                            for p in cell.findall("w:p", _NS)
                        ]
                        cells.append("\n".join(paragraphs))
                    rows.append("\t".join(cells))
                value = "\n".join(rows).strip("\n")
                if value.strip():
                    blocks.append((part, "table", index, value))
            elif local not in {"sectPr"}:
                warnings.add("unsupported_construct_omitted")
            index += 1
    if not blocks:
        raise ExtractionError("no_extractable_text", "DOCX has no extractable text")
    text = "\n\n".join(v for _, _, _, v in blocks)
    raw = text.encode("utf-8")
    if len(raw) > TEXT_MAX_BYTES:
        raise ExtractionError("document_too_large", "extracted text exceeds the limit")
    segments: list[tuple[int, int, str]] = []
    cursor = 0
    for i, (part, kind, index, value) in enumerate(blocks):
        encoded = value.encode("utf-8")
        locator = json.dumps(
            {"type": "docx_block", "part": part, "kind": kind, "index": index},
            separators=(",", ":"),
        )
        segments.append((cursor, cursor + len(encoded), locator))
        cursor += len(encoded) + (2 if i + 1 < len(blocks) else 0)
    return DocxExtraction(text, tuple(segments), tuple(sorted(warnings)))
