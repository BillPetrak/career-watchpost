# CareerOS

CareerOS is a privacy-first career intelligence platform.

**Product 1 v1.0 is released and release-complete.** Core, H1 production
hardening, H2 visual polish, and integrated RC qualification are complete. The
seven existing job sources are the frozen v1.0 source set. Resume/CV
sanitization, structured extraction, and Candidate Profile/Candidate Identity
intelligence are planned for Product 1 v1.05; resume Materials remain opaque
stored bytes in v1.0 and are not parsed. Additional source expansion remains
planned for Product 1 v1.1.## Product 1 v1.05 M2A persistence foundation

Schema v13 adds the local persistence and pure domain foundation for immutable
resume extraction/sanitization artifacts and human-authoritative Career
Profiles. It does not parse Material bytes, run sanitization, extract facts,
call models, or add API/CLI/UI workflows; those operations remain later
milestones. Existing matching profiles and Matching Preferences keep their
v1.0 meaning.

Upgrade an existing supported database explicitly while CareerOS processes
using it are stopped:

```bash
uv run careeros database migrate --database /path/to/careeros.sqlite3
```

The normal migration creates
`/path/to/careeros.sqlite3.pre-v13.backup.sqlite3` unless `--backup` selects
another destination or the acknowledged no-backup path is used. There is no
in-place downgrade. Restore that database-only backup with `database restore`;
Materials and private configuration must still be preserved separately.

## Adaptive matching preferences (R4)

CareerOS schema v12 adds persistent **Matching Preferences** to the local
SQLite database. Explicit preferences are the profile values you deliberately
save; learned preferences are deterministic, structured signals derived from
meaningful `shortlist` and `dismiss` review transitions. Existing reviews are
not backfilled during upgrade, `review_later` is neutral, and review notes and
raw job descriptions never become preference evidence.

Learned preferences remain inactive until repeated evidence crosses the frozen
threshold (two positive opportunities or three negative opportunities with a
net difference of at least two). Canonical duplicate opportunities count once.
You can inspect evidence counts and bounded example opportunities, suppress or
restore one learned preference, or reset only learned state while preserving
explicit preferences and review history:

```bash
uv run careeros preference show --format text
uv run careeros preference explicit replace config/candidate_profile.example.yaml
uv run careeros preference explicit clear
uv run careeros preference suppress KEY
uv run careeros preference restore KEY
uv run careeros preference reset-learned --confirm
```

The Matching Preferences web workspace provides the same explicit save/clear
and learned inspection, suppression, restore, and reset controls. A deliberately
saved empty explicit profile remains configured, creates no learned-dimension
locks, and does not silently fall back to a private profile file.

Active learned preferences apply a bounded adjustment to ranked surfaces,
including rank, Review, rank-mode saved searches, ranked reports/exports, and
their HTTP/web equivalents. Use `--no-adaptive` on supported CLI commands, or
turn off **Use learned preferences** in the web UI, to disable only this learned
layer. This is distinct from `--no-feedback`, which controls the pre-existing
direct review-feedback adjustment on CLI/service paths. Ranked explanations
show only the effective learned points actually applied; full evidence remains
in the dedicated preference inspection surface.

Preference learning, ranking, and explanations are local, deterministic, and
model-free: they make no LLM call and have no recurring model cost. Chronological
search, administrative/audit views, and the profile-less Home report do not
become personalized feeds. Today uses preferences only for its bounded Review
next section, with **Use learned preferences** controlling the learned layer for
that invocation.

## Private reusable Materials (R3 M2)

CareerOS schema v11 includes a local Materials library for reusable private
documents. Versions are immutable and globally reusable; no material belongs
to a job or application. File bytes remain outside SQLite in a managed XDG
root outside the Git checkout. The root is private and marker-bound to the
database; backups of SQLite do not include these bytes.

Use `careeros material list|show|import|add-version|update|retire|restore`
for administration, `export-version` for an explicit verified copy, and
`audit` for a read-only consistency check. Development and migration tests
must use temporary databases and roots. CareerOS never implicitly migrates an
old database or creates a Materials root during startup/read-only operations;
production migration to v11 is a separate explicit decision.

Schema v11 also adds job-specific Application Packages for tracked jobs. A
logical package retains at most one mutable draft plus immutable finalized
versions. Drafts snapshot the concrete job and Preparation requirements, bind
Material requirements to exact library versions, and keep user-authored
answers private to explicit version-detail responses. Approval binds to the
current draft revision; finalization verifies current Preparation/job authority
and selected Material bytes before atomically freezing that same version row.
Package finalization never changes Application status, Review decisions,
reminders, Preparation acknowledgements, jobs, or Material bytes.

The HTTP lifecycle is available under
`/applications/{job_id}/package` and `/application-package-versions/{id}`.
Unsafe operations require `X-CareerOS-Write: 1`; semantic draft mutations use
independent package and Preparation revision headers. Pipeline responses expose
only answer-free lifecycle facts. There is no package CLI or generic export.

## Design principles

- Local-first storage
- Privacy by design
- Modular and replaceable components
- Explainable AI-assisted ranking
- Cost-aware model usage
- Automated testing
- Clear documentation

## Architecture

CareerOS owns its data, business logic, and provider interfaces.

External systems such as Hermes and LLM providers will be treated as
replaceable integrations rather than permanent dependencies.

## Product 1 capabilities

Current CLI capabilities include multi-source job collection (`collect
<source>`, `collect all`), rate-aware scheduled collection (`collect due`) with
persistent run history, read-only collection observability (`collect status`,
`collect history`), plus `search`, `show`, `rank`, `group`,
`saved-search`, `application`, `reminder` (including local `notify-due`/`
notify-history` notifications), `review`, `preference`, `profile` (init/show/
validate for JSON or YAML candidate profiles), a read-only `report` (Career
Snapshot), and `diagnose` (including notification readiness).
Collection commands are mutually exclusive per database via an OS advisory
lock, while the read-only commands remain available during collection; a lock
conflict exits with code 75. See `AGENTS.md` for the full command reference.

### Installed release and private runtime state

CareerOS requires Python 3.11 or newer. An installed user can run a downloaded
or locally supplied release wheel without a Git checkout. Create an isolated
environment and install the wheel with the API dependencies:

```bash
python3.11 -m venv ~/.venvs/careeros
source ~/.venvs/careeros/bin/activate
python -m pip install '/path/to/careeros-1.0.0-py3-none-any.whl[api]'

careeros --help
python -m careeros --help
careeros collect arbeitnow --limit 10  # new state only: create schema v13 + collect
careeros serve                         # then open http://127.0.0.1:8000/ui
```

This route installs an existing release artifact; it does not imply publication
to PyPI. The server deliberately refuses a missing, uninitialized, or old-schema
database. The collection command above is the supported first-run bootstrap for
new private state; users who already have a current schema-v12 database should
skip it. The wheel and source checkout use the same stable XDG-compatible defaults:

- database: `${XDG_DATA_HOME:-~/.local/share}/careeros/careeros.sqlite3`
- managed Materials: `${XDG_DATA_HOME:-~/.local/share}/careeros/materials`
- private profile: `${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml`

`--database` is authoritative when supplied; otherwise
`CAREEROS_DATABASE` overrides the database default. An explicit
`--materials-root` is authoritative where supported; otherwise
`CAREEROS_MATERIALS_ROOT` overrides the Materials default. Explicit profile
paths always win. `~`, `XDG_DATA_HOME`, and `XDG_CONFIG_HOME` are expanded,
and read-only operations do not create absent state. Recreating a wheel or
virtual environment therefore does not move private state.

CareerOS does not silently migrate the former checkout-local defaults. An
existing user should stop writers, make a supported SQLite backup, copy the
old database, matching Materials root, and private profile deliberately to the
paths above (or keep using explicit environment/CLI overrides), then run
`careeros material audit` against the selected database and Materials root.

A local web dashboard (Home, Today, Jobs, Review, Reviewed, Applications,
Reminders, Matching Preferences, and a per-job detail view) is served by the
optional FastAPI slice at <http://127.0.0.1:8000/ui>. For contributors working
from a source checkout, install development and API dependencies separately:

```bash
uv sync --extra development --extra api   # install the API extra
uv run uvicorn careeros.api:app           # serve; then open /ui in a browser
```

Representative documented read endpoints include `GET /report`, `GET /today`,
`GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/group`,
`GET /applications?job_id={id}`, `GET /review-decisions?job_id={id}`,
`GET /reviewed-opportunities`, and `GET /reminders?job_id={id}`. The UI also uses the
computation-only `POST /report` (the Review view's inline candidate-profile
form) and the workflow mutations: the Job Detail review-decision
`PUT`/`DELETE /review-decisions/{id}`, application-status
`PUT /applications/{id}`, and reminder `POST /reminders`,
`PUT /reminders/{id}/complete` and `PUT /reminders/{id}/reopen`, plus a
confirmed `DELETE /reminders/{id}` that always shows a native confirmation
dialog before removing a reminder — and, in the Review view, per-card
review-decision buttons (`Shortlist` / `Dismiss` / `Review later`) on every
Review Queue card, which send the same `PUT /review-decisions/{id}`
(representative job id in grouped mode) and let the refreshed server response
alone remove the decided opportunity. After a successful card decision, the
frontend refreshes only the authoritative `POST /review-queue` section using
the same profile/grouping/limit fields; it does not rerun the full `POST
/report`, and the last authoritative Recommendations response is preserved.
The top-level Reviewed workspace loads reviewed opportunities without ranking,
defaults to Shortlist, and provides Review later and Dismissed tabs, grouped or
flat display, authoritative counts, pagination, decision changes, and clearing.
Grouped actions target the server-returned concrete `decision_job_id`, not
necessarily the displayed representative. Reviewed mutations refresh only
`GET /reviewed-opportunities`; Shortlist remains evaluation intent and never
creates or changes an application status.
The Applications workspace is flat and concrete-job-owned: `GET
/application-pipeline` returns server-authoritative status counts and a paginated
page ordered by application `updated_at` then id. `interested`, `applied`,
`interview`, and `offer` are active; `rejected` and `withdrawn` are completed.
It reuses `PUT /applications/{id}` for status changes and refreshes only the
pipeline read. Application state never rewrites Review decisions, reminders,
or duplicate-group members; duplicate listings may therefore have independent
application records and histories.
Overlapping successful decisions are coalesced into one queue refresh followed
by one dirty rerun when needed. The Job Detail application/review reads use
the job-scoped list endpoints so the normal absent state ("Not tracked." /
"Not reviewed.") is an empty 200 list rather than a canonical 404; the
singular `GET /applications/{id}` and `GET /review-decisions/{id}` endpoints
keep their authoritative 404 absent-state behavior for API clients. These
mutations are local database writes, but they never rewrite Job rows or
saved-search definitions and never contact employers or external services.

Reminder notifications are the third, **independent** piece: a background
systemd user timer (`careeros-reminder-notify.timer`) runs
`careeros reminder notify-due --backend auto` and surfaces due reminders as
local desktop pop-ups (via `notify-send`) even when no browser tab and no
server is open. Installing it is opt-in and separate from the web UI/server:

```bash
careeros systemd install --units notify --enable   # install + enable the notify timer
```

`careeros diagnose` reports notification readiness (notify-send availability,
which backend `auto` resolves to, and whether the notify timer is
installed/enabled/active) with actionable guidance. Delivery is local only —
CareerOS never uses web push, email, or any external service — and each
reminder fires exactly once per scheduled time (rescheduling counts as a new
occurrence, and a due time missed across a reboot still fires once).

## Job sources

CareerOS collects from the following public job feeds, each fetched with a
plain HTTPS request through the shared collector framework (stdlib `urllib`
only, no API keys, no browser automation):

| Source | Feed | Polling floor |
|---|---|---|
| Arbeitnow | `https://www.arbeitnow.com/api/job-board-api` (JSON) | 6 hours (provisional) |
| Remote OK | `https://remoteok.com/api` (JSON) | 4 hours (provisional) |
| We Work Remotely | `https://weworkremotely.com/remote-jobs.rss` (RSS) | 6 hours (provisional) |
| Jobicy | `https://jobicy.com/api/v2/remote-jobs` (JSON) | 1 hour (documented) |
| Remotive | `https://remotive.com/api/remote-jobs` (JSON) | 6 hours (documented) |
| Working Nomads | `POST https://www.workingnomads.com/jobsapi/_search` (JSON) | 6 hours (provisional) |
| Himalayas | `https://himalayas.app/jobs/api` (JSON) | 24 hours (documented) |

Every shared GET/POST helper enforces a 16 MiB response-body ceiling using
both declared `Content-Length` and a bounded actual read; an oversized response
is a collection failure, never an empty feed. Jobicy requests at most the
provider's documented 200-result maximum. These seven sources are the frozen
v1.0 set; additional source expansion belongs to v1.1.

Remotive's terms require attributing listings with a link back to the original
URL and allow polling at most four times a day; its listings are published with
a 24-hour delay. Jobicy requires similar credit. CareerOS always stores each
job's original URL and labels its source, and `collect due` enforces the
polling floors above.

Each source keeps its committed start marker for crash observability, then
commits job upserts, derived grouping, and successful run bookkeeping in one
transaction. A pre-commit interruption leaves only an `incomplete` attempt;
the newest incomplete attempt is due immediately on restart, and startup of a
collection command deterministically repairs grouping before the retry. A
failed attempt remains distinct and follows the normal polling interval.

Working Nomads is a remote-only board and does not publish a documented public
API: its collector targets the same internal Elasticsearch search endpoint that
powers the public listing pages, so the endpoint is undocumented and may change
without notice. CareerOS sends only a minimal bounded request and treats any
malformed response as a collection failure rather than an empty result.

Himalayas publishes a free, documented public JSON API that requires no API
key or authentication. Its browse endpoint returns at most 20 jobs per request,
and the feed is cached and refreshed every 24 hours, so CareerOS enforces a
documented 24-hour polling floor; the API rate-limits excessive volume with
HTTP 429.

## Cross-source duplicate detection

CareerOS detects when the same vacancy is listed by several job boards using
deterministic, stdlib-only normalization (`group regroup` recomputes the
canonical groups; `group explain` shows them read-only). Grouping is
non-destructive: the `jobs` table is the source of truth and the derived group
tables never mutate it. `search --grouped` and `rank --grouped` collapse
cross-source duplicates into one result per group.

## Application tracking

CareerOS records what you have done with a vacancy so already-handled jobs can
be recognized later. Tracking is **local workflow state only**: CareerOS never
submits applications, sends email, or contacts any external service on your
behalf.

One current application record is stored per tracked job (keyed by the stable
`jobs.id`) together with an append-only history of every status transition.
Supported statuses: `interested`, `applied`, `interview`, `offer`, `rejected`,
`withdrawn`.

```bash
uv run careeros application set 12 applied --note "sent CV"   # track + note
uv run careeros application set 12 interview                  # transition
uv run careeros application show 12                           # current state
uv run careeros application list --status applied             # filtered list
uv run careeros application history 12                        # event history
```

Repeating the same status with no note is an idempotent no-op; adding a note
appends a same-status history event. Every write is transactional, and records
are anchored to `jobs.id` (never to the derived grouping tables), so
`group regroup` can never delete or orphan application history.

`search` and `rank` accept explicit tracking filters — `--untracked`,
`--status STATUS` (repeatable), and `--exclude-status STATUS` (repeatable) —
which show the actual state (`Application: Applied`, `Application: Untracked`)
on matching results. Without these flags the default output is unchanged.

## Reminders

CareerOS lets you attach local follow-up reminders to a stored job so a planned
next step is not lost. Reminders are **local workflow state only**: CareerOS
records and reports them locally, and it does not send messages, contact
employers, submit applications, or infer deadlines. Reminder commands are
`careeros reminder <add|list|show|update|complete|reopen|delete>`.

```bash
uv run careeros reminder add 12 interview 2026-08-10T14:00:00Z \
  --message "second round call"            # add a reminder
uv run careeros reminder list              # open reminders, soonest first
uv run careeros reminder list --overdue    # open + past due
uv run careeros reminder list --upcoming   # open + future
uv run careeros reminder list --completed  # done reminders
uv run careeros reminder show 3            # one reminder in detail
uv run careeros reminder update 3 --kind interview --due-at 2026-08-11T09:00:00+02:00
uv run careeros reminder update 3 --clear-message # drop the message
uv run careeros reminder complete 3        # mark done (idempotent)
uv run careeros reminder reopen 3          # reopen (idempotent)
uv run careeros reminder delete 3          # remove the reminder
```

Reminder kinds are validated centrally: `follow_up`, `interview`, `offer`,
`review`, and `custom`. A reminder is anchored to the stable `jobs.id` (never
the derived grouping tables), so `group regroup` can never delete or orphan it.

Due timestamps are deliberately strict and deterministic: a full ISO-8601
timestamp with an explicit UTC offset is required (`2026-08-10T14:00:00Z` or
`+02:00`); naive and date-only values are rejected because CareerOS never
guesses a timezone. Values are normalized and stored as UTC. `complete` and
`reopen` are idempotent no-ops; `update` only changes fields you pass and
supports deliberate message clearing. The write commands (`add`, `update`,
`complete`, `reopen`, `delete`) respect the per-database lock and exit 75 on
contention; the reads (`list`, `show`) never lock.

### Reminder notifications

Separate from storing/editing reminders, `careeros reminder notify-due` delivers
a **local** notification when a reminder comes due, so a due reminder surfaces
without keeping the web UI open. Delivery is **local only** — never web push,
email, or any external service — and never completes, reschedules, or deletes a
reminder. The delivery audit lives in the `reminder_notifications` table.

```bash
uv run careeros reminder notify-due --dry-run          # preview due reminders (no delivery)
uv run careeros reminder notify-due                    # deliver due reminders
uv run careeros reminder notify-due --backend stdout   # force text-line delivery
uv run careeros reminder notify-history                # delivery history, newest first
uv run careeros reminder notify-history --failed       # failed attempts only
```

Occurrence semantics: a reminder notifies **once per scheduled time** (the
`(reminder_id, due_at)` pair). Rescheduling (`update --due-at`) creates a new
occurrence and notifies again; reopening a completed reminder does **not**
re-notify a time already delivered; deleting or completing a reminder silences
it; an overdue time notifies exactly once when first observed — including
across a reboot that spanned the due time.

`--backend auto` (the default) uses the `desktop` backend when `notify-send`
is on `PATH` **and** a session bus is reachable (the standard
`DBUS_SESSION_BUS_ADDRESS` or `$XDG_RUNTIME_DIR/bus` socket), otherwise it
falls back to `stdout` with an explanatory note. The bus — not the
`DISPLAY`/`WAYLAND_DISPLAY` variables — is the delivery signal, so the systemd
user timer delivers visible pop-ups inside a graphical login session even when
the user manager inherits no display environment. `auto` never touches the
network and keeps retrying failed deliveries on later runs. "Delivered" means
notify-send (the desktop notification service) accepted the notification with
exit code 0; CareerOS cannot prove that your desktop rendered a visible banner,
so a transient or unnoticed pop-up is presentation behavior after a successful
delivery, not a delivery failure.

The independent systemd user timer runs the notifier periodically; see
"Scheduled collection" below for installation. `careeros diagnose` verifies
notification readiness end to end.

## Daily review queue

CareerOS gives you a **personalized daily review queue** for deciding what to do
with each new opportunity. For every stored job you can record a lightweight,
private review decision:

- `shortlist` — worth pursuing; keep it on your radar.
- `dismiss` — not a fit; hide it from future queues.
- `review_later` — revisit it later without committing either way.

Decisions are **local workflow state only** — CareerOS never shares them, never
contacts employers, and never submits applications. Each decision is stored per
job (keyed by the stable `jobs.id`) with an append-only history of every
transition, so `group regroup` can never delete or orphan your reviews. Clearing
a decision returns the job to the unreviewed pool.

```bash
uv run careeros review set 12 shortlist --note "top pick"   # decide + note
uv run careeros review set 12 dismiss                       # change your mind
uv run careeros review show 12                              # current decision
uv run careeros review list --decision shortlist            # filtered list
uv run careeros review history 12                           # decision history
uv run careeros review clear 12                             # back to unreviewed
```

`review next` ranks today's queue against your candidate profile. It shows
**unreviewed** opportunities by default, collapsed into one entry per canonical
duplicate group (**grouped by default**; use `--no-grouped` for one entry per
job). Every eligible opportunity is scored with the shared ranking engine and
the queue is ordered best-first:

```bash
uv run careeros review next --profile config/candidate_profile.example.json
uv run careeros review next --profile ... --state shortlist --min-score 60
uv run careeros review next --profile ... --include-tracked
uv run careeros review next --profile ... --no-grouped --format json
```

- `--state` picks the queue filter (`unreviewed`, `shortlist`, `dismiss`,
  `review_later`, or `any`); the default is `unreviewed`.
- `--min-score` trims results below a match-score threshold after ranking.
- By default, opportunities that already have an application record are hidden;
  `--include-tracked` brings them back (annotated with their application
  status).
- `--format text` (default) and `--format json` render the same ranked queue.

`review next`, `show`, `list`, and `history` are read-only and never lock. The
write commands `review set` and `review clear` respect the per-database lock and
exit 75 on contention, matching the rest of the suite.

## Candidate profile

Ranking and the Career Snapshot score jobs against a **candidate profile** — a
small file describing the roles you are looking for. Profiles may be written in
**JSON** (`.json`) or **YAML** (`.yaml`/`.yml`); the format is chosen by file
extension. All fields are optional and unknown top-level fields are silently
ignored.

Supported fields:

- `desired_titles` — titles to match (e.g. `"Machine Learning Engineer"`)
- `skills` — preferred skills to look for in the posting
- `required_skills` — skills that must appear (missing ones are penalized)
- `preferred_locations` — locations that count as a match
- `remote_preference` — an optional provider/domain-specific string (trimmed,
  up to 50 characters), or `null`; the API does not narrow this vocabulary
- `excluded_terms` — soft-excluded terms (small penalty)
- `hard_excluded_terms` — hard exclusions that force a score of 0
- `preferred_employment_types` — e.g. `"Full-time"`
- `preferred_seniority` — provider/domain-specific terms (e.g. `"Senior"`);
  each entry is preserved and may be up to 100 characters

### Private profile

The default profile path is
`${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml`. This file is
**private** and must never be committed. It holds your real job-search
preferences, unlike the committed fictional examples
`config/candidate_profile.example.json` and
`config/candidate_profile.example.yaml`.

Initialize your private profile safely (refuses to overwrite an existing file):

```bash
uv run careeros profile init                    # create the XDG private profile
uv run careeros profile init --force            # overwrite an existing profile
uv run careeros profile init ~/profile.yaml     # create anywhere else (.json also works)
```

Then validate or inspect it:

```bash
uv run careeros profile validate                # validate the private profile
uv run careeros profile validate config/candidate_profile.example.json
uv run careeros profile show                    # show the private profile
uv run careeros profile show config/candidate_profile.example.yaml
```

Exit codes: `0` success, `1` missing/invalid profile (and `init` refusing an
existing file), `2` usage errors (unsupported file extension). `init` writes a
starter profile with every field commented or empty; `show` and `validate`
default to the private path and `--force` is required to overwrite an existing
file.

## Saved searches

CareerOS can capture a reusable search or rank workflow so the same filters can
be re-run later without retyping the flags. A saved search stores the query and
filters (*intent*), never cached results: running it always queries the current
database. Running a saved search never submits applications, sends email, or
contacts any external service.

```bash
# Capture a recurring engineer search (search mode)
uv run careeros saved-search create engineer "Engineer" --mode search --remote

# Capture a ranking workflow (rank mode requires --profile)
uv run careeros saved-search create top-roles "Engineer" --mode rank \
  --profile config/candidate_profile.example.json --min-score 70

uv run careeros saved-search list                       # list workflows
uv run careeros saved-search show engineer              # view a definition
uv run careeros saved-search run engineer               # re-run against current DB
uv run careeros saved-search update engineer --limit 50 # change filters
uv run careeros saved-search rename engineer dev        # rename
uv run careeros saved-search delete engineer            # remove
```

`--mode {search,rank}` is required at creation and cannot change afterwards.
Mode-specific rules apply (`--profile` is invalid for `search` and required for
`rank`; `--min-score` is rank-only). Names are case-insensitive for lookup but
stored as given. `saved-search run` output is identical to the explicit
`search`/`rank` commands. The write commands (`create`, `update`, `rename`,
`delete`) respect the per-database lock and exit 75 on contention; the read
commands (`list`, `show`, `run`) never lock.

## Career Snapshot

The **Career Snapshot** is a read-only local report (`careeros report`) that
pulls your stored jobs, application tracking, saved searches, and collection
history into one deterministic screen. It is a dashboard, not an agent: it
never submits applications, never sends email, never fetches jobs, and never
contacts any external service. The report holds no per-database lock, so it
runs fine while a collection is in progress, and it never writes — not even a
`collection_runs` entry.

```bash
uv run careeros report                                   # text (default)
uv run careeros report --format json                     # same data as JSON
uv run careeros report --profile config/candidate_profile.example.json
uv run careeros report --saved-search top-roles          # rank workflow drives the ranking
uv run careeros report --limit 5 --grouped               # cap + collapse duplicates
```

`--format text` (default) and `--format json` are two renderers over the same
immutable report model built by the same read-only reporting service, so both
carry identical data. The text output has seven sections:

- **Overview** — stored jobs, canonical groups, distinct opportunities,
  tracked applications, saved searches, review-decision counters (reviewed,
  unreviewed, shortlisted, dismissed, review-later groups), per-source last-run
  summary, and which sources are currently due.
- **Application pipeline** — one count per status (including `rejected` and
  `withdrawn`) plus the most recently changed applications (newest first,
  capped by `--limit`).
- **Action items** — the pipeline items worth acting on, in prominence order:
  interview attention, offer attention, interested (not applied), and applied
  (awaiting progress). `rejected`/`withdrawn` appear only in the pipeline
  counts, never as actions. CareerOS never invents deadlines or follow-up
  dates from text.
- **Reminders** — overdue count, due-today count, open total, and the urgent
  open reminders (soonest first, capped by `--limit`). "Due today (UTC)" means
  within the current UTC calendar day: CareerOS stores and compares reminder
  times in UTC and never assumes your local timezone.
- **Review queue** — a ranked, personalized daily queue of unreviewed
  opportunities (grouped by default, tracked applications excluded), scored
  against the report's profile. Without `--profile` or `--saved-search` the
  section is honestly skipped with a note.
- **Recommended opportunities** — profile-ranked jobs using the shared ranking
  engine. Without `--profile` or `--saved-search` the section is honestly
  skipped with a note. Tracked jobs are excluded (recommendations only propose
  new opportunities), and `--grouped` collapses cross-source duplicates.
- **Collection health** — per-source last run, outcome, fetched/inserted/
  updated counts, last error, next-due time, and incomplete-run count.

With `--saved-search NAME`, the named **rank-mode** saved search governs the
query, filters, minimum score, and result limit; `--profile` may be passed
alongside it to override the stored profile. Report flags like `--grouped`
cannot be combined with `--saved-search` (the saved search already defines its
own grouping), and a search-mode saved search is rejected. Exit codes: `0`
success (including an empty database), `1` unknown saved search or
profile/database failure, `2` invalid flags.

## Deterministic exports

CareerOS can project its stored state into portable files with `careeros
export`. Exports are **deterministic and read-only**: the same database and
flags always produce byte-identical output, and exporting never fetches jobs,
never writes to the database, never acquires the collection lock (it runs while
a collection is in progress), and never contacts an external service. A missing
database path yields a valid empty export without creating the file.

```bash
uv run careeros export jobs                                          # JSON to stdout
uv run careeros export jobs --grouped --format csv                   # grouped CSV
uv run careeros export ranked --query engineer \
  --profile config/candidate_profile.example.json                    # scored ranking
uv run careeros export review \
  --profile config/candidate_profile.example.json                    # review queue
uv run careeros export source-stats --format jsonl                   # per-source stats
uv run careeros export collection-history --output runs.csv          # to a file
```

Subcommands and their key flags:

- **`jobs`** — stored vacancies. Filters: `--query`, `--remote`, `--location`,
  `--source`, `--employment-type`, `--grouped` (one record per canonical
  duplicate group), `--clean-descriptions`.
- **`ranked`** — jobs scored against a profile (requires `--query` and
  `--profile`). Adds `--grouped`, `--min-score`, and `--no-feedback`.
- **`review`** — the ranked daily review queue (requires `--profile`). Supports
  `--state`, `--min-score`, `--include-tracked`, `--grouped`/`--no-grouped`
  (grouped by default), and `--no-feedback`, mirroring `review next`.
- **`source-stats`** — unified per-source statistics in registry order, with
  `--source` to select one.
- **`collection-history`** — collection runs newest first, with `--source` and
  `--failed`.

All subcommands share `--format {json,jsonl,csv}` (default `json`), `--limit N`
(default 20; `0` means no cap, hard-capped at 1,000,000 records), `--output
PATH` (default stdout), `--overwrite` (required to replace an existing
`--output` file), and `--database`.

Format contract:

- **JSON** — a single document `{"records": [...], "total": N, "truncated":
  bool}`. Record keys are in a fixed canonical order; nested values (`reasons`,
  `score_components`, grouped `members`/`sources`/`urls`) are native
  structures.
- **JSONL** — one compact JSON record per line, in the same order.
- **CSV** — a header row of the canonical column names followed by one row per
  record; nested values render as stable JSON strings and empty cells are
  blank.

`total` always reports how many records matched before the cap, so truncation
is always explicit (`truncated: true` when some records were dropped).
Ranked and review exports carry `score`, `match_score`, `reasons`, and
`score_components` per record (matching the JSON output of the rank and review
commands), and flat job/ranked records include the canonical `group_id` when the
job belongs to a group. Exporting into a closed pipe (e.g. `| head`) exits 0.
Exit codes: `0` success, `1` output/profile/database failure, `2` usage errors.

## Scheduled collection

A periodic run can be installed from either a wheel or source environment.
Templates ship inside the `careeros` package, and generated services execute
the exact Python interpreter that owns the installed CareerOS artifact:

```bash
careeros systemd install --dry-run                 # preview (writes nothing)
careeros systemd install                           # install collection units
careeros systemd install --units notify --enable   # independent notify timer
careeros systemd install --units all --enable      # both timers
careeros systemd uninstall --units all             # remove exact owned units
```

The timer runs `careeros collect due` every 15 minutes (first check 5 minutes
after login) so CareerOS stays authoritative for per-source eligibility. The
installer and uninstaller never touch the database. If the timer must run
without a login session, enable lingering manually with
`loginctl enable-linger "$USER"`; it is never enabled automatically. Re-run the
installer with `--force` after deliberately moving/replacing the Python
environment, since units embed that interpreter path. Installation is always
user-level (`${XDG_CONFIG_HOME:-~/.config}/systemd/user`), never uses `sudo`,
and never writes system-wide unit directories. Differing existing units abort
unless `--force` is supplied; uninstall removes only exact generated content.

## Complete private-state backup and recovery

A recoverable CareerOS snapshot is a coordinated set, not only SQLite. It may
contain the database, the matching managed Materials root (including
`.careeros-store`), and the private candidate profile/config.

1. Stop every writer first: stop the CareerOS web process, collection and
   notification workers, and the two user timers if enabled. Do not copy a
   Materials tree while imports, version creation, or package finalization can
   run.
2. Create the SQLite backup with the supported online operation:
   `careeros database backup /backup/careeros.sqlite3 --database /active/careeros.sqlite3`.
   This backup contains SQLite only.
3. Copy the entire matching Materials root to the same backup set while it is
   quiescent, preserving private permissions and the `.careeros-store` marker.
   Also copy the private profile/config if it is needed to reproduce ranking.
4. Record the database path, Materials-root path/override, CareerOS schema
   version, and backup time together. The database's `material_store.store_id`
   and the root marker form one binding; mixing a database and root from
   different snapshots is rejected.
5. To restore, stop writers again and restore SQLite only with
   `careeros database restore BACKUP --database TARGET --confirm-replace`;
   `database restore` has no Materials-root option. Separately restore the
   matching Materials tree to the configured XDG location. Select that tree
   for subsequent operation with `CAREEROS_MATERIALS_ROOT`, or pass
   `--materials-root` only to a command that supports it, such as `material
   audit` or `serve`. Restore the profile separately to its XDG path or keep
   using its explicit path.
6. Before resuming writers, run
   `careeros material audit --database TARGET --materials-root RESTORED_ROOT`.
   Only a successful audit certifies the database/root pair.

Recovery intentionally loses database, Material, and profile changes newer
than their respective backup copies. A missing/mismatched marker, missing or
corrupt object, unsafe path/permissions, or inconsistent database metadata
fails closed; do not edit the marker or database by hand to bypass the audit.

## Today / Workflow and Attention Center

Today is the single action-oriented Workflow / Attention Center. Home remains
the separate profile-less operational and system overview, including collection
and source health. `GET /today?limit=10` captures one server-side UTC `as_of`
instant and one coherent SQLite read transaction. It returns these
server-classified sections in order: overdue reminders, overdue application
work, reminders due later today, application work due later today,
non-temporal application attention, shortlisted opportunities not yet started,
and Review next. Totals precede each section's independent limit (maximum 20;
Review next is a three-item score-qualified preview).

Review next reuses the stored explicit and learned Matching Preferences
authority. The **Use learned preferences** checkbox is enabled by default and
changes only that request; turning it off neither changes stored preferences nor
disables explicit preference matching. Today remains deterministic, local-first,
and model-free.

Completing a Reminder is the only inline Today write. Application,
Preparation, Package, and Review details and mutations stay in their
authoritative workspaces, reached through Today links. Loading Today never
creates a Reminder or notification, executes a saved search, or turns source
health into an attention category. Its public projection omits reminder
messages, workflow notes/events, requirement text, Package answers and Material
metadata, private profile paths, raw preference evidence, provider errors, and
private scorer diagnostics.

## Ranking explanations

Ranked HTTP results include a deterministic `explanation` projection derived
from the same `MatchResult` that produced `score` and `match_score`. It has
`scope`, `listing_count`, `summary`, `score`, and structured `matches`, `gaps`,
`unknowns`, and `other_factors`; factor points reconcile exactly to the raw
contribution total. It is embedded in ranked jobs, grouped opportunities,
review queues, and report ranking sections. Search-mode results remain
unranked. Explanations belong to the exact ranked response item: they are not
persisted, and the API has no generic job explanation endpoint.

Application event notes are private local workflow data. The
`application redact-note` command removes a note while preserving immutable
event facts; backups made before redaction may retain the original content.
