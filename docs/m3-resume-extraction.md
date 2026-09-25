# Product 1 v1.05 M3 résumé extraction

M3 converts one explicitly selected, immutable résumé Material version into
one immutable extracted-text artifact. Selection is by both `material_id` and
exact `material_version_id`; the source object is checked against the bound
Materials root, stored byte size, and stored SHA-256 before parsing or cache
lookup. Historical versions of retired Materials remain valid provenance.

The parser is local and deterministic. M3 supports text-based PDF files using
the pure-Python `pypdf` reader and ordinary DOCX files using a narrow
standard-library `zipfile` + `defusedxml` OOXML reader. OCR/image-only PDFs,
legacy `.doc`, RTF, ODT, HTML, images, encrypted documents, macro/OLE packages,
remote URLs, and cloud documents are unsupported. M3 stores extracted text
only; it does not detect PII, sanitize text, infer facts, call models, or
provide Profile UI/API/CLI workflows.

The bounds are 8 MiB source bytes and 4 MiB extracted UTF-8 bytes. PDF has a
25-page limit; DOCX has at most 256 members, 16 MiB total uncompressed data,
4 MiB per member, a 100:1 compression ratio, and only stored/deflated ZIP
members. XML is limited to 100,000 elements and depth 64. pypdf is run under
an isolated configuration limiting declared,
decompressed, array, image, JBIG2/LZW/RunLength/zlib streams to 8 MiB,
limiting page trees to 25 entries and depth 64, limiting form invocations
to 1,000 per extraction, and limiting root-object recovery to 1,000. Each
page content stream is checked against the 8 MiB limit before text extraction.
Parser work is complete before `persist_extraction` opens its M2
`BEGIN IMMEDIATE` transaction. Page and DOCX block output normalizes line
endings to LF, trims only parser-boundary newlines, and joins nonempty blocks
with `\n\n`. DOCX order is unique referenced headers, main body, then unique
referenced footers. Paragraphs and tables are preserved; table cells use tabs
and rows use LF. Hyperlink display text is retained without fetching targets;
internal bookmark targets remain local and unresolved, and other external
relationships are rejected. Tracked insertions are included
and deletions omitted with bounded warnings. Comments, notes, text boxes,
images, OLE, macros, and other unsupported constructs are omitted with
warnings. Segments use zero-based half-open UTF-8 byte offsets and canonical
JSON locators such as `{"type":"docx_block","part":"word/document.xml","kind":"paragraph","index":0}`.
Locators contain no filesystem path or source text.

Warnings are bounded, deterministic extraction diagnostics returned with the
in-memory result. Schema v13 deliberately has no extraction-warning column, so
M3 does not persist them or change the schema solely to add one.

The cache identity binds the exact Material version and source hash, declared
and detected format, parser runtime/version, normalization policy, and all
resource-policy bounds. DOCX additionally binds the `defusedxml` and Python/
`zipfile` runtime fingerprint and its complete policy fingerprint. A cache hit
is considered only after source integrity verification. A concurrent persistence conflict is accepted only when the
same successful cache identity can then be read.

Owner learning opportunities: trace one exact Material version from its
database metadata through the bound object descriptor and hash check; then
trace one PDF page through final UTF-8 offsets and `resume_text_segments`.
