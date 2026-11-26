# Career Profile authority

Career Profile is an immutable, human-reviewed snapshot assembled from
Career Fact candidates. The pipeline is:

```text
immutable candidates -> review events -> deliberate complete snapshot -> CAS save
```

An unreviewed candidate is `pending`; the latest immutable review event is
authoritative. `approved` retains the original value, `approved_edit` stores a
strictly validated edited value, and `rejected` is ineligible for a new
snapshot. Same-decision retries are idempotent unless a note requires an audit
event. Original candidates, evidence, and prior snapshots never change.

Assembly validates every selected aggregate, effective value/key/hash,
source compatibility, logical identity, privacy classification, and content
fingerprint before a short `BEGIN IMMEDIATE` transaction. The transaction
rereads authority, requires the caller's `expected_revision`, inserts the
complete version and sources, advances state with CAS, and commits atomically.
Conflicts roll back; no draft is silently regenerated. Empty snapshots and
deliberate semantic no-op saves are valid new versions. History remains
immutable and is validated fail-closed.

Candidate-backed facts require exact fact-type and normalized-key agreement
for deliberate multi-source support. Equal keys never merge automatically.
Version-local privacy defaults classify name/email/phone as sensitive,
portfolio/employment/education as private, and skill/certification/language
as public; this is classification only, never publication.

Career Profile is separate from the ranking `CandidateProfile` (Matching
Profile). Saving or reviewing Career Profile does **not** populate or mutate
the ranking Matching Profile, Matching Preferences, review feedback, or
adaptive state. See [`career-profile-workspace.md`](career-profile-workspace.md)
for the local surfaces.
