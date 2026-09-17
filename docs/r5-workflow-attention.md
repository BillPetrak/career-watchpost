# R5 — Workflow / Attention Center

> **Status:** Accepted architecture; frozen for implementation
>
> **Authoritative baseline:** the qualified prior release
>
> **Baseline commit:** `Complete R4 qualification and documentation`
>
> **Schema:** v12 remains current; R5 plans no persistence migration

## 1. Purpose

R5 answers one bounded product question:

> What needs my attention now, and what should I do next?

CareerOS already stores the authoritative facts needed to answer that question:
reminders, application stages, Preparation state, Application Package drafts,
Review decisions, canonical duplicate groups, jobs, and R4 Matching
Preferences. R5 combines selected facts into one deterministic daily workflow
snapshot. It does not introduce a task system, a second workflow database, or
an AI prioritizer.

R5 evolves the existing **Today / Action Center** into the complete
**Workflow / Attention Center**. It does not create a new Attention workspace.

0.

## 2. Release boundary

R5 owns:

- the application-work projection added to Today;
- a bounded, preference-aware Review-next preview;
- deterministic priority and ordering across Today sections;
- Today navigation into existing authoritative workspaces;
- focused web, API, privacy, performance, and browser qualification.

R5 does not own:

- a new source of mutable workflow state;
- ranking or R4 preference redesign;
- application submission, outreach, or deadline inference;
- collection/source administration;
- a new notification mechanism;
- a generic task manager or calendar.

PRE-R4 and R4 remain closed. R5 consumes their frozen contracts and must not
reopen them without concrete regression evidence.

## 3. Existing primitives

### 3.1 Today

CareerOS already provides:

- a top-level `#today` workspace;
- `GET /today`;
- one server-authoritative UTC `as_of` instant;
- one coherent SQLite read snapshot;
- overdue reminders;
- reminders due later today;
- shortlisted canonical opportunities without an application;
- total-before-independent-limit semantics;
- deterministic server-owned classification and ordering;
- inline reminder completion through the existing reminder endpoint;
- navigation into Job Detail, Reviewed, Applications, and Reminders;
- frontend request-sequence, view-generation, mutation, refresh, and focus
  guards.

The browser renders the server's classifications. It does not reclassify UTC
times, join workflow state, or reorder attention items.

### 3.2 Home

The separate `#home` workspace is driven by profile-less `GET /report`. It is
the broad operational/system overview and already provides:

- stored and distinct job counts;
- Review counts;
- the application pipeline and broad status categories;
- reminder summaries;
- collection/source health and source quality.

Home's existing status summaries remain useful overview/navigation material.
They do not define Today's urgency contract.

### 3.3 Existing authoritative workflows

R5 reuses, without duplicating their state machines:

- Reminder creation, update, completion, reopening, deletion, and local
  notification delivery;
- Application status and history;
- Preparation revisions, requirements, review, readiness, and deadlines;
- Application Package draft, approval, rebase, and finalization workflows;
- Review decisions and canonical opportunity resolution;
- R4 explicit and learned preference authority;
- the shared deterministic scorer and ranked Review queue.

## 4. Actual R5 gaps

The existing Today snapshot is sound but incomplete for the R5 product
question. It lacks:

1. active application attention for `offer` and `interview`;
2. persisted Preparation work and authoritative Preparation deadlines;
3. unfinished Application Package draft visibility;
4. one frozen priority contract across these facts;
5. a bounded R4-aware Review-next cycle.

R5 fills only these gaps. It does not absorb source health, saved-search
execution, notification delivery, or full Package completeness.

## 5. Frozen Home / Today boundary

### Home

Home remains:

- profile-less;
- operational and administrative;
- broad rather than action-queue shaped;
- the owner of collection/source health;
- unchanged by R5.

Home must not silently become a personalized feed.

### Today

Today becomes:

- the single Workflow / Attention Center;
- user-facing and action-oriented;
- derived from existing authority;
- deterministic and local-first;
- preference-aware only in its bounded Review-next section;
- read-only except for the already-existing explicit reminder-completion UI
  action, which uses the existing reminder endpoint.

There is no third Attention workspace and no `/attention` endpoint.

## 6. Authoritative state reused

R5 may read only existing authoritative state:

| Concern | Authority |
|---|---|
| Reminder urgency | `reminders.due_at`, `completed_at`, and row presence |
| Application stage | `applications.current_status`, `updated_at`, and stable `id` |
| Preparation work | `application_preparations` and live `application_requirement_items` |
| Preparation deadline | A confirmed, required, open live requirement of kind `deadline` with an explicit valid `deadline_at` |
| Package work | The one open `application_package_versions.state='draft'` row, when present |
| Package/preparation mismatch | Draft `preparation_revision` compared with the current live Preparation revision |
| Shortlist | Resolved current `review_decisions` over current canonical groups |
| Review next | Current unreviewed/untracked canonical groups plus the shared scorer |
| Explicit matching | R4 stored explicit preference authority |
| Learned matching | R4 derived active/suppressed learned authority and reset boundary |
| Duplicate identity | Current derived `job_groups` and `job_group_members` |

R5 never treats a derived attention item as new authority.

## 7. Attention taxonomy

Today contains exactly these attention categories, in the order frozen below:

1. overdue reminders;
2. overdue application work;
3. reminders due later today;
4. application work due later today;
5. non-temporal application attention;
6. shortlisted, not started;
7. Review next.

The first five categories answer “what needs attention.” Shortlist and Review
next answer “what should I advance next,” with explicit user intent preceding
new discovery.

### 7.1 Overdue reminders

**Source:** open Reminder rows.

**Include:**

```text
completed_at IS NULL
AND due_at < as_of
```

**Exclude:** completed reminders and reminders due exactly at `as_of`.

**Urgency:** Tier 1, category rank 0.

**Order:** `due_at ASC`, then `reminders.id ASC`.

**Identity:** `reminders.id`.

**Duplicate semantics:** concrete reminder/job ownership; never collapsed by
opportunity group.

**Action:** inline Complete, plus Job Detail and Reminders navigation.

**Authoritative mutation:** existing reminder-completion endpoint.

**Leaves the category when:** completed, deleted, or rescheduled to a
non-overdue time.

### 7.2 Overdue application work

**Source:** one eligible concrete Application item and its live Preparation
deadline authority.

**Include:** the application's earliest qualifying Preparation deadline is
strictly earlier than `as_of`.

**Exclude:** terminal applications and non-authoritative deadline values as
defined in section 10.

**Urgency:** Tier 1, category rank 1.

**Order:** earliest qualifying deadline ascending; application stage rank;
`applications.updated_at DESC`; `applications.id ASC`.

**Identity:** `applications.id`.

**Duplicate semantics:** concrete Application ownership. Two concrete
applications in one duplicate group remain two authoritative workflow items.

**Action:** review the deadline in Job Detail's Preparation section.

**Authoritative mutations:** existing Preparation endpoints.

**Leaves or changes category when:** the requirement is rescheduled, deleted,
made not applicable, or otherwise ceases to qualify; the application becomes
terminal; or the deadline moves into another temporal bucket.

### 7.3 Reminders due later today

**Source:** open Reminder rows.

**Include:**

```text
completed_at IS NULL
AND as_of <= due_at
AND due_at < today_ends_at
```

**Exclude:** overdue, completed, and next-day reminders. A reminder due exactly
at `as_of` belongs here, not in overdue.

**Urgency:** Tier 2, category rank 0.

**Order, identity, action, and mutation:** the same contracts as overdue
reminders.

**Leaves the category when:** completed, deleted, rescheduled, or time advances
past its due timestamp on a later snapshot.

### 7.4 Application work due later today

**Source:** one eligible concrete Application item and its live Preparation
deadline authority.

**Include:** the application's earliest qualifying Preparation deadline is in
the half-open interval `[as_of, today_ends_at)`.

**Exclude:** terminal applications and non-authoritative deadline values.

**Urgency:** Tier 2, category rank 1.

**Order:** earliest qualifying deadline ascending; application stage rank;
`applications.updated_at DESC`; `applications.id ASC`.

**Identity, duplicate semantics, destination, and mutation:** the same as
overdue application work.

**Leaves or changes category when:** the deadline or Application authority
changes, or a later snapshot classifies it as overdue.

### 7.5 Non-temporal application attention

**Source:** current Application status, persisted Preparation state, and the
presence/narrow state of an open Package draft.

**Include:** any eligible application not placed in an overdue or due-today
application bucket.

**Urgency:** Tier 3.

**Order:**

1. `offer`;
2. `interview`;
3. `applied`;
4. `interested`;
5. `applications.updated_at DESC`;
6. `applications.id ASC`.

**Identity:** `applications.id`.

**Duplicate semantics:** concrete Application ownership, never canonical-group
collapse.

**Action:** derived by the safe precedence in section 13. Today navigates to
Job Detail's Application, Preparation, or Package section, or to Applications.

**Leaves or changes category when:** the application becomes terminal; a
status-only `offer`/`interview` becomes `applied`/`interested` without another
qualifying artifact; persisted work is resolved; a draft is finalized/deleted;
or a qualifying deadline moves it to Tier 1 or 2.

### 7.6 Shortlisted, not started

**Source:** current Review decisions resolved over current canonical groups.

**Include:** resolved decision is `shortlist` and no current member has any
Application row.

**Exclude:** unreviewed, `review_later`, `dismiss`, and any opportunity with an
Application on any current member.

**Urgency:** Tier 4.

**Order:** `decision_updated_at DESC`, `decision_id DESC`, representative job ID
ascending.

**Identity:** canonical `group_id`; for an ungrouped singleton, the stable
representative job ID.

**Duplicate semantics:** one canonical opportunity. When member decisions
conflict, the lowest decided `jobs.id` owns the resolved decision and the
conflict remains visible.

**Action:** Job Detail or Reviewed. Starting/changing application state and
changing Review state remain owned by their existing endpoints and workspaces.

**Leaves the category when:** the resolved decision changes/clears, any member
gains an Application record, or regrouping changes its current authoritative
opportunity membership.

### 7.7 Review next

**Source:** the existing shared Review queue and shared deterministic scorer.

**Include:** canonical opportunities that are:

- resolved `unreviewed`;
- untracked across every current member;
- scored at `match_score >= 1`;
- among the first three under the frozen ranking order.

**Exclude:** reviewed groups, groups with any Application, hard-excluded/zero
score results, and results beyond the three-item preview.

**Urgency:** Tier 5. This is discovery guidance, not a due obligation.

**Order:** final displayed match score descending; newest valid member
publication time descending with unknown last; representative job ID
ascending.

**Identity:** canonical `group_id`; for a singleton, its representative job ID.

**Duplicate semantics:** exactly one item per canonical opportunity.

**Action:** navigate to Review, with an optional Job Detail link. Today does not
duplicate Review decision buttons.

**Leaves the preview when:** any member receives a Review decision or
Application, the score drops below one, eligibility changes, or ranking moves
the opportunity below the first three.

## 8. Application-work eligibility

Application eligibility is evaluated before temporal classification.

An Application is eligible when its current status is active and at least one
of these is true:

1. status is `offer`;
2. status is `interview`;
3. a persisted `application_preparations` row requires work;
4. an open Package draft exists;
5. a qualifying Preparation deadline falls before `today_ends_at`.

Active statuses are `interested`, `applied`, `interview`, and `offer`.
`rejected` and `withdrawn` are terminal and always excluded.

Persisted Preparation requires work when the current live Preparation row is
not at current-revision `user_confirmed_ready`. This includes:

- requirements not reviewed or review stale;
- unconfirmed requirements;
- required open non-deadline requirements;
- a passed required deadline;
- requirements satisfied but current readiness not confirmed.

The synthetic empty Preparation projection used when no Preparation row has
ever been persisted does not, by itself, qualify `applied` or `interested`.
Otherwise those statuses would silently become attention solely through an
absence of state.

An `applied` or `interested` application therefore needs persisted Preparation
work, an open Package draft, or a qualifying deadline. Its status alone is
insufficient.

## 9. Offer / interview status-only rule

`offer` and `interview` are bounded exceptions to the general rule that status
does not create temporal urgency.

CareerOS already labels these states “Offer attention” and “Interview
attention.” They are active, high-consequence workflow states even when the
user has not captured a due timestamp.

Therefore:

- `offer` alone qualifies for Tier 3;
- `interview` alone qualifies for Tier 3;
- `applied` alone does not qualify;
- `interested` alone does not qualify;
- `rejected` and `withdrawn` never qualify.

Status-only attention never invents a deadline, never enters Tier 1 or 2, and
never creates a Reminder. Its safe next action is `manage_application`, leading
to Job Detail's Application section or Applications.

When an `offer` or `interview` also has Preparation or Package work, it still
produces exactly one Application card. The card's classification and next
action reflect the higher-priority authoritative work.

## 10. Deadline authority and classification

Only a persisted live Preparation requirement meeting every condition below may
determine an Application's Today deadline bucket:

```text
kind = 'deadline'
required = true
fulfillment_state = 'open'
confirmed = true
deadline_at is a valid explicit timezone-aware timestamp
```

The timestamp is normalized under the existing Preparation rules and compared
in UTC.

For each Application, Today selects the earliest qualifying deadline and
classifies it using the same captured snapshot boundaries as Reminders:

```text
deadline_at < as_of                         => overdue / Tier 1
as_of <= deadline_at < today_ends_at       => due today / Tier 2
deadline_at >= today_ends_at               => no temporal Today bucket
```

Unconfirmed deadlines can make persisted Preparation incomplete and therefore
Tier-3 workworthy, but they cannot establish overdue/due-today authority.
Optional, satisfied, not-applicable, dateless, or invalid deadline rows cannot
establish a temporal bucket.

R5 does not derive deadlines from job descriptions, notes, Application status,
event age, provider data, or Package contents.

## 11. One item per Application

Today returns at most one Application-work item per stable
`applications.id`.

Classification precedence is:

1. earliest qualifying deadline is overdue → overdue Application work;
2. earliest qualifying deadline is due today → due-today Application work;
3. otherwise eligible → non-temporal Application attention.

Preparation, Package, status, and deadline context are compact facts on that
single item. They never create parallel cards for the same Application.

An Application card and a Reminder card may both refer to the same job because
they are distinct authoritative obligations with distinct identities. R5 does
not deduplicate across those domains.

## 12. Exact priority and tie-break contract

Today is the concatenation of these server-ordered sections:

| Tier | Category rank | Category | Sort after tier/category |
|---:|---:|---|---|
| 1 | 0 | Overdue reminders | `due_at ASC`, reminder ID ASC |
| 1 | 1 | Overdue Application work | earliest deadline ASC, stage rank ASC, Application update DESC, Application ID ASC |
| 2 | 0 | Due-today reminders | `due_at ASC`, reminder ID ASC |
| 2 | 1 | Due-today Application work | earliest deadline ASC, stage rank ASC, Application update DESC, Application ID ASC |
| 3 | 0 | Non-temporal Application attention | stage rank ASC, Application update DESC, Application ID ASC |
| 4 | 0 | Shortlisted, not started | decision update DESC, decision ID DESC, representative job ID ASC |
| 5 | 0 | Review next | match score DESC, newest valid publication DESC/unknown last, representative job ID ASC |

Application stage rank is frozen as:

```text
offer = 0
interview = 1
applied = 2
interested = 3
```

Reminder categories precede Preparation deadlines inside the same urgency tier
because Reminders are the user's explicit follow-up and notification authority.

The server owns every classification and sort. The browser must not sort or
reclassify Today data.

## 13. Safe next-action derivation

`next_action` is a derived public presentation enum, never persisted state.
For one Application item, use the first matching rule:

1. Temporal deadline bucket → `review_preparation_deadline`.
2. Persisted Preparation is `unknown` or `incomplete` →
   `complete_preparation`.
3. Persisted Preparation is `requirements_satisfied` but not current-ready →
   `confirm_preparation_ready`.
4. An open draft's Preparation revision is stale →
   `review_rebase_package`.
5. Any other open Package draft → `continue_package`.
6. Status-only `offer` or `interview` → `manage_application`.

These labels direct the user to existing authority. They do not assert that a
Package can be finalized, that an employer expects a response, or that a
deadline exists.

## 14. Package-staleness narrow projection

Today does not own Package completeness.

The only Package staleness fact needed by Today is:

```text
draft_preparation_revision_stale =
    open_draft.preparation_revision != current_live_preparation_revision
```

When no `application_preparations` row exists, the current live Preparation
revision follows existing Package authority and is revision zero.

Today may expose only compact draft facts:

- `has_draft`;
- `draft_approved_for_current_revision`;
- `draft_preparation_revision_stale`.

Today must not execute, reproduce, or approximate Package `completeness()`.
Specifically it must not perform or duplicate:

- Material byte reads or verification;
- hashes;
- storage-root or marker verification;
- Material availability/retirement authority;
- answer completeness;
- full Package reason generation;
- job-snapshot fingerprint/completeness evaluation;
- finalization authority.

Approval for the current draft revision is display context, not proof of
present finalizability. Apart from the explicit Preparation-revision mismatch,
the safe action for a draft is `continue_package`. Full authority is evaluated
only when the user opens or acts on the existing Package workflow.

## 15. Shortlist semantics

The existing shortlist implementation is frozen:

- opportunity identity follows current canonical grouping;
- filters are evaluated over current members;
- any Application row on any member means the opportunity has started;
- the representative is the lowest-ID current member;
- the lowest decided member owns a conflicting resolved decision;
- conflicts remain visible;
- Application rows and Review decisions are never copied across members;
- regrouping can change derived membership but never mutates workflow rows.

Shortlist is Tier 4 because it is explicit pursuit intent. It precedes discovery
of new unreviewed opportunities.

`review_later` does not automatically reappear in Today. A timed revisit
requires an explicit Reminder.

## 16. Adaptive Review-next contract

R5 chooses one bounded adaptive **Review next** section. It does not add a
separate Recommended-next section.

Frozen inputs:

```text
state = unreviewed
grouped = true
include_tracked = false
minimum_score = 1
preview_limit = 3
query/source/location/remote/employment filters = absent
```

The service scores the complete eligible candidate set once, applies the score
threshold, records total-after-threshold before preview limiting, and returns
the first three items.

The response distinguishes:

- `candidate_total`: eligible unreviewed/untracked opportunities before score
  threshold;
- `total`: score-qualified opportunities before the three-item preview limit.

Today does not present Review decision mutation controls. The user navigates to
Review for the complete workflow.

## 17. R4 preference and scoring reuse

Review next must use existing R4 authority and scoring without modification:

1. resolve stored explicit preference authority;
2. preserve configured-empty versus absent/default semantics;
3. convert that authority through the existing CandidateProfile mapping;
4. build one immutable adaptive preference set for the request;
5. preserve explicit-dimension locks;
6. preserve learned thresholds, polarity, strength, suppression, reset, caps,
   hard vetoes, and explanations;
7. call the existing grouped Review queue/scorer once.

Today never reads a private profile path and never creates another scorer.

The R5 cycle is:

1. Today presents up to three Review-next opportunities.
2. The user records decisions in Review or Job Detail.
3. Existing R4 hooks maintain learned evidence.
4. A later Today GET re-derives eligibility and ordering from current state.

No Today load creates evidence, decisions, preferences, or any other mutation.

## 18. HTTP/web FeedbackSet exclusion

Legacy direct `FeedbackSet` remains absent from Today Review-next scoring.

This preserves the frozen R4 HTTP/web base policy:

- direct Review feedback: `None`;
- persistent learned preferences: enabled by default;
- explicit profile authority: retained;
- combined-score and explanation behavior: shared R4 implementation.

R5 must not use direct feedback indirectly, rebuild it in Today, or change CLI
feedback behavior.

## 19. Adaptive opt-out and zero state

`GET /today` accepts `use_learned_preferences`, defaulting to `true`. The Today
web UI exposes the existing user-facing wording **Use learned preferences**.

When false:

- learned preferences contribute zero;
- stored explicit authority still supplies the base profile;
- no persistent preference state is changed.

When there are no qualified learned preferences, the learned adjustment is
exactly zero under the frozen R4 contract.

When the entire effective authority produces no result with
`match_score >= 1`, Review next is empty. Today must not label a chronological
zero-score list as personalized recommendations. The empty state links to
Matching Preferences and the full Review workspace without executing either.

A public `personalized` boolean may be true only when the effective invocation
uses at least one populated stored explicit field or an enabled, active,
unsuppressed, unlocked learned preference. It exposes no preference values or
evidence.

## 20. Action, navigation, and write ownership

| Today category | Today action | Authoritative destination/mutation |
|---|---|---|
| Reminder | Inline Complete; Job Detail; Reminders | Existing Reminder endpoint/workspace |
| Application status-only | Navigate | Job Detail Application section or Applications |
| Preparation work/deadline | Navigate | Job Detail Preparation section and existing endpoints |
| Package draft | Navigate | Job Detail Package section and existing endpoints |
| Shortlist | Navigate | Job Detail or Reviewed; existing Review/Application endpoints |
| Review next | Navigate | Review or Job Detail; existing Review endpoints |

Only reminder completion is inline. A successful reminder mutation triggers a
GET-only Today refresh. Retry after a successful write must never replay the
write.

Today does not duplicate Application, Preparation, Package, Review, Reminder
creation/editing, or saved-search state machines.

## 21. Persistence and schema

R5 introduces no genuinely new persistent concept.

Attention categories, priority tiers, next-action values, personalization
flags, and totals are derived projections and must not be persisted as cache
rows.

Freeze:

```text
SCHEMA_VERSION remains 12.
```

No migration or recovery rehearsal is required solely for R5 because no schema
change is planned.

## 22. `GET /today` evolution

R5 extends the existing endpoint. It does not add `/attention` or another
parallel read model.

The response preserves:

```text
as_of
timezone = "UTC"
today_ends_at
limit
reminders.overdue
reminders.due_today
shortlist.unstarted
```

It adds:

```text
applications.overdue
applications.due_today
applications.work

review.next
```

Every ordinary attention section has:

```text
total
items
```

Each total is computed before that section's independent `limit`. The existing
Today `limit` remains validated from 1 through 20 and applies independently to
Reminder, Application, and shortlist sections.

`review.next` has:

```text
candidate_total
total
limit = 3
minimum_score = 1
personalized
use_learned_preferences
items
```

One public Application-work item contains only:

```text
application_id
job_id
current_status
updated_at
job                         # existing safe public Job projection
classification              # overdue | due_today | attention
next_action                 # frozen enum from section 13
earliest_deadline_at         # null outside temporal buckets
preparation                  # null or {category, blocking_count}
package                      # null or compact draft facts from section 14
```

Review-next items reuse the existing public Review queue item projection and
ranking explanation. Private notes and raw diagnostics remain absent.

A missing database returns the same complete empty envelope without creating a
file, directory, or schema.

## 23. Coherent read snapshot and query bounds

Every `GET /today` response must use:

- one captured timezone-aware UTC `as_of`;
- one derived `today_ends_at` at the next UTC midnight;
- one SQLite read transaction for all sections and preference authority;
- server-owned classification and ordering;
- total-before-limit semantics.

M1 Application work must use batched reads with query count independent of the
number of returned Applications. It must not query Preparation or Package once
per card.

M2 Review next must:

- build explicit/learned authority once;
- load eligible groups and workflow maps in batches;
- score each eligible group once;
- avoid per-candidate preference queries;
- avoid saved-search execution;
- avoid model/network calls.

Today Package projection must perform no Material filesystem access, byte
verification, hashes, root checks, or full completeness evaluation.

## 24. Today web contract

The existing `#today` workspace remains the only frontend surface changed by
R5.

The browser must:

- make one authoritative Today GET for the visible snapshot;
- render sections in the exact server-defined category order;
- never parse timestamps to decide a bucket;
- never join, regroup, score, or sort Today items;
- render all API/external strings with safe DOM construction and `textContent`;
- show UTC-classification guidance while displaying timestamps in local browser
  time;
- keep inline reminder completion;
- navigate all other actions to their authoritative workspaces;
- expose a **Use learned preferences** control for Review next;
- show total-versus-preview context without expanding into a second Review
  page;
- preserve accessible loading, status, error, empty, and focus states.

Changing the adaptive checkbox may issue a new GET. It never mutates stored
preferences and never triggers an implicit saved search.

## 25. Home unchanged contract

R5 must not change profile-less `GET /report` or Home rendering.

In particular:

- Home remains profile-less;
- Home does not consume R4 preferences;
- Home's broad application categories remain unchanged;
- source due/failure information remains on Home/admin surfaces;
- Home does not adopt Today priority tiers;
- Today changes must not alter Home response shape, ordering, or privacy.

Focused regression tests must prove this unchanged boundary.

## 26. CLI decision

R5 adds no `careeros today` command.

Existing `report`, `review next`, `application`, `reminder`, Preparation web,
and Package web interfaces already cover terminal and detailed workflows. A new
CLI would duplicate a web-oriented composite projection without adding
authority.

## 27. Saved searches and source health

Loading Today never executes a saved search. Execution remains an explicit user
action through the existing command or `POST /saved-searches/{name}/execute`.

Ordinary source-due state is scheduling information, not user attention. A
failed or incomplete collection run also does not prove that manual
intervention is required: existing scheduling can retry it, and CareerOS has no
durable “operator intervention required” state.

Collection/source health therefore remains on Home and administrative
surfaces. R5 adds no Today category for it.

## 28. Reminder and notification boundary

Reminders remain the authority for user-created temporal follow-up and local
notification delivery.

R5 freezes:

- no automatic Reminder creation;
- no inferred follow-up timestamp from Application state or text;
- no second notification concept;
- no change to occurrence identity, delivery claims, retries, or `notify-due`;
- no automatic notification for a Preparation deadline;
- no notification side effect from loading Today.

Preparation deadlines may be visible in Today because they are explicit live
Preparation authority. A user who wants desktop delivery must still create a
Reminder explicitly.

## 29. Privacy and public projection

R5 is local-first, deterministic, model-free, and free of recurring model cost.

The Today HTTP response must not expose:

- Reminder messages;
- Application or Review notes/events;
- requirement labels or details;
- Package answers;
- Material names, filenames, versions, hashes, storage keys, or paths;
- Material/root verification results;
- private candidate-profile paths;
- raw learned evidence or provenance;
- provider error strings;
- raw scorer components or diagnostics outside the existing approved public
  explanation projection.

Allowed compact fields include public Job data, current Application status,
explicit qualifying deadline timestamps, Preparation category/blocking count,
narrow draft facts, final score, and the existing safe ranking explanation.

No Today query may read event-note tables merely to build attention cards.

## 30. Concurrency and stale-response contract

`GET /today` remains read-only, lock-free with respect to collection writes,
and coherent through one read transaction.

The frontend must retain independent Today guards for:

- view generation;
- request sequence;
- data generation;
- refresh ownership and dirty coalescing;
- reminder mutation identity/in-flight state;
- navigation away during reads or writes;
- focus recovery after removal/reclassification.

A stale GET must not render after a newer GET or after leaving Today. A
successful Reminder PUT followed by a failed refresh reports “saved, refresh
failed”; its retry is GET-only. A late failed write must not overwrite a newer
view generation.

Application, Preparation, Package, and Review mutations occur outside Today.
Returning to or explicitly refreshing Today obtains a new authoritative
snapshot.

## 31. Test architecture

Tests are evidence for the frozen behavior; they must use temporary databases
and, where applicable, temporary Material roots.

### 31.1 Application-work domain tests

Cover:

- status-only `offer` inclusion;
- status-only `interview` inclusion;
- status-only `applied` and `interested` exclusion;
- `rejected`/`withdrawn` exclusion;
- persisted Preparation eligibility for `applied`/`interested`;
- open Package draft eligibility for `applied`/`interested`;
- the absence of a Preparation row not qualifying those statuses;
- one item when status, Preparation, Package, and deadline all coexist;
- exact stage/update/ID ordering;
- status transitions that add, retain, reclassify, or remove attention.

### 31.2 Deadline tests

Cover every authority predicate independently:

- deadline kind versus other kinds;
- required versus optional;
- open versus satisfied/not-applicable;
- confirmed versus unconfirmed;
- explicit aware timestamp versus absent/invalid state;
- exact `as_of` and UTC-midnight boundaries;
- earliest of several qualifying deadlines;
- overdue/due-today/non-temporal partition disjointness;
- reschedule, deletion, and not-applicable transitions.

Unconfirmed deadlines must demonstrate Tier-3 Preparation work without
temporal classification.

### 31.3 Package tests

Cover:

- no Package versus finalized-only versus open draft;
- current and mismatched Preparation revision;
- approval absent/current/stale for compact display;
- safe next action when draft finalizability is unknown;
- no Material filesystem access or completeness invocation;
- no answers, Material metadata, fingerprints, or completeness reasons in the
  projection;
- draft finalization/deletion removing or changing Today attention.

### 31.4 Existing-category tests

Preserve and extend tests for:

- Reminder inclusion, boundaries, ordering, independent totals, and concrete
  Application context;
- shortlist decision resolution, conflicts, canonical membership, ordering,
  and any-member Application exclusion;
- missing connection and naive-time validation.

### 31.5 Review-next tests

Cover:

- stored explicit authority;
- absent versus configured-empty explicit state;
- active/inactive/suppressed/locked learned state;
- adaptive enabled and opt-out;
- zero effective learned adjustment;
- legacy `FeedbackSet` absence;
- minimum score one;
- candidate total, score-qualified total, and preview limit three;
- canonical grouping and any-member Review/Application exclusion;
- frozen score/publication/representative ordering;
- no private profile-path fallback;
- one preference snapshot and no per-item preference queries.

### 31.6 API and privacy tests

Assert the exact response envelope and structural absence of every private
field listed in section 29. Verify missing-database behavior and exact query
validation.

### 31.7 Home regression tests

Prove profile-less `GET /report` and Home rendering remain unchanged and do not
acquire adaptive behavior or Today sections.

### 31.8 Frontend contract tests

Cover:

- exact section order;
- server-owned ordering/no browser sorting;
- safe DOM rendering;
- empty and partial states;
- adaptive opt-out request propagation;
- inline Reminder write and GET-only retry;
- navigation destinations;
- stale-response, concurrent refresh, and focus recovery behavior.

## 32. Browser and human qualification

Browser acceptance is Playwright-first against a temporary, deterministic
database.

Automate at least:

1. exact category order with representative seeded states;
2. status-only offer/interview cards and absence of applied/interested cards;
3. one-card behavior when an Application has several qualifying facts;
4. Reminder completion, removal/reclassification, and focus recovery;
5. Preparation, Package, Application, Reviewed, and Review navigation;
6. adaptive enabled/disabled Review-next results;
7. navigation away during delayed GET/write completion;
8. hostile external strings rendered as text;
9. empty Today and zero-personalization states.

After automation, perform only a short human usability smoke test:

1. confirm hierarchy and scanability;
2. complete one Reminder;
3. open one Application/Preparation/Package item;
4. move from Review next to Review and back.

No migration/recovery rehearsal is required because R5 plans no schema change.

## 33. Milestone sequence

R5 is implemented in four coherent milestones.

### M1 — Application-work projection

**Objective:** extend the Today domain/API with one deterministic Application
item per eligible concrete Application.

**Production scope:**

- batched read-only Application-work query;
- active Application statuses;
- status-only `offer` and `interview`;
- persisted Preparation state;
- confirmed required open Preparation deadlines;
- open Package drafts;
- narrow draft/Preparation revision mismatch;
- public Job projection;
- exact classification, ordering, totals, and next-action state;
- Today models, service, database reads, presenters, API schemas, and
  `GET /today` projection.

Likely production files:

- `src/careeros/today/models.py`;
- `src/careeros/today/service.py`;
- `src/careeros/database/today.py`;
- `src/careeros/presenters.py`;
- `src/careeros/api/schemas.py`;
- `src/careeros/api/main.py`.

**Tests:** focused Today domain/API/privacy/query-bound tests. Tests must include
the full status matrix, deadline predicates/boundaries, one-card rule, narrow
Package facts, transitions, totals, ordering, and absence of private fields or
Package completeness work.

**Non-goals:** frontend; scorer; R4 preferences; Review next; Home; schema;
notifications; saved searches; collection/source behavior; Package
completeness/finalization implementation.

**Acceptance gate:**

- exact eligibility and classification contract passes;
- status-only offer/interview behavior passes;
- applied/interested status-only exclusion passes;
- one item per Application passes;
- all deadline authority and boundary cases pass;
- only narrow Package facts are read;
- query count is independent of result count;
- privacy allow-list passes;
- `SCHEMA_VERSION == 12`;
- focused lint, mypy, and tests pass in implementation work.

**Intended commit boundary:** `Add application work to Today snapshot`.

### M2 — R4-aware Review next

**Objective:** add the three-item, score-qualified, stored-preference Review
preview to the Today service and API.

**Production scope:** existing Review queue/scorer reuse; stored explicit
authority; one adaptive preference snapshot; opt-out; safe public projection;
candidate and qualified totals.

**Tests:** explicit/empty/default authority, learned activation/suppression/
locks, opt-out, zero state, no FeedbackSet, score threshold, totals, preview
limit, grouping, ordering, query behavior, and privacy.

**Non-goals:** new scorer, weights, preference persistence, saved-search
execution, Review mutation UI, frontend rendering.

**Acceptance gate:** shared frozen scorer and R4 authority are reused unchanged;
direct FeedbackSet is absent; score/order match shared behavior; no per-item
preference queries or model calls.

**Intended commit boundary:** `Add adaptive Review next preview to Today`.

### M3 — Today web evolution

**Objective:** render the complete Workflow / Attention Center and its existing
authoritative navigation safely.

**Production scope:** Today HTML only if needed for controls; plain JavaScript
rendering/interactions; CSS; learned-preference control; navigation; accessible
empty/loading/error/focus behavior.

**Tests:** static frontend contracts and deterministic Playwright flows,
including concurrency, navigation, safe DOM use, Reminder completion, and
adaptive opt-out.

**Non-goals:** frontend framework/build tooling; new workspace; inline
Application/Preparation/Package/Review mutation logic; Home changes.

**Acceptance gate:** exact server order renders without client classification;
only Reminder completion writes inline; stale responses cannot corrupt the
visible view; Playwright acceptance passes.

**Intended commit boundary:** `Evolve Today workflow center`.

### M4 — Qualification and documentation

**Objective:** qualify the integrated R5 slice, confirm regression boundaries,
and make user/developer documentation factual.

**Production scope:** documentation corrections only unless qualification
finds an in-scope defect.

**Tests/qualification:** lint → mypy → focused tests → complete suite;
Playwright automation; short human smoke; packaged/static checks appropriate to
the changed surfaces.

**Non-goals:** new features, migrations, source expansion, or broad
Core-v1 work.

**Acceptance gate:** all automated qualification is clean; Home/R4/schema/
privacy boundaries are unchanged; human smoke finds no blocking usability
defect; docs match behavior.

**Intended commit boundary:** `Qualify and document R5 workflow center`.

## 34. First implementation package

The first bounded implementation package is:

> **R5 M1 — Application-work projection**

M1 must implement only the Today domain/API projection described above. Its
core deliverable is a batched read that returns one item per eligible concrete
Application, partitioned into overdue, due-today, or non-temporal attention.

M1 explicitly includes:

- status-only `offer` and `interview`;
- status-only `applied`/`interested` exclusion;
- persisted Preparation work;
- authoritative confirmed required open deadlines;
- open Package drafts;
- `draft_preparation_revision_stale` only;
- current-draft approval as compact context, never finalizability proof;
- exact stage/update/ID ordering;
- compact safe next-action values;
- total-before-limit semantics;
- public Job data and strict privacy pruning.

M1 explicitly excludes:

- frontend changes;
- Review-next and any scoring;
- R4 preference work;
- Home;
- schema/migration work;
- Reminder notification changes;
- saved-search or source-health work;
- full Package completeness, Material verification, or finalization logic;

M1 is complete only when its focused tests demonstrate every inclusion,
exclusion, transition, ordering, privacy, and query-bound rule without changing
schema v12.

## 35. Explicit R5 non-goals

R5 does not include:

- source expansion;
- collector or scheduler redesign;
- a generic task manager;
- a generic calendar;
- automatic Application submission, email, or outreach;
- inferred Application deadlines or follow-up dates;
- automatic Reminder creation;
- new notification channels or notification semantics;
- an AI/LLM prioritizer or recurring model cost;
- MCP/Superpowers work;
- scorer or ranking-weight redesign;
- R4 preference/evidence redesign or backfill;
- direct FeedbackSet on HTTP/web Today;
- automatic saved-search execution;
- source-health escalation into Today;
- a CLI `today` command;
- a parallel `/attention` endpoint or workspace;
- persisted attention rows or caches;
- schema v13;
- Package completeness duplication;
- broad Core-v1 audit work.

## 36. Risks and frozen mitigations

| Risk | Frozen mitigation |
|---|---|
| Today becomes a generic task list | Admit only the exact categories and authority in sections 7–10 |
| Every Application becomes “attention” | Status-only inclusion is limited to `offer` and `interview`; applied/interested need persisted work |
| Duplicate cards for one Application | Partition after eligibility and return exactly one item per `applications.id` |
| Invented urgency | Tier 1/2 require explicit Reminder or confirmed required open Preparation deadline |
| Offer/interview implies a fake deadline | Keep them non-temporal Tier 3 and expose no due timestamp |
| Package status is mispredicted | Project only draft existence, current-revision approval, and Preparation-revision mismatch; defer completeness |
| Material I/O makes Today expensive/unsafe | Prohibit Package completeness and all Material filesystem verification |
| Review next becomes a second Review page | Limit to three score-qualified grouped items and navigation only |
| Zero-state feed is mislabeled personalized | Require match score at least one; expose bounded `personalized` context |
| Adaptive behavior diverges from R4 | Reuse one R4 authority snapshot and shared scorer; no direct FeedbackSet |
| N+1 queries | Batched Application/Preparation/Package reads and fixed query-bound tests |
| Incoherent cross-section times | One captured UTC instant and one read transaction |
| Private workflow text leaks | Explicit public allow-list and structural absence tests |
| Stale frontend results overwrite current state | Preserve Today generation/request/mutation guards and GET-only refresh retry |
| Home becomes personalized or action-queue shaped | No Home production change; focused regression contract |
| Derived projection becomes new authority | No persistence; all disappearance/reclassification follows authoritative source transitions |

## 37. Freeze statement

This document is the canonical implementation contract for R5 — Workflow /
Attention Center.

The architecture is frozen around these invariants:

- Home remains the profile-less operational overview.
- Today is the one Workflow / Attention Center.
- Offer and interview are bounded status-only Tier-3 attention states.
- Applied and interested status alone are not attention.
- Explicit Reminders and confirmed Preparation deadlines are the only temporal
  Today authorities.
- One concrete Application produces at most one Today Application card.
- Package completeness remains owned entirely by the Package workflow.
- Review next reuses R4 and is limited to three score-qualified opportunities.
- Today does not execute saved searches or escalate source health.
- No attention state is persisted.
- `SCHEMA_VERSION` remains 12.
