# Product 1 v1.05 M4 PII sanitization

M4 detects a bounded, deterministic local vocabulary of résumé PII (labelled
names, email addresses, and phone numbers) over the exact UTF-8 artifact stored
by an M3 extraction run. Findings use zero-based half-open byte coordinates and
may overlap.

The version-2 detector supports explicit name labels containing one to five
Unicode letter/combining-mark tokens, including common Unicode apostrophes and
hyphens. It also supports internationalized email local/domain forms made from
Unicode letters, combining marks, and digits, with conservative ASCII email
punctuation, dot-separated domains, and a letter-bearing final label. It does
not attempt unlabelled person-name inference. A non-empty explicit name or
email label outside those supported forms becomes a `review` finding and
therefore cannot silently pass the approval gate.

The resolver turns findings and review dispositions into a complete ordered
`copy`, `replace`, or `delete` map. The persisted map tiles both immutable
artifacts and stores slice hashes, so historical output is authoritative even
if sanitizer code changes. Projection remains discontinuous where a query
crosses replacements or deletions.

Sanitization runs are persisted through the M2 repository. The supported local
service owns the sanitizer and policy fingerprints and derives cache identity
from the exact extraction ID, raw hash and byte size, policy, parent, and
canonical override manifest. Cache reuse first rereads and validates the stored
aggregate; failed or tampered artifacts are never accepted as cache winners.
The detector version is part of the sanitizer fingerprint, so version-1 and
version-2 results cannot share cache authority.
Overrides accept typed finding actions and generate their manifest from the
persisted parent findings. They create immutable child runs; parent text,
findings, spans, and review history are unchanged. Bounded ancestry validation
rejects corrupted parents, cross-extraction edges, cycles, and excessive depth.

For the recognized version-2 fingerprint, repository validation reconstructs a
root aggregate from the exact extraction text and reconstructs each child from
its validated parent findings plus canonical typed override actions. The full
finding sequence, including count, ordinals, category, range, rule identity,
disposition, and span reference, must exactly match persistence along with the
complete map and sanitized text. Unknown detector authorities, missing or extra
findings, and any mutated lineage metadata fail closed for approval.

External/model use has no implicit permission. Approval is an append-only
review event for one exact successful artifact, is revoked by a later revoke
event, and is not inherited by override children. Missing approval, unresolved
findings, failed runs, invalid JSON manifests, hash failures, and map failures
all fail closed. M4 performs no network transmission or model invocation.
