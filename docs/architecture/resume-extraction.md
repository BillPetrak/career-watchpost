# Résumé extraction

Career WatchPost extracts one explicitly selected, immutable résumé Material
version into one immutable UTF-8 text artifact. Selection binds both the
Material ID and exact version ID; before parsing or cache lookup the service
checks the managed Materials root, stored size, and SHA-256. Retired versions
remain valid provenance.

## Supported inputs and bounds

The local deterministic parser supports text-based PDF (`pypdf`) and ordinary
DOCX (bounded `zipfile` plus `defusedxml`). OCR/image-only PDF, legacy DOC,
RTF, ODT, HTML, images, encrypted files, macro/OLE packages, URLs, and cloud
documents are unsupported. Source bytes are limited to 8 MiB and extracted
UTF-8 bytes to 4 MiB. PDF input is limited to 25 pages. DOCX accepts at most
256 members, 16 MiB uncompressed total, 4 MiB per member, 100:1 compression,
and stored/deflated members only. XML is bounded to 100,000 elements and depth
64. PDF object, stream, page-tree, and form-invocation limits are enforced by
the isolated parser configuration.

Parsing finishes before persistence opens its `BEGIN IMMEDIATE` transaction.
Line endings are normalized to LF; non-empty blocks are joined with two
newlines. DOCX output preserves paragraphs and tables, uses tabs for cells,
and follows referenced header/body/footer order. Hyperlink display text is
kept without fetching targets; unsupported constructs produce bounded,
deterministic warnings. No network, OCR, model, PII detection, sanitization,
or fact inference occurs here.

## Provenance and cache identity

Segments use zero-based, half-open UTF-8 byte offsets and canonical locators
(for example a PDF page or DOCX block). Locators contain no path or source
text. The cache fingerprint binds the exact Material version and source hash,
format, parser/runtime fingerprints, normalization policy, and all resource
bounds. A cache hit is considered only after source integrity verification.
Concurrent persistence is accepted only when the same successful cache
identity can be reread. Extraction warnings are returned in memory; schema 14
does not persist them.

The implementation lives in `src/careeros/resume_intelligence/extraction.py`;
the workspace contract is described in
[`career-profile-workspace.md`](career-profile-workspace.md).
