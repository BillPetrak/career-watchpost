# Today materialized Review ranking

CareerOS schema 14 materializes the deterministic Review ranking used by
`GET /today`. SQLite is the sole durable authority. A successful Today read
never ranks the whole corpus and never falls back to stale or request-time
full-corpus results.

## Durable authority

Each completed generation records its exact explicit-preference identity,
opportunity-corpus identity, mode-specific learned-preference identity,
scorer/normalizer/format versions, item count, contiguous one-based ordinals,
and complete item-set fingerprint. Opportunity fingerprints cover grouping and
representative authority plus every scorer-visible and public Job field,
including the URL. Private preference snapshots, prepared text, duplicated job
descriptions, and Today responses are not persisted.

Explicit-authority and opportunity changes invalidate both learned modes.
Their replacement generations become current in one activation transaction.
An effective learned-only change invalidates only the learned-enabled mode and
may retain the already-valid disabled-mode generation. Semantic no-ops,
inactive learned evidence, note-only Review events, and Application eligibility
changes do not request score rebuilds.

Collection and regroup update opportunity authority and any changed effective
learned authority in the same transaction as jobs and canonical groups. An
unchanged effective learned identity does not advance its revision. Rebuild
claims, heartbeats, retry state, and current bindings use short SQLite CAS
transactions; corpus scoring runs outside a write transaction. Generation
persistence requires the same live claim, target, mode, and scoring-authority
identity used to compute the scores. Stale-claim takeover removes abandoned
unbound generations before retry, so interruption before activation cannot
wedge the target. Failure metadata is an allow-listed code and never contains
exception text or private values.

## Today behavior

Normal Today reads verify the complete current generation's persisted item
count, contiguous ordinals, item fingerprint, and generation-content
fingerprint in the same coherent read snapshot. They then apply live Review and
Application eligibility, calculate exact eligible and qualified totals, and
select the first three qualified items by stored ordinal. This integrity scan
does not reconstruct or score the opportunity corpus. Only selected
opportunities are reconstructed, and their fingerprints and stored scores are
checked by re-scoring against preference authority read in that transaction.

If ranking is absent, rebuilding, stale, or corrupt, Today fails closed:

```text
HTTP 503
Retry-After: 5
{"detail":{"code":"review_ranking_rebuilding"}}
```

There is no stale-generation response and no request-time whole-corpus
fallback.

## Rebuild operation

The loopback API lifespan owns one cooperative local worker. Durable SQLite
state, not the in-process wake event, decides whether work is needed. The event
reduces latency after local writes; a roughly five-second durable poll recovers
missed wakes and restarts. Blocking rebuild work runs in a thread with its own
database connection.

Startup, explicit audit wakes, and the administrative command reconcile live
ranking identities before auditing complete current generations, then rebuild
missing or corrupt derived ranking state. The ordinary five-second poll keeps
the cheaper reconciliation-and-claim path and does not repeat that full audit.

An administrator can request the same local, idempotent rebuild while holding
the normal collection lock:

```bash
uv run careeros database rebuild-ranking --database /path/to/careeros.sqlite3
```

Schema migration remains explicit and creates a SQLite-only
`.pre-v14.backup.sqlite3` backup by default. Managed Materials and private
configuration/profile state remain separate recovery authorities.
