# Materialized Today/Review ranking

Schema 14 stores the deterministic Review ranking consumed by `GET /today`.
SQLite is the durable authority: a successful read never ranks the whole
corpus and never falls back to a stale or request-time full-corpus result.

## Generation authority

Each completed generation records explicit-preference identity,
opportunity-corpus identity, mode-specific learned identity, scorer/normalizer
/format versions, item count, contiguous one-based ordinals, and complete
item-set/content fingerprints. Opportunity identity covers grouping,
representative authority, every scorer-visible/public Job field, and URL.
Private preference snapshots, prepared text, duplicated descriptions, and
Today responses are not persisted.

Explicit or opportunity changes invalidate both learned modes; effective
learned-only changes invalidate only the enabled mode. Semantic no-ops,
inactive learned evidence, note-only review events, and application
eligibility changes do not request score rebuilds. Collection/regroup commits
opportunity changes and relevant learned identity together with their writes.

Claims, heartbeats, retry state, and current bindings use short SQLite CAS
transactions; scoring runs outside a write transaction. Persistence requires
the same live claim, target, mode, and authority identity used for scoring.
Stale-claim takeover removes abandoned unbound generations. Failure metadata
is allow-listed and never contains exception text or private values.

## Read integrity and recovery

Today verifies generation count, contiguous ordinals, item fingerprint, and
content fingerprint in one read snapshot, then applies live Review/Application
eligibility and selects the first three qualified stored ordinals. Selected
items are reconstructed and re-scored against transactionally read preference
authority. A missing, rebuilding, stale, or corrupt generation fails closed
with HTTP 503, `Retry-After: 5`, and
`review_ranking_rebuilding`; there is no stale response or whole-corpus
fallback.

The loopback API worker cooperatively polls durable state (roughly five
seconds) and recovers missed wakes/restarts. Startup, explicit audit wakes,
and `database rebuild-ranking` reconcile identities and audit complete
generations. Rebuild is local and idempotent and uses the normal collection
lock. Database backup remains SQLite-only; Materials and private profile/
configuration are separate recovery authorities.
