# Privacy and PII sanitization

Sanitization is a local, deterministic projection of one exact successful
extraction artifact. It is intentionally bounded vocabulary detection, not
comprehensive PII discovery or anonymisation.

The detector recognizes labelled names, supported email forms, and phone
numbers. It does not infer unlabelled person names. Unsupported non-empty
label forms become `review` findings instead of silently passing. Findings
are zero-based half-open UTF-8 byte ranges and may overlap.

## Immutable resolution

Findings and review dispositions resolve to a complete ordered `copy`,
`replace`, or `delete` map. Persisted spans include raw/sanitized ranges and
slice hashes; projections remain discontinuous across replacements/deletions.
Child runs are immutable and retain validated parent lineage. Overrides are
typed, canonical, and derived from persisted parent findings. Validation
rejects cross-extraction parents, cycles, excessive ancestry, altered
findings, unknown detector authority, and hash/map mismatches.

Cache identity binds extraction ID, raw hash/size, policy and sanitizer
fingerprints, parent, and canonical override manifest. A tampered or failed
artifact never wins cache authority. Detector version is part of the
fingerprint, so detector versions cannot share authority.

External/model use has no implicit permission. Approval is an append-only
review event for one exact successful artifact, revoked by a later revoke
event, and not inherited by override children. Missing approval, unresolved
findings, invalid manifests, failed runs, and integrity failures fail closed.
The service performs no network transmission or model invocation.

See `src/careeros/resume_intelligence/pii.py` and `sanitization.py` for the
bounded detector and persistence orchestration.
