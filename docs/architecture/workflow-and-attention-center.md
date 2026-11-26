# Workflow and Attention Center

Today is a deterministic, server-authoritative local snapshot answering what
needs attention now. It composes existing reminders, application records,
review decisions, duplicate groups, jobs, and ranking preferences; it is not a
second workflow database, generic task manager, deadline inference engine, or
application-submission service.

One UTC `as_of` instant and one coherent SQLite read snapshot classify overdue
and due-today reminders, application work, shortlisted-but-unstarted jobs, and
the bounded Review-next preview. Each category has deterministic totals,
limits, priority, and tie-breaks. The browser renders server classifications;
it does not reclassify times, join state, or reorder items. Actions delegate to
the owning reminder/application/review endpoint.

## Current category and ordering contract

The response exposes sections in this order: overdue reminders, overdue
application work, reminders due later today, application work due later today,
non-temporal application work, shortlisted-but-unstarted opportunities, and
Review-next. Reminder items sort by `due_at ASC, reminder_id ASC`. Application
items in the two temporal sections sort by earliest confirmed open deadline,
application stage, most recently updated first, then application ID. The
non-temporal application section sorts by stage, most recently updated first,
then application ID. Stage priority is `offer` (0), `interview` (1),
`applied` (2), `interested` (3). Shortlisted opportunities sort by decision
updated time descending, decision ID descending, representative job ID
ascending; member rows then use job ID ascending. Review-next uses persisted
rank ordinal and returns at most three unreviewed, untracked opportunities with
public score at least 1.

Application classification and next action use this precedence:

| Condition | Classification / next action |
| --- | --- |
| Earliest confirmed open deadline is before `as_of` | `overdue` / `review_preparation_deadline` |
| Earliest confirmed open deadline is before end of day | `due_today` / `review_preparation_deadline` |
| Preparation is unknown or incomplete | `attention` / `complete_preparation` |
| Preparation requirements are satisfied but not confirmed ready | `attention` / `confirm_preparation_ready` |
| Package draft is stale against preparation revision | `attention` / `review_rebase_package` |
| Current package draft exists | `attention` / `continue_package` |
| Otherwise eligible application work | `attention` / `manage_application` |

Only active application statuses (`interested`, `applied`, `interview`,
`offer`) participate. Work is eligible when status is offer/interview,
preparation is not user-confirmed-ready, a package draft exists, or a confirmed
deadline is today/overdue. All comparisons use UTC and invalid timestamps do
not become inferred deadlines.

Application attention is based on local status and reminder state. It never
claims an employer deadline, sends outreach, submits an application, or
creates workflow state outside the owning tables. Package/preparation
information is a narrow read projection where present. Review-next reuses the
deterministic scorer and optional adaptive preferences with explicit opt-out;
it does not mutate feedback or learned state.

The loopback frontend uses request-sequence, generation, mutation, focus, and
safe-DOM guards. Stale responses cannot overwrite newer state. See
[`today-materialized-ranking.md`](today-materialized-ranking.md) for the
materialized ranking dependency.
