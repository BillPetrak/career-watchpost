# Product 1 v1.05 M6 Career Profile contract

Status: owner-approved implementation authority for M6.

M6 turns immutable M5 career-fact candidates into human-reviewed authority and
then into complete, immutable Career Profile snapshots:

```text
human review events
    -> effective candidate authority
    -> deliberate complete profile assembly
    -> immutable Career Profile versions with CAS
```

M6 adds no API, CLI, web UI, model/provider/network call, extraction fact type,
or schema migration. `SCHEMA_VERSION` remains 13 and no dependency is added.

## Candidate review authority

A candidate with no review event is `pending`. Review events are immutable and
the latest event is authoritative by `created_at DESC, id DESC`. The effective
states are:

| Latest event | Effective authority |
| --- | --- |
| none | `pending`; ineligible for a new profile save |
| `approved` | the immutable candidate's original canonical value, hash, and normalized key |
| `approved_edit` | the event's validated edited canonical value, hash, and recomputed normalized key |
| `rejected` | rejected and ineligible for a new profile save |

Every transition between `approved`, `approved_edit`, and `rejected` is legal.
The public M6 review service implements one atomic `BEGIN IMMEDIATE` operation:
read latest, decide whether the request is a retry, append when required, and
commit. A request with the same latest semantic decision/value and `note is
None` returns the existing effective result without adding an event. A changed
decision or value adds an event. Any non-`None` note adds an audit event even
when semantic authority is unchanged. Notes never affect authority. The
existing low-level repository append primitive remains available and retains
its generic transaction behavior; M6 uses the smallest transaction-aware seam
needed to avoid nested transactions.

The public service generates timestamps itself with
`datetime.now(UTC).isoformat(timespec="microseconds")`. Production timestamps
therefore have exactly the canonical form
`2026-09-26T15:42:13.123456+00:00`: UTC `+00:00`, fixed microsecond precision,
and lexical order equal to chronological order. A test clock or internal test
timestamp may be injected only through a seam that cannot weaken production
authority. M6 validates every review-event timestamp used for authority or
history. A malformed or noncanonical timestamp is corruption: it is never
normalized, reordered, or repaired. Equal timestamps are resolved by the
greater database event ID.

The effective service result exposes at least the candidate ID, effective
status, effective canonical value JSON/hash/normalized key for approved
states, and latest event database ID/timestamp when present. Review history is
newest first by the same authority ordering.

## Approved-edit validation and candidate immutability

Before accepting `approved_edit`, M6 validates the complete recognized M5
candidate aggregate. The edit cannot change `fact_type`. Its canonical value
must use the exact M5 JSON shape for that type, canonical project JSON, and an
exact SHA-256. It rejects missing, extra, null, non-string, empty, multiline,
or surrounding-whitespace fields; NaN/Infinity; values outside M5 field,
canonical-value, or normalized-key bounds; and invalid names, email addresses,
phone numbers, URLs, or dates. Dates use the frozen M5 grammar and an
`end_date` requires `start_date`. M6 recomputes the exact M5 normalized key.
It does not rerun resume structural-label or evidence discovery over edited
values. The original candidate row, value, key, and evidence remain immutable.

## M5 aggregate authority digest

For each referenced M5 fact run, M6 computes one transient, non-persisted
deterministic aggregate authority digest before taking the M6 write lock. It
first fully validates the recognized M5 aggregate using frozen M5 authority and
bounded M5 limits. Its canonical authority object contains:

- the exact extraction run ID;
- the current extraction raw artifact SHA-256 and UTF-8 byte size, verified
  against the actual raw artifact;
- every persisted fact-run authority field: `id`, `extraction_run_id`,
  `sanitization_run_id`, `input_kind`, `mode`, `outcome`, `cache_fingerprint`,
  `input_sha256`, `input_byte_size`, `extractor_fingerprint`,
  `schema_fingerprint`, represented provider/model/prompt fields,
  `token_count`, and `created_at`;
- the complete candidate set in deterministic ID order with every persisted
  candidate field; and
- the complete evidence set in deterministic `(candidate_id, ordinal)` order
  with every persisted evidence field.

M6 serializes that object with the project's canonical JSON convention and
SHA-256 hashes the UTF-8 representation. Multiple selected candidates from one
fact run share the one prevalidated digest.

Inside the short profile-save `BEGIN IMMEDIATE` transaction, M6 rereads the
same extraction, fact-run, candidate, and evidence authority, re-verifies the
current raw artifact hash/size, recomputes the digest, and requires exact
equality with the prevalidated digest. It then rereads the exact latest review
authority for every source candidate. Parsing, resume extraction, PII
detection, deterministic M5 extraction, and model/network/provider work never
run inside that transaction. The digest is only a race/tamper guard; it is not
persisted and does not require schema 14.

## Logical fact identity

The first candidate-backed identity is:

```text
candidate:<lexicographically-smallest-selected-source-candidate-id>
```

It fits schema-v13 bounds and, once established, is stable. An approved edit or
supporting-source change does not rekey it. A historically unambiguous
candidate association may restore the same ID after removal. Equal normalized
keys never merge automatically: later duplicate-equivalent candidates require
deliberate association with an explicit existing identity target. Conflicting
facts remain separate, including a deliberate distinct/new-identity choice for
the same normalized value. Ambiguous historical mappings fail closed. Removal
omits a fact from the next complete snapshot but preserves history; deliberate
reappearance may reuse its valid identity. Existing manual/imported logical
IDs are retained exactly. M6 does not derive identity from random profile-fact
row IDs and introduces no manual/imported authoring identity policy.

## Complete profile assembly

Assembly receives an ordered desired complete snapshot and an observed
`expected_revision`. A candidate-backed entry identifies its
`primary_candidate_id`, ordered request support IDs, an optional explicit
existing logical identity target, an explicit distinct/new-identity choice
where required, and an optional privacy override. Current valid
manual/imported facts may be carried by existing logical ID; M6 may reorder or
remove them or change only their version-local privacy classification.

Before the write transaction M6 validates the current profile aggregate,
fully validates every selected M5 aggregate, resolves all review authority and
effective values/keys/hashes, validates merge compatibility, resolves logical
identities, applies privacy, assigns contiguous sort order, computes the
content fingerprint, and computes each distinct M5 run digest.

Inside `BEGIN IMMEDIATE`, M6:

1. validates the current `career_profile_state`;
2. requires the exact `expected_revision` and expected next version number;
3. rechecks every M5 aggregate authority digest;
4. rechecks exact latest review authority for every source;
5. rechecks source merge compatibility and effective values;
6. verifies that profile state and parent still match the prepared draft;
7. inserts the version, every fact, and every source row;
8. advances state through a guarded CAS; and
9. commits atomically.

Any disagreement rolls back and fails closed. M6 never silently regenerates a
new draft while holding the lock.

Every saved version is a complete snapshot; an empty snapshot is valid. Every
deliberate successful save creates a version even when semantic content is
unchanged: random new version and fact row IDs, the next `version_number`, the
prior current version as parent, the same fingerprint if content is unchanged,
and exactly one revision increment. This is authority history, not cache
deduplication.

## Candidate sources and merge rules

For each candidate-backed fact:

```text
persisted_source_candidate_ids = sorted({
    primary_candidate_id,
    *supporting_candidate_ids,
})
```

The primary determines the stored effective display/canonical value and must
always be persisted in `career_profile_fact_sources`. Request input that
contains duplicate support IDs or repeats the primary in the support list is
invalid and is not silently deduplicated. One candidate cannot source two
distinct facts in one profile version.

Every persisted source must exist, belong to a recognized valid M5 aggregate,
and currently be `approved` or `approved_edit`. A deliberate multi-source fact
is valid only when every source has the same `fact_type` and every effective
value recomputes to the same full frozen M5 normalized key. This permits exact
cross-run duplicates, display variants with equal keys, and compatible
approved edits. Different keys, partial semantic similarity, pending sources,
and rejected sources cannot merge. Equal keys make a deliberate merge
eligible but never trigger it automatically. A later rejection does not alter
historical snapshots, but prevents using that candidate in a new version until
reapproved.

Source rows are deterministically sorted and unique. Origin/source authority
is exact:

```text
approved_candidate -> source candidate IDs are non-empty
manual             -> source candidate IDs are empty
imported            -> source candidate IDs are empty
```

This applies to new saves and all aggregate/history validation. Generic M2
manual/imported persistence remains, but M6 creates no new authoring flow for
it. A manual/imported historical fact with a candidate source is corruption
and is never repaired.

## Privacy

The default version-local privacy classification is:

| Fact type | Default |
| --- | --- |
| `identity.name` | `sensitive` |
| `contact.email` | `sensitive` |
| `contact.phone` | `sensitive` |
| `portfolio_reference` | `private` |
| `skill` | `public` |
| `employment` | `private` |
| `education` | `private` |
| `certification` | `public` |
| `language` | `public` |

An assembly entry may override this with `public`, `private`, or `sensitive`.
The classification belongs only to that immutable version, never mutates a
source candidate, and may change in later versions. M6 performs no external
publication regardless of classification.

## Origins and semantic fingerprint

M6 candidate-backed saves use `CareerProfileVersion.origin =
"approved_facts"` and `CareerProfileFact.origin = "approved_candidate"`. A
version created through this flow remains `approved_facts` even if it carries
unchanged manual/imported facts. Valid generic M2 version origins `manual` and
`imported`, and fact origins `manual` and `imported`, remain supported.

The profile content fingerprint binds the ordered semantics of each fact:
`logical_fact_id`, `fact_type`, `normalized_key`, `canonical_value_json`, fact
origin, privacy classification, `sort_order`, and sorted source candidate IDs.
It excludes random version/fact row IDs, timestamps, and `value_sha256` (which
is exactly derived from canonical JSON). All M6 profile aggregate reads
recompute and compare the fingerprint. A mismatch is corruption, not cache
drift.

## Profile aggregate integrity and reads

Current and historical reads fail closed. Validation includes canonical JSON;
exact value hash; candidate-backed fact-type and normalized-key validity;
contiguous persisted `sort_order` from zero without sorting malformed data into
validity; unique logical IDs per version; exact privacy/origin vocabularies;
the origin/source relationship; source-candidate existence; persisted sources
already in deterministic sorted unique order; fingerprint; parent/version
chain; and singleton state invariants.

The initial state is current version `NULL`, revision 0, and next version 1. In
a noninitial state the current version exists, `revision ==
next_version_number - 1`, and the current version number is
`next_version_number - 1`. Version 1 has no parent; each later version points
to the preceding authority version. Missing parents, malformed singletons,
malformed facts/sources, duplicate identities, invalid ordering, and hash/key/
fingerprint mismatches fail closed. Immutable rows are never sorted,
deduplicated, normalized, repaired, or rewritten.

Historical snapshots are interpreted as the authority saved at that time, not
through present-day candidate review state. Their source provenance remains
immutable. Validated service reads expose `CareerProfileState`, current version
or `None`, one complete snapshot with facts in persisted deterministic
`sort_order` and source IDs lexicographically, profile history newest first,
candidate review history newest first, latest effective review state, and
effective value/key/hash for approved states.

## Scope boundary

M6 is limited to review service and effective authority, strict
`approved_edit`, deliberate identity/assembly, immutable complete version
creation, candidate provenance, current/history validation and reads,
revision CAS, and privacy classification. It does not add API endpoints, CLI
commands, web UI, model-assisted extraction, fact types, job sources, schema changes,
or dependencies; change M5 extraction behavior; alter Matching Preferences,
ranking, or adaptive state; or modify Product 1 v1.0 or CareerOS-learning.
