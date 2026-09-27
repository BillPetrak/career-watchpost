# Product 1 v1.05 M7 Career Profile workspace contract

Status: owner-approved implementation authority for M7.

M7 exposes the closed M3 deterministic résumé extraction, closed M5
deterministic Career Fact authority, and closed M6 human-review and immutable
Career Profile authority through one local CLI/API/web workspace. It changes no
M3, M5, or M6 semantics. `SCHEMA_VERSION` remains 13 and M7 adds no dependency,
model/provider/network call, migration, Matching Profile mutation, Matching
Preferences mutation, ranking/feedback/adaptive mutation, or external
publication.

## Product vocabulary and boundary

- **Career Profile** is the immutable schema-13 M6 authority.
- **Matching Profile** is the existing file-backed `CandidateProfile` used by
  ranking.
- **Matching Preferences** are the existing explicit and learned ranking
  authority.

The existing `careeros profile` family remains the Matching Profile file
surface. M7 adds `careeros career-profile`; the web label is **Career Profile**.
Reviewing or saving Career Profile authority never changes either matching
surface, ranking, direct review feedback, or adaptive state.

## Closed services and M7 orchestration

M7 calls, rather than reimplements:

- M3 `extract_material` for one explicit Material ID and immutable version ID;
- M5 `run_deterministic_facts` and
  `validate_recognized_fact_authority`;
- M6 `effective_candidate_review`, `candidate_review_history`,
  `candidate_review_history_page`,
  `review_candidate`, `assemble_profile`, `current_profile`,
  `profile_history`, and `profile_snapshot`.

The explicit Analyze operation verifies the managed object/root binding through
M3, calls M3, then deterministic M5 under existing write coordination. It does
not depend on M4 sanitization and never runs merely because the workspace is
opened. Existing M3/M5 cache validation remains authoritative; later source
corruption cannot be hidden by a cached success. A successful empty M5 run is a
valid visible result.

M7's shared `careeros.career_profile.workspace` layer is presentation-safe
orchestration and projection only. Bounded SQL may select lineage IDs, counts,
and pages, but every exposed authority-bearing value is validated by closed
M5/M6 services. Pagination never makes a malformed aggregate appear valid.

## Résumé and run selection

There is no current-résumé singleton. Navigation is explicit:

```text
résumé Material
    -> immutable Material version
    -> M3 extraction
    -> recognized deterministic M5 run
```

Only `resume` Materials participate. Active and retired immutable lineage may
be inspected. M7 never substitutes another version or run, and never presents a
failed, model, sanitized-input, unknown-fingerprint, or corrupt run as
recognized deterministic authority. Profile assembly may deliberately combine
approved candidates from multiple recognized runs and Material versions.

## Candidate review and evidence

An unreviewed candidate is machine-produced and non-authoritative. M7 exposes
all legal M6 transitions: `approved`, `approved_edit`, and `rejected`, including
later re-review. `approved_edit` changes only effective authority; the original
candidate and evidence remain immutable. Later rejection prevents the
candidate from sourcing a future version but never changes historical Career
Profile snapshots. Review notes are absent from all M7 surfaces and remain
audit-only lower-layer authority.

Candidate detail returns two independently paginated nested envelopes:

- `evidence`: `items`, `total`, `offset`, `limit`;
- `review_history`: `items`, `total`, `offset`, `limit`.

Review-history pages are provided by the M6 paginated read seam. Every event
returned on the requested page is M6-validated, and all persisted events remain
reachable by requesting the appropriate page. A requested page does not assert
the integrity of unrequested pages. The three M6 read capabilities remain
distinct: paged history validates only the requested page, complete history
(`candidate_review_history`) validates every event, and effective authority
(`effective_candidate_review`) remains the owner of the validated latest/effective
review state.

Query defaults are `evidence_offset=0`, `evidence_limit=20`,
`history_offset=0`, and `history_limit=20`. Offsets must be non-negative and
limits must be 1 through 100. Every persisted record remains pageable; no
authority is silently truncated.

Before evidence is projected, M7 validates the complete recognized M5
aggregate. Each item is the exact persisted zero-based half-open UTF-8 byte
slice from the immutable M3 raw artifact, with UTF-8 boundaries and SHA-256
verified by authority validation. No surrounding context, arbitrary byte
window, or full extracted text is returned. A source label is derived only
from a recognized M3 segment locator: `PDF page N`, or a bounded DOCX label
from recognized `type`, `kind`, and `index`. Raw DOCX `part` strings are never
displayed. Malformed or unrecognized locator shapes fail closed.

M7 may return `merge_eligible_candidate_ids` and
`merge_eligible_count` as non-authoritative presentation hints. They are
computed only from fully validated effective approved authority and exact full
M5 normalized-key equality. Normalized keys and any token derived from them
remain private server internals. Hints never auto-merge, establish identity, or
serve as write authority; saves submit explicit candidate IDs and M6 rechecks
compatibility. Hint discovery is bounded to 100 relevant latest-approved
candidates per projection; absence from this optional bounded hint is never
write authority or proof of incompatibility.

## CLI contract

M7 provides:

```text
careeros career-profile sources
careeros career-profile analyze MATERIAL_ID VERSION_ID
careeros career-profile candidates FACT_RUN_ID
careeros career-profile candidate CANDIDATE_ID
careeros career-profile review CANDIDATE_ID DECISION
careeros career-profile show [--version VERSION_ID]
careeros career-profile history
careeros career-profile save MANIFEST --expected-revision N
```

Reads and writes accept `--database`; Analyze alone accepts
`--materials-root`. Reads use strict read-only connections. Writes use the
collection lock; contention exits 75. Invalid command/manifest input exits 2,
domain/database/authority failures exit 1, and stale profile revision exits 1
while exposing only the current revision. Read commands support text and JSON.
`approved_edit` requires `--value-file`, never a sensitive argv value. Save
uses a JSON manifest whose ordered `entries` may be empty and may contain only
candidate/source/identity/privacy choices or current manual/imported logical
IDs. It cannot provide authoritative values, hashes, normalized keys, fact
types, origins, or sort orders. File and parse errors never expose file paths.

CLI output is explicitly private and may show candidate/profile values, but
never storage keys, Materials-root paths, complete raw text, notes, normalized
keys, or unnecessary hashes.

## HTTP contract

M7 exposes:

```text
GET  /career-profile/sources
POST /career-profile/sources/{material_id}/versions/{version_id}/analyze
GET  /career-profile/fact-runs/{run_id}/candidates
GET  /career-profile/candidates/{candidate_id}
PUT  /career-profile/candidates/{candidate_id}/review
GET  /career-profile/current
GET  /career-profile/history
GET  /career-profile/versions/{version_id}
POST /career-profile/versions
```

Reads use strict `mode=ro`, take no write lock, and never create missing state.
Writes use existing writable/collection-lock dependencies; closed services own
their persistence transactions. Request models forbid extra fields.

A profile save supplies `expected_revision` and a complete ordered list. A
candidate entry supplies only kind, primary candidate ID, explicit supporting
candidate IDs, optional existing logical ID, distinct-identity choice, and
optional privacy override. An existing manual/imported entry supplies only
kind, logical ID, and optional privacy override. No request supplies fact
value, normalized key, hash, fact type, origin, or sort order.

Missing resources map to privacy-safe 404 responses. Invalid requests/domain
input map to privacy-safe 422 responses. Revision conflicts map to 409 with
only `career_profile_revision_conflict` and `current_revision`; recognized M5
authority races also map to safe 409. Corrupt/unavailable authority maps to a
fixed safe 500, and lock/SQLite contention retains existing 503 behavior.

For every path under `/career-profile`, FastAPI request validation returns only:

```json
{"detail":{"code":"invalid_career_profile_request"}}
```

It never echoes submitted input, edited values, body fragments, keys,
filenames, paths, or exceptions. Unrelated routes retain their existing
validation behavior. Existing loopback/same-origin security, explicit write
intent, and `Cache-Control: no-store` apply.

## Web workspace

The existing zero-build HTML/CSS/vanilla-JavaScript application adds top-level
`#career-profile` navigation and three internal panels: **Career Facts**,
**Current Profile**, and **Profile History**. No frontend dependency, build
tool, or SPA rewrite is introduced.

Selected source/version/run, candidate pages/details, assembly draft, and a
preserved stale-CAS draft are memory-only. Career Profile values and drafts are
never written to localStorage, sessionStorage, or IndexedDB. Re-entry refetches
server authority. Existing request-generation, operation-identity,
duplicate-click, `aria-busy`, disabled-control, safe-DOM, live-status, focus,
and narrow-layout conventions apply. Writes are never optimistic. Ordering uses
keyboard-accessible Up/Down controls rather than drag-only interaction.

Candidate detail visibly distinguishes immutable original value from effective
authority and shows safe provenance, paginated exact evidence, and paginated
note-free review history. Approved-edit forms gather type-specific fields but
do not implement M5/M6 semantic validation; the server serializes canonical
project JSON and M6 remains final authority.

## Complete profile assembly and CAS

The assembly draft begins from the current immutable snapshot and is never
saved by a review action. The user deliberately controls inclusion/removal,
primary and supporting candidates, existing identity target, explicit
distinct/new identity when required, ordering, and version-local privacy.
Equal effective keys are shown as merge eligible but are never auto-selected;
different effective keys remain distinct/conflicting. Manual/imported facts may
only be carried from the current profile, reordered, removed, or assigned a new
version-local privacy class. M7 adds no manual/imported authoring.

An empty complete snapshot and a deliberate semantic no-op save are valid. The
save confirmation shows the observed/base revision and ordered fact count.
Every successful save creates a new immutable version through M6.

The current read supplies the observed revision, which CLI/web return unchanged
as `expected_revision`. On 409, the browser reports the conflict, keeps the
submitted draft in memory, performs no replay, refetches current
profile/candidate/history authority, marks invalid entries, and offers an
explicit **Reapply preserved draft** action. The user must reconcile and press
Save again. Candidate review has no M6 revision input; M7 does not invent one.

## Privacy and corruption

This is an explicitly private loopback workspace. Candidate/profile values,
including name, email, and phone, are visible by default in candidate detail,
current/history detail, and explicit CLI output. Privacy classification remains
visible and version-local and never implies publication.

M7 never exposes filesystem or Materials-root paths, storage keys, full raw
résumé text, evidence context, audit notes, normalized keys, request bodies,
submitted values in validation errors, values in URLs/diagnostics/analytics,
or irrelevant provider/model metadata. There is no external transmission,
analytics, cloud synchronization, or publication.

Corrupt selected M5 authority blocks its candidate/evidence/review surface;
malformed effective review authority blocks that candidate's review; corrupt
Career Profile history blocks current/history/assembly and all profile saves.
Independent valid fact authority may remain inspectable. Errors use fixed safe
codes. M7 never repairs, reorders, normalizes, deduplicates, or rewrites corrupt
authority.

## Frozen exclusions

M7 adds no fact types, extraction semantics, model/LLM/provider/network work,
model-assisted extraction behavior, desired-role inference, Career Profile-to-ranking conversion,
Matching Profile or Matching Preferences mutation, ranking/feedback/adaptive
change, new manual/imported authoring, notes UI, document preview, silent merge,
autosave, external publication, job source, Product 1 v1.0 change, or
CareerOS-learning change.
