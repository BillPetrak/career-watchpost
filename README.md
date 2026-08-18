# CareerOS

CareerOS is a privacy-first career intelligence platform.

Its first working module will be an AI-assisted job discovery and
recommendation system that collects job advertisements, normalizes them,
stores them locally, ranks them, and produces explainable recommendations.

## Design principles

- Local-first storage
- Privacy by design
- Modular and replaceable components
- Explainable AI-assisted ranking
- Cost-aware model usage
- Automated testing
- Clear documentation

## Planned modules

- Job Hunter
- Candidate Profile
- Job Ranking
- Feedback and Preference Learning
- Skill Intelligence
- CV Analysis
- Career Analytics
- Reporting and Notifications

## Architecture

CareerOS owns its data, business logic, and provider interfaces.

External systems such as Hermes and LLM providers will be treated as
replaceable integrations rather than permanent dependencies.

## Current status

Milestone 1: repository and development foundation.

Current CLI capabilities include multi-source job collection (`collect
<source>`, `collect all`), rate-aware scheduled collection (`collect due`) with
persistent run history, read-only collection observability (`collect status`,
`collect history`), plus `search`, `show`, `rank`, `group`,
`saved-search`, `application`, `reminder` (including local `notify-due`/`
notify-history` notifications), `review`, `profile` (init/show/
validate for JSON or YAML candidate profiles), a read-only `report` (Career
Snapshot), and `diagnose` (including notification readiness).
Collection commands are mutually exclusive per database via an OS advisory
lock, while the read-only commands remain available during collection; a lock
conflict exits with code 75. See `AGENTS.md` for the full command reference.

A read-only web dashboard (Home, Jobs, a Review view with an inline
candidate-profile form, and a per-job detail view) is served by the optional
FastAPI slice at <http://127.0.0.1:8000/ui>:

```bash
uv sync --extra development --extra api   # install the API extra
uv run uvicorn careeros.api:app           # serve; then open /ui in a browser
```

It calls only the documented read endpoints (`GET /report`, `GET /jobs`,
`GET /jobs/{id}`, `GET /jobs/{id}/group`,
`GET /applications?job_id={id}`, `GET /review-decisions?job_id={id}`,
`GET /reminders?job_id={id}`) plus the
computation-only `POST /report` (the Review view's inline candidate-profile
form) and the Job Detail workflow mutations (review-decision
`PUT`/`DELETE /review-decisions/{id}`, application-status
`PUT /applications/{id}`, and reminder `POST /reminders`,
`PUT /reminders/{id}/complete` and `PUT /reminders/{id}/reopen`, plus a
confirmed `DELETE /reminders/{id}` that always shows a native confirmation
dialog before removing a reminder). The Job Detail application/review reads use
the job-scoped list endpoints so the normal absent state ("Not tracked." /
"Not reviewed.") is an empty 200 list rather than a canonical 404; the
singular `GET /applications/{id}` and `GET /review-decisions/{id}` endpoints
keep their authoritative 404 absent-state behavior for API clients. These
mutations are local workflow state only and never touch Job rows,
saved-searches, or the database itself.

Reminder notifications are the third, **independent** piece: a background
systemd user timer (`careeros-reminder-notify.timer`) runs
`careeros reminder notify-due --backend auto` and surfaces due reminders as
local desktop pop-ups (via `notify-send`) even when no browser tab and no
server is open. Installing it is opt-in and separate from the web UI/server:

```bash
uv run python systemd/install.py --units notify --enable   # install + enable the notify timer
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

Remotive's terms require attributing listings with a link back to the original
URL and allow polling at most four times a day; its listings are published with
a 24-hour delay. Jobicy requires similar credit. CareerOS always stores each
job's original URL and labels its source, and `collect due` enforces the
polling floors above.

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
"Current status" above for installation. `careeros diagnose` verifies
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
- `remote_preference` — `"remote"`, `"onsite_or_unspecified"`, or `null`
- `excluded_terms` — soft-excluded terms (small penalty)
- `hard_excluded_terms` — hard exclusions that force a score of 0
- `preferred_employment_types` — e.g. `"Full-time"`
- `preferred_seniority` — e.g. `"Senior"`

### Private profile

The default profile path is `config/candidate_profile.yaml`. This file is
**private**: it is listed in `.gitignore` and must never be committed. It holds
your real job-search preferences, unlike the committed fictional examples
`config/candidate_profile.example.json` and
`config/candidate_profile.example.yaml`.

Initialize your private profile safely (refuses to overwrite an existing file):

```bash
uv run careeros profile init                    # create config/candidate_profile.yaml
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

A periodic run can be installed as a systemd user service and timer from the
repository-owned templates in `systemd/`:

```bash
uv run python systemd/install.py --dry-run   # preview (writes nothing)
uv run python systemd/install.py             # install units (not enabled)
uv run python systemd/install.py --enable    # install, enable, and start
uv run python systemd/uninstall.py           # remove the units
```

The timer runs `careeros collect due` every 15 minutes (first check 5 minutes
after login) so CareerOS stays authoritative for per-source eligibility. The
installer and uninstaller never touch the database. If the timer must run
without a login session, enable lingering manually with
`loginctl enable-linger "$USER"`; it is never enabled automatically. Re-run the
installer after moving the repository, since the units embed the absolute
project path.
