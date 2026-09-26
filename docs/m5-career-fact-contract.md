# Product 1 v1.05 M5 deterministic career-fact contract

This document is the frozen implementation authority for M5. M5 is a local,
deterministic structural extractor over the exact successful M3 extracted
artifact. It does not call a model, network, provider, embedding service, or
M4 approval workflow. M6 is the authority for human review and construction of
an authoritative Candidate Profile.

## Vocabulary and scope

The only persisted fact types are `identity.name`, `contact.email`,
`contact.phone`, `portfolio_reference`, `skill`, `employment`, `education`,
`certification`, and `language`. Section headings are parser structure only;
there is no standalone `date_range` fact. M5 produces only CareerFactRun,
CareerFactCandidate, and CareerFactEvidence. It never mutates Matching
Preferences, CandidateProfile/ranking state, learned preferences, or adaptive
state. Model-assisted extraction and all API, CLI, and web workflow are outside
this contract.

## Input and authority

The input is exactly one immutable successful M3 run with
`mode=deterministic`, `input_kind=extracted`, and `sanitization_run_id=None`.
The input is UTF-8 text of at most 4 MiB. M4 sanitization fingerprints,
approval, and sanitized text are not M5 authority. A successful empty M3 text
artifact produces a successful empty fact aggregate; failed, mismatched, or
over-bound artifacts fail closed.

## Display, comparison, and date/URL normalization

Display (D) preserves authored content: trim surrounding Unicode whitespace,
normalize CRLF/CR to LF at parser boundaries, and otherwise retain spelling,
case, punctuation, accents, and internal spacing. Canonical JSON has no
presentation whitespace. Comparison (K) applies Unicode NFKC, casefolds,
trims, converts runs of Unicode whitespace to one ASCII space, and treats
ASCII punctuation separators (`- _ / , ; : |`) as spaces before collapsing
spaces. K never adds synonyms, aliases, seniority, proficiency, or inferred
meaning. Email comparison applies NFKC plus casefold while retaining every
email punctuation character. Phone comparison retains a leading `+` and the
NFKC-normalized digits only. A normalized key is the fact-type prefix plus
canonical JSON of the type-specific comparison object.

KD compares dates only after accepting complete ISO-like year (`YYYY`),
year-month (`YYYY-MM`), or date (`YYYY-MM-DD`) tokens. It canonicalizes to the
same precision (`YYYY`, `YYYY-MM`, or `YYYY-MM-DD`) and never infers missing
precision, chronology, duration, or years of experience. U comparison lowercases
the scheme/host, removes a default port and fragment, preserves path/query
meaning, and removes only one trailing slash from a non-root path. Arbitrary
URL discovery is not performed.

## Canonical values and structural gates

The executable structural grammar is intentionally small and line-bound. Label
values use the M4 line grammar `(?im)^\s*LABEL\s*[:=-]\s*([^\r\n]+)`;
recognized labels are `Name|Full name|Preferred name|Candidate`,
`Email|E-mail`, `Phone|Mobile|Telephone`, and
`Portfolio|Website|Personal site|GitHub`. Recognized list headings are
`Skills|Technical skills|Core skills`, `Languages`, and
`Certifications|Certificates`. A heading may carry its list after `:`, `=`, or
`-` on the same line, or on subsequent non-empty lines until the next
recognized heading/section. Items are split only on comma, semicolon, vertical
bar, `•`, or `·` (a leading list `-` or `*` is decoration, not a separator).
Empty items are omitted. A language item containing parentheses, brackets, or
a colon is ambiguous (often a proficiency annotation) and is omitted rather
than partially interpreted.

Experience/employment headings are exactly `Experience|Employment|Work
experience`; education is exactly `Education`. Only records prefixed by `-`
or `•` are considered. Their title/company or degree/institution pair must be
separated by one explicit `at`, `|`, `–`, or `—` delimiter. Complete date
tokens are exactly `YYYY`, `YYYY-MM`, or `YYYY-MM-DD`; an optional range uses
one `-`, `–`, or `—` separator and preserves each token's precision. The date
hyphens are part of the token and are never split as delimiters. Certification
items require a non-empty name; issuer is retained only after explicit `at`,
`–`, or `—`, and date only after the complete date grammar. No other line or
prose is interpreted.

Canonical values are JSON objects with only the fields shown below. Optional
fields are omitted rather than guessed. The first occurrence (byte order)
chooses display spelling and optional display fields; later exact duplicates
only add evidence.

* `identity.name`: `{"name": "..."}` from a recognized `Name`, `Full name`,
  `Preferred name`, or `Candidate` line, when the M4 name-label grammar accepts
  one to five Unicode letter/mark tokens. Ambiguous labels are omitted.
* `contact.email`: `{"email": "..."}` from the line-anchored M4 grammar
  `(?im)^\s*(?:e-?mail)\s*[:=-]\s*([^\r\n]+)`. The complete trimmed value
  must equal one supported M4 ASCII/Unicode email finding range. Unlabelled
  findings, prose, multiple addresses, and ambiguous labels are omitted.
  This is a résumé-authored email appearing as the complete value of an
  explicit email/e-mail label and is emitted as a **NON-AUTHORITATIVE contact
  candidate for M6 review**. The structural match does **not** establish that
  the address belongs to the résumé subject.
* `contact.phone`: `{"phone": "..."}` from an explicit `Phone`, `Mobile`, or
  `Telephone` line whose complete trimmed value is one supported M4 phone
  finding. Unlabelled phone findings are omitted.
* `portfolio_reference`: `{"url": "..."}` from an explicit `Portfolio`,
  `Website`, `Personal site`, or `GitHub` label and one complete supported URL
  value. It is not inferred from arbitrary URL text.
* `skill`: `{"name": "..."}` from a `Skills`, `Technical skills`, or
  `Core skills` heading/list line. Items are separated only by commas,
  semicolons, bullets, or vertical bars; blank/ambiguous items are omitted.
* `language`: `{"name": "..."}` from a `Languages` heading/list line using
  the same explicit separators. Proficiency is never inferred or persisted.
* `employment`: `{"title":"...","company":"...","start_date":"...",
  "end_date":"..."}` from an explicitly labelled `Experience`,
  `Employment`, or `Work experience` block. A record requires one unambiguous
  title/company pair separated by ` at `, ` — `, or ` | ` and optional complete
  date tokens. Unlabelled prose is not interpreted.
* `education`: `{"degree":"...","institution":"...","start_date":"...",
  "end_date":"..."}` from an explicitly labelled `Education` block, with
  the same conservative pairing and date grammar as employment. Unlabelled
  institution/degree prose is omitted.
* `certification`: `{"name":"...","issuer":"...","date":"..."}` from
  a `Certifications` or `Certificates` heading/list. A list item is accepted
  only when it has a non-empty certification name; issuer/date are retained
  only when explicitly delimited by ` — `, ` at `, or a complete date token.

Ambiguous structures emit no candidate. No free-prose achievements, projects,
publications, summary extraction, location, technology aliases, proficiency,
seniority, chronology, or semantic inference is allowed. Date ranges are
fields of employment/education/certification only, never facts themselves.

## Deduplication, evidence, and ordering

Each occurrence keeps D display content, a type-specific K/KD/U normalized key,
and a duplicate signature over the semantic canonical value. Only exact
duplicate-equivalent occurrences merge; similar or conflicting values remain
distinct. Composite facts retain separate evidence ranges for each field.
Evidence is `coordinate_space="extracted"`, zero-based half-open UTF-8 byte
ranges into the exact M3 raw text, with the SHA-256 of that exact slice.
Identical `(start_byte,end_byte,slice_sha256)` values are deduplicated, then
sorted by `(start_byte,end_byte,slice_sha256)` and assigned ordinals from zero.
Discontinuous evidence is never widened. Candidates are ordered by the frozen
nine-type taxonomy order above, then normalized key, canonical value JSON, and
candidate ID; evidence is ordered by candidate ID then ordinal. Confidence is
always exactly `1.0` and origin exactly
`deterministic`: this means only that the frozen structural rule matched; it
never means truth, verification, ownership, validity, or authority.

## Candidate identity and fingerprints

The frozen constants are:

`FACT_SCHEMA_VERSION = "careeros-career-fact-schema-v1"`

`FACT_SCHEMA_FINGERPRINT = "f49c008962f1591f498708825f664d13434600a0a079c0557bc075e0217f9e3f"`

`DETERMINISTIC_EXTRACTOR_VERSION = "careeros-m5-deterministic-career-facts-v1"`

`DETERMINISTIC_EXTRACTOR_FINGERPRINT = "0882205196f757b28c54d6d7ce27824cf052b7b65c9642009def87b062be3e04"`

The candidate identity object is exactly:

```json
{"extraction_run_id":"<exact M3 extraction run ID>","extractor_fingerprint":"<64 lowercase hex>","fact_type":"<exact fact_type>","input_byte_size":123,"input_sha256":"<64 lowercase hex>","mode":"deterministic","normalized_key":"<exact key>","schema_fingerprint":"<64 lowercase hex>","semantic_value":{},"version":1}
```

In object form (the same object and keys):

```json
{
  "extraction_run_id": "<exact M3 extraction run ID>",
  "extractor_fingerprint": "<64 lowercase hex>",
  "fact_type": "<exact fact_type>",
  "input_byte_size": 123,
  "input_sha256": "<64 lowercase hex>",
  "mode": "deterministic",
  "normalized_key": "<exact key>",
  "schema_fingerprint": "<64 lowercase hex>",
  "semantic_value": {},
  "version": 1
}
```

Its canonical serialization is
`json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`.
Candidate ID is `SHA-256(canonical_json(identity_object).encode("utf-8")).hexdigest()[:32]`.
The identity includes exact `extraction_run_id`; it excludes random fact-run
ID, timestamps, evidence coordinates/count, confidence, and display spelling.
Thus same extraction/candidate/contract is stable, a different extraction run
is different even for byte-identical text, and semantic changes are different.

The fact-run cache fingerprint binds exact extraction ID, input SHA-256 and
byte size, `input_kind=extracted`, `mode=deterministic`, extractor and schema
fingerprints, and version. Cache hits are authoritative only after complete
aggregate revalidation: run fields/outcome, input identity, fingerprints,
candidate shape/normalized key/canonical JSON/value hash/recomputed ID,
evidence cardinality/ordinals/bounds/UTF-8 boundaries/slice hashes, and all
aggregate limits. Failed, malformed, truncated, or tampered aggregates never
win and are never silently repaired. Schema-v13 repository transactions own
persistence integrity; no migration is introduced.

## Bounds and future separation

Global limits are input 4 MiB, 1,000 candidates, 1,000 evidence rows per
candidate, and 10,000 evidence rows total. Every emitted display field is
limited to 512 UTF-8 bytes, every normalized key to 512 Unicode code points,
and every canonical value JSON string to 100,000 Unicode code points, matching
the schema-v13 column contract. Per-type limits are name 8, email
32, phone 32, portfolio 64, skill 256, employment 128, education 64,
certification 128, and language 64. Any exceeded bound fails the aggregate
before persistence; no truncation or partial success is allowed. A failed run
may be persisted with zero artifacts. M6 alone may approve/reject/edit a
candidate and create authoritative Candidate Profile values. Model-assisted extraction may add
model extraction under its own sanitized-input authority. Any behavior change
after this freeze increments the relevant version/fingerprint.

The M4 detector's own `MAX_FINDINGS` bound remains authoritative for contact
and name/phone equality checks; M5 requests the bounded detector once and does
not widen or reinterpret it. Every M5 per-type limit is checked before any
candidate or evidence object is constructed, and an over-limit field fails the
whole aggregate.
