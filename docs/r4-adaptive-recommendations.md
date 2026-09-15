# CareerOS R4 Adaptive Recommendations — Design Freeze

> **Release:** R4 — Adaptive Recommendations
>
> **Status:** Accepted architecture / implementation not yet started
>
> **Design baseline commit:** the documented baseline
>
> **PRE-R4:** closed
>

This document is the canonical implementation contract for CareerOS R4. It
freezes the accepted architecture and milestone boundaries. It contains no
PRE-R4 operational evidence, qualification logs, transcripts, temporary data,
or implementation claims.

## 1. Purpose and product boundary

R4 adds a persistent, deterministic, inspectable, and reversible preference
authority to CareerOS Core. Current review decisions can then produce bounded
learned preferences that adjust appropriate ranked product surfaces without
replacing the existing ranking system.

The release sequence remains:

1. persistent preference authority;
2. review-driven learning;
3. inspectable and reversible learned state;
4. bounded adaptive ranking and explanations on appropriate surfaces;
5. qualification and documentation.

R4 is not a source-expansion release, an AI-agent release, an application-assistant release, or a general ranking rewrite. R5 owns the larger personalized
recommendation cycle and Today/Attention workflow integration.

## 2. Current-state primitives R4 reuses

R4 builds on these existing repository authorities:

- SQLite schema v11 with explicit, ordered, fail-closed migrations.
- `CandidateProfile` and its nine existing JSON/YAML and inline-API fields.
- One deterministic flat/grouped scoring path through `ranking.scorer`.
- Current score components, public ranking explanations, 0–100 display
  clamping, and hard-exclusion vetoes.
- Deterministic ordering by displayed match score, publication time, and stable
  job or representative ID.
- Current review decisions: `shortlist`, `dismiss`, and `review_later`.
- One current `review_decisions` row per concrete job plus append-only
  `review_events` history.
- Deterministic lowest-decided-job conflict resolution for canonical duplicate
  groups.
- Existing direct review `FeedbackSet` scoring: `+5` for shortlist, `-5` for
  dismiss, neutral review-later, at most two sources and `±10` total.
- Existing title, company, location, remote, employment-type, and seniority
  normalization/scoring primitives.
- Shared rank, grouped-rank, review queue, report, saved-search, export, CLI,
  HTTP, and plain HTML/CSS/JavaScript presentation boundaries.
- Current chronological, administrative, audit, and Today read models.
- Established temporary-database, migration, FastAPI, and static-frontend test
  conventions.

The current web Matching Preferences workspace exposes all nine explicit
profile fields but stores its draft and active profile only in process memory.
CLI profiles are private files, and HTTP profiles are request-local.

Existing direct review feedback is not persistent learned preference state.
It is calculated from current review decisions for a ranking operation. It is
enabled on existing CLI/service ranked paths and deliberately disabled on
existing HTTP/web ranking paths.

## 3. Actual R4 gaps

The gaps R4 must close are:

- no durable explicit preference authority shared by CLI, service, API, and
  UI;
- no persistent, structured review-derived preference evidence;
- no canonical distinction between explicit preferences, learned preferences,
  and defaults;
- no activation threshold for persistent learning;
- no learned-preference inventory, evidence counts, provenance, suppression,
  restore, or learned-only reset;
- no shared persistent learned-preference authority across ranked product
  surfaces;
- no learned-preference contribution in public ranking explanations;
- no migration or recovery contract for persistent preference state.

The pre-existing distinction in direct legacy `FeedbackSet` policy is not an
R4 defect to normalize. R4 preserves that distinction for compatibility while
standardizing learned authority and the raw learned-adjustment calculation.

## 4. Canonical preference authority

### 4.1 Authority hierarchy

For each ranking invocation, preference authority is resolved in this order:

1. a supplied run-scoped explicit profile, when present;
2. otherwise, the configured stored explicit profile;
3. eligible active learned preferences for dimensions not locked by explicit
   state;
4. existing empty/default `CandidateProfile` behavior.

A supplied run-scoped profile is a complete replacement for stored explicit
state for that invocation. It is not merged field by field with the stored
profile.

Defaults are code behavior and are not persisted as preference rows.

### 4.2 Canonical models

The `careeros.preferences` domain will expose immutable service-facing models:

- `ExplicitPreferenceProfile`;
- `PreferenceEvidence`;
- `LearnedPreference`;
- `EffectivePreferenceContext`;
- `AdaptivePreferenceSet`.

Origin is structural rather than a generic free-form field:

- rows in `explicit_preferences` are user-authored;
- rows in `preference_evidence` are review-derived;
- `LearnedPreference` objects are deterministic aggregates;
- defaults are non-persistent fallback behavior.

R4 does not introduce a generic confidence float. Learned state exposes exact
positive and negative counts, net evidence, activation, polarity, a bounded
integer strength level, and suppression state.

## 5. Explicit-profile authority and locks

The stored explicit profile preserves every current `CandidateProfile` field:

- `desired_titles`;
- `skills`;
- `required_skills`;
- `preferred_locations`;
- `remote_preference`;
- `excluded_terms`;
- `hard_excluded_terms`;
- `preferred_employment_types`;
- `preferred_seniority`.

Order and duplicates are preserved because they are part of the current profile
contract. Learned state never edits, replaces, or writes into explicit rows.

Only a populated list dimension or non-null/non-empty scalar dimension creates
a learned-dimension lock:

| Populated explicit field | Locked learned dimension |
|---|---|
| `desired_titles` | title |
| `preferred_locations` | location |
| `remote_preference` | remote |
| `preferred_employment_types` | employment type |
| `preferred_seniority` | seniority |

Company has no current explicit-profile counterpart, so eligible learned
company preferences may apply. Required skills and explicit soft exclusions
remain existing base-ranking authorities. Hard exclusions remain final vetoes
regardless of any legacy or learned adjustment.

## 6. Empty explicit-profile semantics

### 6.1 Configured stored empty profile

A deliberately persisted empty explicit profile:

- is configured and authoritative as the selected stored profile;
- contains zero populated explicit dimensions;
- creates zero learned-dimension locks;
- never silently falls back to a private profile file;
- may combine with eligible learned preferences;
- is not negative preference intent;
- uses the explicit adaptive opt-out to disable learned influence.

`preference_state.explicit_configured` distinguishes this state from having no
stored profile.

### 6.2 Supplied run-scoped empty profile

A supplied empty profile:

- replaces stored explicit state for that invocation;
- has zero populated explicit dimensions and therefore zero dimension locks;
- may combine with eligible learned preferences;
- disables learned influence only when the invocation explicitly opts out of
  adaptive preferences.

## 7. Learnable and excluded dimensions

### 7.1 Learnable structured dimensions

A meaningful review decision snapshots these deterministic job attributes when
present:

- normalized title signature;
- normalized company signature;
- non-generic normalized location;
- explicit `remote` status;
- normalized stated employment type;
- canonical title-derived seniority.

Extraction rules:

- Empty or missing attributes create no evidence.
- Generic locations such as Remote or Anywhere create no location evidence.
- `onsite_or_unspecified` creates no remote or onsite evidence because the
  stored value conflates two states.
- Multi-valued employment fields may create one normalized value per stated
  type.
- Title, company, and location reuse existing grouping normalization.
- Seniority reuses the scorer's canonical junior/mid/senior detection.

### 7.2 Deliberately excluded dimensions and inputs

R4 does not learn from:

- descriptions or description-derived skills and terms;
- provider/source identity;
- URLs or publication age;
- application state;
- review notes, application notes, reminders, requirements, answers, or
  materials;
- searches, clicks, previews, navigation, or time spent;
- development telemetry;
- inferred personality, employability, or opaque semantic summaries;
- age, gender, ethnicity, nationality, religion, health, family status,
  politics, or any other sensitive characteristic.

## 8. Review action to learning signal

Only current CareerOS review actions are eligible:

| Current action | Learning result |
|---|---|
| First `shortlist` | Positive signal |
| Transition to `shortlist` | Replace that job's prior evidence with positive evidence |
| First `dismiss` | Negative signal |
| Transition to `dismiss` | Replace that job's prior evidence with negative evidence |
| `review_later` | No signal; remove current evidence for that job |
| Clear decision | No new signal; remove current evidence for that job |
| Same decision without a note | No new signal and no evidence rewrite |
| Same decision with a note | No new signal; note/event remains audit-only |

Application actions, reminder actions, ranking impressions, and UI behavior
never create learning signals.

Review current-state mutation, its `review_events` row, and preference-evidence
replacement/removal must commit atomically. A preference-learning failure rolls
the entire review mutation back.

## 9. Evidence and history model

R4 chooses model D: current structured evidence snapshots plus derived learned
state and existing review history.

The authoritative pieces are:

- `review_events`: existing append-only user-action audit;
- `preference_evidence`: current post-R4 structured evidence snapshots;
- derived `LearnedPreference` aggregates;
- persistent suppression controls;
- a reset boundary in `preference_state`.

R4 does not add a second append-only learning event stream and does not
materialize learned preference aggregates.

### 9.1 Duplicate prevention

- Repeating an identical decision without a note does not alter evidence.
- A same-decision note event does not alter evidence.
- A concrete job has at most one current evidence row per dimension and
  normalized value.
- One meaningful review event cannot insert the same dimension/value twice.
- During aggregation, one current canonical opportunity contributes at most
  once.
- Duplicate-group conflict resolution uses the existing lowest-decided-job
  authority.

Evidence is job-anchored rather than group-owned. Aggregation reads current
canonical grouping, so regrouping cannot orphan evidence or require mutable
group foreign keys.

### 9.2 Reversal

- A decision transition replaces that job's evidence snapshot.
- `review_later` and clear remove that job's evidence.
- Suppression preserves evidence.
- Restore removes suppression.
- Global reset deletes current evidence and suppression controls but preserves
  explicit preferences, review decisions, and review history.

## 10. Activation, strength, contradiction, and time

For one learned dimension and normalized value:

```text
net_evidence = positive_count - negative_count
```

Activation is frozen as follows:

- positive when `positive_count >= 2` and `net_evidence >= 2`;
- negative when `negative_count >= 3` and `net_evidence <= -2`;
- inactive and score-neutral otherwise.

Negative activation requires three dismissals because dismissal is a noisier
signal than shortlist. Once active, positive and negative preferences use
symmetric point magnitude for the same strength.

Strength is:

```text
strength_level = min(2, abs(net_evidence) - 1)
```

after activation, giving levels one and two.

Contradictory evidence cancels arithmetically. For example, two positive and
one negative evidence items produce net one and therefore no score effect.

Evidence does not decay or expire in R4. Staleness is managed through later
contradictory reviews, inspection, suppression, or reset rather than hidden
time weighting.

## 11. Inspection, suppression, restore, and reset

### 11.1 Inspection

Users must be able to inspect:

- the effective explicit profile and whether stored explicit state is
  configured;
- active, inactive, and suppressed learned preferences;
- dimension, display value, polarity, positive/negative counts, net evidence,
  strength, activation reason, and suppression status;
- up to three example opportunities per polarity while retaining exact total
  counts.

Review notes and raw descriptions never appear in learned-preference
provenance.

### 11.2 Suppression

Suppressing one learned preference creates a control for its stable key.
Evidence continues to accumulate, inspection remains available, and ranking
contribution becomes zero.

### 11.3 Restore

Restoring removes the suppression. If evidence still meets activation rules,
the preference becomes active immediately. Otherwise it remains visible but
inactive.

### 11.4 Reset

Global learned reset runs transactionally and:

- deletes all current `preference_evidence` rows;
- deletes all learned-preference suppressions;
- records reset time and the current maximum review-event ID;
- preserves stored explicit preferences;
- preserves current review decisions and append-only `review_events`.

Old reviews do not regenerate evidence after reset. Only a later meaningful
review transition creates new evidence.

R4 exposes no arbitrary individual evidence-deletion command. Users suppress a
derived preference or change/clear its underlying review. Global reset is the
only bulk evidence deletion.

### 11.5 Operations

The accepted CLI contract is:

```text
careeros preference show [--format text|json]
careeros preference explicit replace PROFILE
careeros preference explicit clear
careeros preference suppress KEY
careeros preference restore KEY
careeros preference reset-learned --confirm
```

The accepted HTTP contract is:

```text
GET    /preferences
PUT    /preferences/explicit
DELETE /preferences/explicit
GET    /preferences/learned
PUT    /preferences/learned/{key}
DELETE /preferences/learned
```

The learned-preference PUT body is `{"suppressed": true|false}`. Preference
writes use the existing same-origin/write-header, collection-lock, and writable
connection protections.

The UI extends Matching Preferences with one compact saved-and-learned section
for explicit save/clear, learned inspection, suppress/restore, and confirmed
reset. R4 does not create a separate administration application.

## 12. Schema v12 persistence design

R4 requires an additive schema migration because v11 cannot represent a
configured explicit profile, post-R4 evidence, suppression, or reset without
ambiguity.

`SCHEMA_VERSION` becomes 12. The four tables below are frozen.

### 12.1 `preference_state`

Purpose: singleton authority metadata and reset boundary.

Columns and constraints:

- `singleton INTEGER PRIMARY KEY CHECK (singleton = 1)`;
- `explicit_configured INTEGER NOT NULL DEFAULT 0 CHECK (explicit_configured IN (0,1))`;
- `explicit_updated_at TEXT`;
- `learning_reset_at TEXT`;
- `learning_reset_review_event_id INTEGER NOT NULL DEFAULT 0 CHECK (learning_reset_review_event_id >= 0)`;
- `created_at TEXT NOT NULL`.

Exactly one singleton row is created by fresh bootstrap and migration.

### 12.2 `explicit_preferences`

Purpose: ordered user-authored values for the configured stored profile.

Columns and constraints:

- `dimension TEXT NOT NULL`, constrained to the nine current profile fields;
- `position INTEGER NOT NULL CHECK (position BETWEEN 0 AND 99)`;
- `value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 100)`;
- primary key `(dimension, position)`.

The service restricts `remote_preference` to position zero and its current
50-character API bound. There is deliberately no value-uniqueness constraint:
order and duplicates remain preserved.

### 12.3 `preference_evidence`

Purpose: current post-R4 structured evidence snapshots from meaningful review
actions.

Columns and constraints:

- `job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT`;
- `review_event_id INTEGER NOT NULL REFERENCES review_events(id) ON DELETE RESTRICT`;
- `dimension TEXT NOT NULL`, constrained to title, company, location, remote,
  employment type, and seniority;
- `value TEXT NOT NULL`;
- `normalized_value TEXT NOT NULL`;
- `signal INTEGER NOT NULL CHECK (signal IN (-1,1))`;
- `observed_at TEXT NOT NULL`;
- primary key `(job_id, dimension, normalized_value)`;
- unique `(review_event_id, dimension, normalized_value)`.

Indexes:

- `(dimension, normalized_value)`;
- `(review_event_id)`.

### 12.4 `learned_preference_suppressions`

Purpose: persistent user control independent of evidence.

Columns and constraints:

- `preference_key TEXT PRIMARY KEY`, constrained to a 32-character lowercase
  hexadecimal truncated SHA-256;
- `dimension TEXT NOT NULL`;
- `normalized_value TEXT NOT NULL`;
- `suppressed_at TEXT NOT NULL`;
- unique `(dimension, normalized_value)`.

The stable key is derived from the preference dimension and normalized value.
No foreign key to a materialized learned row exists because learned rows are
derived.

## 13. Migration, upgrade, and recovery contract

The v11-to-v12 migration must:

- add `_apply_11_to_12` and register `MIGRATIONS[11]`;
- run inside the existing ordered `BEGIN IMMEDIATE` migration transaction;
- create the four tables and indexes without `IF NOT EXISTS` so collisions fail
  closed;
- extend exact structural inspection for v12 columns, checks, foreign keys,
  unique constraints, and named indexes;
- validate the v12 target before updating `schema_metadata`;
- insert the singleton preference state with `explicit_configured=0`;
- set `learning_reset_review_event_id` to
  `COALESCE(MAX(review_events.id), 0)`;
- create no explicit-preference, evidence, or suppression rows;
- leave jobs, groups, reviews, applications, reminders, materials, packages,
  and all other existing state unchanged;
- roll back every schema and metadata change on failure.

Fresh v12 databases create the same normative schema and use a zero reset-event
boundary when no review events exist.

Migration is explicit through the existing database administration flow.
Running migration on a structurally valid current v12 database is idempotent
and writes nothing. The default backup suffix changes from
`.pre-v11.backup.sqlite3` to `.pre-v12.backup.sqlite3`.

There is no down migration. Recovery means restoring the pre-v12 database
backup. That intentionally discards preference changes made after the backup;
private profile files remain external and separately managed.

### 13.1 No historical review seeding

Existing review decisions and history are not converted into R4 evidence.
Existing direct legacy feedback continues to behave as it did before R4, but
old review history is not silently reinterpreted as persistent learned state.

After migration, only a new meaningful review transition can produce R4
evidence. Same-decision notes do not cross this boundary.

## 14. Derived learned-state decision

Learned preferences are derived on read from current evidence and suppression
controls; they are not stored as materialized preference rows.

This choice prevents aggregate drift, keeps contradiction arithmetic
reproducible, allows current duplicate grouping to be applied consistently,
and avoids a cache-repair subsystem.

Each ranking/report/export operation builds one immutable
`AdaptivePreferenceSet` and reuses it for every candidate. There are no per-job
preference queries and no preference writes during ranking.

Reset evidence is intentionally unavailable for automatic reconstruction from
old reviews. Within the current reset epoch, the derived learned state is
always reproducible from persisted evidence.

## 15. Adaptive ranking contract

### 15.1 Raw learned contributions

For each active matching learned preference:

- title contributes `2 × strength_level`;
- company, location, remote, employment type, and seniority each contribute
  `1 × strength_level`;
- positive or negative polarity supplies the sign.

Per-dimension bounds are:

- title: `[-4, +4]`;
- each other learned dimension: `[-2, +2]`.

The sum is then clamped:

```text
raw_learned_adjustment =
    clamp(sum(per_dimension_adjustments), -8, +8)
```

Match rules are deterministic:

- title: normalized equality or token-prefix compatibility;
- company: normalized equality;
- location: existing normalized token-prefix compatibility;
- remote: exact explicit remote class;
- employment type: normalized exact type match;
- seniority: exact canonical level.

Missing candidate attributes, inactive evidence, contradicted evidence,
suppressed preferences, and explicitly locked dimensions are neutral.

### 15.2 Surface-specific base and anti-double-counting envelope

Each adapted surface preserves its own pre-R4 base-scoring and legacy direct
`FeedbackSet` policy. The exact score formula is:

```text
base_raw_score =
    current surface-specific pre-R4 raw score

legacy_feedback =
    current direct FeedbackSet points for that surface, or 0

raw_learned_adjustment =
    shared deterministic learned-preference calculation
    bounded to [-8, +8]

effective_learned_adjustment =
    clamp(
        legacy_feedback + raw_learned_adjustment,
        -10,
        +10
    ) - legacy_feedback

final_raw_score =
    base_raw_score + effective_learned_adjustment

final_match_score =
    0 if an existing hard-exclusion veto applies
    else clamp(final_raw_score, 0, 100)
```

The `±10` range is the combined anti-double-counting envelope for existing
direct review feedback and persistent learned influence. Same-direction legacy
feedback can reduce the available effective learned headroom. Opposite-direction
learned influence may counter legacy feedback.

Headroom never changes learned evidence, normalization, activation, polarity,
strength, or suppression. It changes only the effective learned points applied
on that surface. Explanations report those effective points.

Compatibility examples:

```text
legacy +5, raw learned +8 => effective learned +5
legacy  0, raw learned +8 => effective learned +8
legacy -5, raw learned -8 => effective learned -5
legacy  0, raw learned -8 => effective learned -8
```

When there are no qualified learned preferences,
`raw_learned_adjustment = 0` and `effective_learned_adjustment = 0` on every
surface.

### 15.3 Ordering and ties

Adaptive `final_match_score` changes ordering only on adapted ranked surfaces.
Existing secondary ordering remains:

1. descending final displayed match score;
2. newest valid publication time, with unknown values last;
3. ascending stable concrete-job or representative-job ID.

Clamped scores tie under these existing rules. Hard exclusions remain final
vetoes everywhere.

## 16. Cross-surface compatibility contract

The following rules are mandatory:

1. Every adapted surface preserves its own pre-R4 base-scoring contract,
   including its existing legacy direct-feedback policy.
2. Zero qualified learned state reproduces the exact pre-R4 behavior of that
   same surface for identical inputs and existing flags.
3. All adapted surfaces share identical persisted evidence, normalization,
   explicit-lock behavior, thresholds, activation, polarity, strength,
   suppression, and raw learned-adjustment calculation.
4. Surfaces sharing the same base-feedback policy and otherwise identical
   inputs agree on effective learned adjustment, final score, ordering, and
   explanation points.
5. Surfaces with different preserved base-feedback policies are not required
   to produce identical final scores or ordering.
6. HTTP/web surfaces do not acquire legacy direct `FeedbackSet` behavior merely
   because persistent learned preferences are added.
7. CLI/service surfaces do not lose their existing legacy direct feedback.
8. Universal cross-surface equality applies to learned authority and raw
   learned calculation, not universally to final totals.

## 17. Adapted surface matrix

| Adapted surface | Legacy direct `FeedbackSet` in pre-R4 base | Persistent learned preferences in R4 |
|---|---|---|
| CLI `rank` | Enabled by default; existing `--no-feedback` disables it | Enabled by default; `--no-adaptive` disables it |
| CLI `review next` | Enabled by default; existing `--no-feedback` disables it | Enabled by default; `--no-adaptive` disables it |
| CLI rank-mode `saved-search run` | Enabled | Enabled by default; add `--no-adaptive` |
| CLI `report` ranked sections | Enabled unless existing `--no-feedback` | Enabled unless `--no-adaptive` |
| `export ranked` | Enabled unless existing `--no-feedback` | Enabled unless `--no-adaptive` |
| `export review` | Enabled unless existing `--no-feedback` | Enabled unless `--no-adaptive` |
| `POST /ranked-jobs` | Disabled and remains disabled | Enabled by default; request-level opt-out |
| `POST /review-queue` | Disabled and remains disabled | Enabled by default; request-level opt-out |
| Rank-mode `POST /saved-searches/{name}/execute` | Disabled and remains disabled | Enabled by default; request-level opt-out |
| `POST /report` ranked sections | Disabled and remains disabled | Enabled by default; request-level opt-out |
| Web Matching Preview | Disabled through its HTTP base contract | Enabled through `/ranked-jobs`; UI opt-out |
| Web Review recommendations/queue | Disabled through its HTTP base contract | Enabled through POST report/queue; UI opt-out preserved during refresh |
| Web rank-mode Saved Search results | Disabled through its HTTP base contract | Enabled through saved-search execution; UI opt-out |
| Ranked Job Detail explanation | Inherits the originating ranked response | Displays that response's effective learned points; never reranks |

The HTTP request-level field is `use_learned_preferences`, defaulting to true.
The web control is labelled “Use learned preferences.” Existing
`--no-feedback` semantics are not repurposed; `--no-adaptive` controls the new
persistent learned layer.

## 18. Unchanged and deferred surface matrix

| Surface | Classification | Frozen behavior |
|---|---|---|
| Chronological jobs, groups, and search | Leave unchanged | Publication-based ordering remains predictable |
| Search-mode saved searches | Leave unchanged | No adaptive scoring or ordering |
| Reviewed Opportunities | Leave unchanged | Review decision/recency authority remains unchanged |
| Application Pipeline and administrative views | Leave unchanged | No preference integration |
| Review, application, reminder, and collection histories | Leave unchanged | Audit ordering and contents remain unchanged |
| Collection/source/database/material/package views | Leave unchanged | No preference integration |
| Profile-less `GET /report` | Leave unchanged | Does not silently become a personalized feed |
| Today/Attention | Defer to R5 | No adaptive ordering or new recommendation cycle in R4 |

## 19. Explanation contract

R4 adds one score-component kind and public criterion:
`learned_preference`.

When effective learned points are non-zero, the ranking explanation contains
one user-readable aggregate factor, for example:

```text
Learned preferences: Backend Engineer, remote work (+3)
```

The ordinary ranking response exposes only the summary, effect, and signed
effective points. Full counts and bounded provenance remain in the dedicated
learned-preference inspection response.

The explanation score adds:

- `base_score`;
- `preference_adjustment`;
- existing final `contribution_total`;
- final `match_score`;
- existing resolution.

It must reconcile:

```text
base_score + preference_adjustment = contribution_total
```

`preference_adjustment` is the effective learned adjustment after the combined
legacy-plus-learned envelope, not the raw learned value. If effective learned
points are zero, no learned factor appears. Suppressed, inactive, contradicted,
or explicitly locked preferences never appear.

The existing legacy-feedback factor remains distinct. Hard-exclusion factors
and resolution remain authoritative.

## 20. Privacy, safety, and operating-cost contract

Preference state remains local-first in the CareerOS SQLite database. Private
explicit profile files remain outside Git. Dedicated preference endpoints use
the existing local same-origin and write protections. Preference values are not
echoed in unrelated ranking, report, audit, or diagnostic responses.

R4 persists no raw model reasoning, embeddings, opaque semantic profile,
hidden personality narrative, or sensitive inference. Review notes and raw job
descriptions never enter preference evidence.

Learning, aggregation, ranking, and explanations are deterministic local code.
They require no model call for a review action, preference update, ranking
operation, or explanation render. Target recurring model cost is `$0`.

Luna is not required anywhere in R4 and has no authoritative, operational, or
advisory release role.

## 21. Test and acceptance architecture

### 21.1 Unit coverage

Tests must cover:

- explicit profile validation and ordered round-trip;
- supplied-over-stored precedence;
- configured-empty and supplied-empty semantics;
- populated-dimension locks and empty-dimension non-locks;
- positive, negative, and neutral review mappings;
- meaningful transitions, same-decision no-ops, and same-decision notes;
- missing and generic attribute exclusion;
- deterministic normalization for all learned dimensions;
- duplicate opportunity collapse;
- positive and negative activation thresholds;
- one-click and two-dismissal non-overreaction;
- repeated evidence from distinct opportunities;
- contradiction cancellation;
- no decay or expiration;
- suppression and restore;
- raw learned weights and per-dimension/global caps;
- combined anti-double-counting headroom;
- deterministic explanation order and wording;
- unchanged tie-breaking and hard-exclusion vetoes.

### 21.2 Integration and service coverage

Tests must cover:

- atomic review decision, event, and evidence updates;
- injected learning failure rolling back all three;
- clear/review-later evidence removal;
- duplicate-group decision authority;
- one immutable adaptive context per operation;
- rank, grouped rank, queue, report, saved-search rank, and ranked/review
  exports;
- existing direct-feedback behavior remaining unchanged;
- learned reset preserving explicit preferences and review history.

### 21.3 Database and migration coverage

Tests must cover:

- exact fresh v12 structure;
- additive v11-to-v12 migration;
- target validation before metadata update;
- migration collision and injected-failure rollback;
- complete migration dispatcher boundaries 8 through 11;
- current-schema idempotency;
- preservation of existing jobs and workflow history;
- cutoff initialization to the pre-upgrade maximum review-event ID;
- empty explicit/evidence/suppression tables after upgrade;
- no historical review seeding;
- exact foreign keys, uniqueness, checks, and indexes;
- configured-empty versus absent explicit state;
- pre-v12 backup and restore behavior.

### 21.4 CLI, API, and UI coverage

CLI tests cover explicit replace/show/clear, learned inspection, suppress,
restore, confirmed reset, lock contention, validation, and the distinct
`--no-feedback` and `--no-adaptive` controls.

API tests cover response allow-lists, write protections, lock behavior,
privacy, preference operations, learned opt-out, and unchanged chronological
GET endpoints.

Static UI contract tests cover persisted explicit loading, local drafts before
save, authoritative preference refetch, suppression/restore/reset, reset
confirmation, stale-response protection, propagation of adaptive opt-out, safe
`textContent` explanation rendering, and absence of client-side learning or
ranking.

R4 does not introduce a new browser framework solely for this release. FastAPI
integration and static UI contract tests remain the automated convention. M5
adds a temporary-database manual browser acceptance pass.

### 21.5 Corrected M4 compatibility acceptance

#### A. Per-surface zero-state compatibility

Every adapted surface with empty, inactive, or fully suppressed learned state
must reproduce that surface's exact pre-R4 scores, ordering, explanation
components, and legacy-feedback behavior.

#### B. Shared learning authority

Identical evidence and explicit-lock state must produce identical:

- learned keys and values;
- positive and negative counts;
- net evidence;
- activation and polarity;
- strength;
- suppression state;
- raw learned adjustment before surface-specific legacy headroom.

#### C. Same-base consistency

Surfaces sharing the same base-feedback policy and otherwise identical inputs
must agree on:

- effective learned adjustment;
- final raw and displayed score;
- ordering;
- explanation points.

#### D. Different-base compatibility

Surfaces with different preserved legacy-feedback policies may differ in final
score and ordering. Each must conform exactly to the frozen surface-specific
formula. Tests include all four compatibility examples in section 15.2.

#### E. Explanation correctness

The learned explanation factor exposes only effective learned points actually
applied after headroom and caps, and its arithmetic reconciles exactly.

#### F. Hard exclusions

Existing hard exclusions remain final displayed-score vetoes on every surface.

## 22. R4 milestone plan

### M1 — Persistent Preference Authority

**Objective:** establish schema v12 and one canonical explicit-preference
service.

**Scope:** the four frozen tables; explicit ordered profile persistence;
configured-empty distinction; supplied/stored/default resolution; stable
preference models and keys.

**Principal modules:** `database/schema.py`, `database/migrations/core.py`,
`database/admin.py`, new `database/preferences.py`, new
`preferences/models.py`, new `preferences/service.py`, and a narrow profile
conversion helper if required.

**Schema:** full v12 delta.

**Service/API/CLI/UI:** domain service only; no API, CLI, or UI yet.

**Tests:** exact schema, migration, rollback, explicit round-trip, empty-state
distinction, precedence, and preservation of existing data and ranking.

**Acceptance gate:** fresh and upgraded v12 are structurally current; explicit
state round-trips every existing field in order; no ranking, review, API, CLI,
or UI behavior changes.

**Dependencies:** accepted design-freeze checkpoint.

**Non-goals:** review learning, inspection operations, adaptive scoring, or UI.

M1 is backend-only.

### M2 — Review-Driven Learning

**Objective:** produce deterministic post-R4 evidence and derived learned
preferences.

**Scope:** structured extraction, review signal mapping, threshold/strength
aggregation, duplicate collapse, bounded provenance, and atomic
review/evidence coordination.

**Principal modules:** new `preferences/learning.py`, preference service and
database adapters, `review/service.py`, and reused/refactored normalization
helpers without changing grouping behavior.

**Schema:** use M1 tables; no new migration.

**Service/API/CLI/UI:** service only.

**Tests:** all signal mappings, transitions, reversals, duplicates, thresholds,
contradictions, missing fields, atomic rollback, and old-history exclusion.

**Acceptance gate:** every meaningful post-R4 review produces or replaces the
specified evidence atomically; repeated and neutral actions do not; aggregation
is deterministic.

**Dependencies:** M1.

**Non-goals:** ranking influence, inspection UI, or historical seeding.

M2 is backend-only.

### M3 — Inspection and Reversal

**Objective:** make explicit and learned state understandable and controllable.

**Scope:** accepted CLI commands, preference API, explicit save/clear, learned
inspection, suppress/restore, global learned reset, and compact Matching
Preferences integration.

**Principal modules:** `cli/parser.py`, new `cli/commands/preferences.py`,
`api/schemas.py`, `api/main.py`, and the existing static HTML/JS/CSS assets.

**Schema:** no change.

**Tests:** CLI/API/static UI contracts, privacy, write protection, lock
contention, confirmations, stale-response guards, and authoritative refetches.

**Acceptance gate:** users can inspect why a learned preference exists,
suppress it, restore it, reset learned state, and verify explicit preferences
survive.

**Dependencies:** M2.

**Non-goals:** adaptive ordering and Today integration.

M3 is the first milestone requiring UI work.

### M4 — Adaptive Ranking, Explanations, and Surface Integration

**Objective:** apply bounded learned adjustments consistently according to
each ranked surface's preserved base contract.

**Scope:** immutable adaptive context, learned formula and caps, combined
legacy/learned envelope, explanation component, opt-out controls, and the
adapted matrix in section 17.

**Principal modules:** new `ranking/adaptive.py`, existing scorer and
explanation modules, saved-search execution, review and report services, export
services, CLI adapters, API schemas/routes, and ranked web rendering.

**Schema:** no change.

**Tests:** all corrected compatibility requirements in section 21.5, formula
bounds, deterministic reranking, ties, explanations, opt-outs, and unchanged
unranked surfaces.

**Acceptance gate:** every surface preserves its own zero-state baseline; all
surfaces share learned authority and raw calculation; same-base surfaces agree
on totals; different-base surfaces follow the frozen formula without an
equality requirement; explanations report effective points; hard exclusions
remain final.

**Dependencies:** M3.

**Non-goals:** base-weight rewrite, legacy-feedback removal, or Today redesign.

### M5 — R4 Qualification and Documentation

**Objective:** qualify the implemented release and document actual behavior.

**Scope:** full regression, v11 migration rehearsal, temporary-database browser
acceptance, recovery guidance, and implementation-state documentation.

**Principal files:** `README.md`, `AGENTS.md`, this design document's
implementation status, and tests only where qualification reveals a real gap.

**Schema:** verify v12; no new schema.

**Acceptance gate:** lint, format check, strict mypy, full pytest, diff check,
migration/rollback rehearsal, wheel/static asset verification, and manual
browser acceptance all pass with a clean intended worktree.

**Dependencies:** M4.

**Non-goals:** feature additions, source expansion, or R5 work.

## 23. Risks and smallest mitigations

| Risk | Frozen mitigation |
|---|---|
| One-click reinforcement | No persistent activation before two positive or three negative distinct opportunities |
| Noisy dismissals | Higher negative evidence floor; review-later remains neutral |
| Duplicate review inflation | Current canonical-opportunity collapse using existing review resolution |
| Contradictory feedback | Net dominance of at least two; ties and net-one states remain inactive |
| Sparse evidence | Keep inactive evidence inspectable; do not weaken thresholds implicitly |
| Stale learned state | Explicit inspection, later contradiction, suppression, restore, and reset; no hidden decay |
| Learned score domination | Per-dimension caps, learned `±8`, combined review-derived `±10`, hard vetoes |
| Legacy and learned double counting | Shared combined `±10` envelope |
| Reduced learned headroom on CLI/service | Document and test as intentional surface compatibility behavior |
| Explicit profile dilution | Populated-dimension locks |
| Ambiguous job fields | Omit evidence instead of inferring |
| Partial review/learning write | One transaction with complete rollback |
| Regrouping changes duplicate authority | Derive aggregates from current groups; do not persist group-owned preferences |
| Aggregate drift | Derive learned state instead of materializing it |
| Existing installations change unexpectedly | No evidence backfill; per-surface zero-state regression tests |
| Reset resurrects old history | Delete current evidence and advance reset-event boundary |
| Confusing explanations | One aggregate learned factor; detailed provenance only in preference inspection |
| UI/state drift | Server-authoritative refetch and existing generation guards |
| Private preference leakage | Dedicated allow-listed local endpoints; no unrelated response or log exposure |
| Migration collision or partial DDL | Exact structural inspection and one transactional boundary |

## 24. Explicit R4 non-goals

R4 will not:

- add, qualify, or harden job sources;
- change collector polling;
- submit applications or contact employers;
- generate resumes, cover letters, application answers, or employer research;
- redesign Today/Attention or implement the R5 recommendation cycle;
- add MCP or Superpowers integration;
- add generalized model routing;
- use Luna or another LLM as preference authority;
- create an online or cloud preference service;
- persist embeddings, personality profiles, or semantic summaries;
- infer sensitive personal characteristics;
- learn from clicks, searches, time-on-page, or development telemetry;
- rewrite base ranking;
- remove or globally normalize existing direct-feedback policy;
- adapt chronological, administrative, audit, or explicitly sorted views;
- seed learned state from PRE-R4 review history;
- move PRE-R4 operational evidence into the repository;

## 25. First implementation work package

The first bounded package is **R4 M1 — v12 preference persistence
foundation**. It is suitable for one focused builder session.

### 25.1 Included scope

- Set `SCHEMA_VERSION` to 12.
- Add the four frozen preference tables and indexes.
- Add the v11-to-v12 migration.
- Add exact v12 structural validation.
- Change default migration backup naming to pre-v12.
- Add preference domain models and validation constants.
- Add preference database adapters.
- Add a transaction-owning stored explicit-profile replace/clear/load service.
- Add supplied-profile > configured-stored-profile > empty-default explicit
  resolution.
- Support deliberately configured empty stored profiles.
- Preserve all current explicit fields, ordering, and duplicates.
- Add focused schema, migration, service, and precedence tests.

Likely implementation files:

- `src/careeros/database/schema.py`;
- `src/careeros/database/migrations/core.py`;
- `src/careeros/database/admin.py`;
- new `src/careeros/database/preferences.py`;
- new `src/careeros/preferences/__init__.py`;
- new `src/careeros/preferences/models.py`;
- new `src/careeros/preferences/service.py`;
- a narrow helper in `src/careeros/ranking/profile.py` only if required;
- new `tests/test_preferences_schema.py`;
- new `tests/test_migration_v12.py`;
- new `tests/test_preferences_service.py`;
- focused database-administration migration test updates.

### 25.2 Tests written first

1. Exact fresh v12 schema.
2. Additive v11-to-v12 upgrade preserving existing rows.
3. Complete rollback after injected migration failure.
4. Empty preference tables and no historical review seeding.
5. Nine-field explicit round-trip preserving order and duplicates.
6. Configured empty profile distinguished from absent stored profile.
7. Supplied explicit profile precedence over stored explicit state.
8. Clear returning authority to fallback without touching other state.
9. Existing ranking remaining unchanged because M1 does not integrate stored
   preferences into ranking entry points.

### 25.3 M1 acceptance gate

- `uv run ruff check src tests`;
- `uv run ruff format --check src tests`;
- `uv run mypy`;
- focused v12 and preference tests;
- full `uv run pytest`;
- `git diff --check`;
- temporary v11 migration rehearsal;
- only intended M1 changes present;
- no ranking, review, API, CLI, UI, source, or telemetry behavior change.

### 25.4 Explicit M1 exclusions

M1 must not implement:

- review-to-evidence hooks;
- learned aggregation;
- suppression, restore, or reset commands;
- preference HTTP endpoints;
- preference UI;
- adaptive score changes;
- explanation changes;
- ranked-surface adaptation;
- Today/Attention integration;
- historical review seeding;
- LLM or model integration.

## 26. Design-freeze checkpoint strategy

This accepted document should be committed separately before M1 with:

```text
Document R4 adaptive recommendations architecture
```

After document-only verification, that commit should be pushed immediately.
M1 begins from the resulting clean pushed checkpoint. PRE-R4 operational
evidence remains external and is not part of this checkpoint.
