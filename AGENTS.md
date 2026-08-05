# CareerOS — Agent instructions

## Quick start

```bash
uv sync --extra development   # install with dev deps (pytest, ruff, mypy)
uv run careeros diagnose      # verify environment
```

## Developer commands

| Command | Purpose |
|---|---|
| `uv run pytest` | Run all tests |
| `uv run pytest tests/test_foo.py::test_bar` | Single test |
| `uv run pytest -k "keyword"` | Test match |
| `uv run ruff check src tests` | Lint |
| `uv run ruff format --check src tests` | Format check |
| `uv run ruff format src tests` | Auto-format |
| `uv run mypy` | Typecheck (strict) |
| `uv run careeros collect arbeitnow --limit 10` | Collect jobs |
| `uv run careeros collect arbeitnow --database /tmp/test.db` | Override DB path |
| `uv run careeros collect remotive --limit 10` | Collect jobs from Remotive |
| `uv run careeros collect workingnomads --limit 10` | Collect jobs from Working Nomads |
| `uv run careeros collect all --limit 25` | Collect from all sources sequentially |
| `uv run careeros collect all --limit 25 --database /tmp/careeros_all.db` | Collect all sources into a custom DB |
| `uv run careeros collect due --limit 25` | Collect only sources whose polling interval has expired |
| `uv run careeros collect status [--database PATH]` | Show per-source polling status |
| `uv run careeros collect history [--source NAME] [--limit N] [--failed] [--database PATH]` | Show recent collection run history |
| `uv run careeros search engineer --limit 10` | Search stored jobs |
| `uv run careeros search engineer --remote --location Berlin` | Search with filters |
| `uv run careeros search engineer --grouped` | Search, collapsing cross-source duplicates into one result per group |
| `uv run careeros rank engineer --profile config/candidate_profile.example.json` | Rank stored jobs against profile |
| `uv run careeros rank engineer --profile config/candidate_profile.example.json --grouped` | Rank jobs, one scored result per duplicate group |
| `uv run careeros profile init [PATH] [--force]` | Create a starter candidate profile (default: private `config/candidate_profile.yaml`; refuses to overwrite without `--force`) |
| `uv run careeros profile validate [PATH]` | Validate a JSON/YAML candidate profile (default: private path) |
| `uv run careeros profile show [PATH]` | Show a candidate profile readably (default: private path) |
| `uv run careeros report [--format {text,json}] [--profile PATH] [--saved-search NAME] [--limit N] [--grouped]` | Read-only Career Snapshot dashboard (default format: text) |
| `uv run careeros group regroup [--database PATH]` | Recompute canonical duplicate groups (write; locks the DB) |
| `uv run careeros group explain [--database PATH] [--group-id ID] [--limit N]` | Show canonical groups read-only |
| `uv run careeros application set JOB_ID STATUS [--note TEXT] [--database PATH]` | Set/update a job's application status (write; locks the DB) |
| `uv run careeros application show JOB_ID [--database PATH]` | Show current application state for one job |
| `uv run careeros application list [--status STATUS ...] [--limit N] [--database PATH]` | List tracked applications |
| `uv run careeros application history JOB_ID [--limit N] [--database PATH]` | Show the event history for one tracked job |
| `uv run careeros search engineer --untracked` | Search, showing only jobs with no application record |
| `uv run careeros search engineer --exclude-status applied --exclude-status rejected` | Search, hiding selected application statuses |
| `uv run careeros search engineer --status interview` | Search, showing only jobs in the selected status |
| `uv run careeros rank engineer --profile config/candidate_profile.example.json --untracked` | Rank only untracked jobs |
| `uv run careeros reminder add JOB_ID KIND DUE_AT [--message TEXT] [--database PATH]` | Add a local follow-up reminder for a stored job (write; locks the DB) |
| `uv run careeros reminder list [--open\|--overdue\|--due\|--upcoming\|--completed] [--job ID] [--kind KIND] [--after TS] [--before TS] [--limit N] [--database PATH]` | List reminders, soonest first (read-only) |
| `uv run careeros reminder show REMINDER_ID [--database PATH]` | Show one reminder in detail (read-only) |
| `uv run careeros reminder update REMINDER_ID [--kind KIND] [--due-at TS] [--message TEXT\|--clear-message] [--database PATH]` | Change a reminder (write; locks the DB) |
| `uv run careeros reminder complete REMINDER_ID [--database PATH]` | Mark an open reminder completed (write; locks the DB) |
| `uv run careeros reminder reopen REMINDER_ID [--database PATH]` | Reopen a completed reminder (write; locks the DB) |
| `uv run careeros reminder delete REMINDER_ID [--database PATH]` | Delete a reminder (write; locks the DB) |
| `uv run careeros saved-search create NAME QUERY --mode {search,rank} [--profile PATH] [FILTERS...]` | Create a reusable search/rank workflow (write; locks the DB) |
| `uv run careeros saved-search list [--database PATH]` | List saved workflows, ordered case-insensitively by name |
| `uv run careeros saved-search show NAME [--database PATH]` | Show a saved workflow definition |
| `uv run careeros saved-search run NAME [--database PATH]` | Execute a saved workflow against the current DB (read-only) |
| `uv run careeros saved-search update NAME [--query TEXT] [--grouped/--no-grouped] [--untracked/--no-untracked] [--status S] [--exclude-status S] [--source S] [--location S] [--remote] [--min-score N] [--limit N] [--profile PATH] [--clear-*]` | Change a saved workflow (write; locks the DB) |
| `uv run careeros saved-search rename NAME NEW_NAME [--database PATH]` | Rename a saved workflow (write; locks the DB) |
| `uv run careeros saved-search delete NAME [--database PATH]` | Delete a saved workflow (write; locks the DB) |
| `uv run python systemd/install.py --dry-run` | Preview systemd user-unit install (writes nothing) |
| `uv run python systemd/install.py` | Install the systemd user service+timer (not enabled) |
| `uv run python systemd/install.py --enable` | Install, then enable and start the timer |
| `uv run python systemd/uninstall.py --dry-run` | Preview uninstall (writes nothing) |
| `uv run python systemd/uninstall.py` | Remove the installed systemd user units |
| `loginctl enable-linger "$USER"` | Manual only: run the timer without a login session |

Run order: **lint → mypy → test** (all independent; can run in parallel).

`collect all` runs each registered source sequentially, once per invocation, in
registry order, and applies `--limit` per source. Respect provider polling
limits — do not run repeated all-source collections more often than the most
restrictive source permits (Jobicy: at most once per hour).

`collect due` collects only sources whose last completed run (success or
failure) is older than their polling interval. A source with no completed run
is due immediately, and a failed run delays the next automatic attempt by
exactly one interval. Polling intervals live in
`src/careeros/collection_policies.py` (jobicy: 60 minutes, documented;
remotive: 6 hours, documented per Remotive's "no more than four times a day"
guidance; arbeitnow/remoteok/weworkremotely/workingnomads: provisional
defaults). The workingnomads collector targets an undocumented internal
Elasticsearch search endpoint (`POST https://www.workingnomads.com/jobsapi/_search`)
that powers the public listing pages; it may change without notice, and its
6-hour polling floor is a conservative provisional default because Working
Nomads publishes no rate-limit guidance. Manual
`collect <source>` and `collect all` bypass scheduling, but every collection
attempt is recorded in the `collection_runs` table. `collect due` exits 0 when
nothing is due or all due sources succeed, 1 when any due source fails or setup
fails, and 75 when another collection already holds the database lock.

`collect status` and `collect history` are read-only observability commands:
they never fetch jobs, never write to the database, and never alter scheduling
state. `collect status` shows, per registered source in registry order: the
polling interval (documented or provisional), the last completed attempt and
its outcome, fetched/inserted/updated counts, the last error when present, the
next due timestamp, and the current due state. It captures one timezone-aware
UTC `now` per invocation and uses the same scheduling rules as `collect due`
(a source with no completed attempt is due, and a malformed stored timestamp
is treated as due). `collect history` lists collection runs newest first
(default `--limit 20`), including incomplete/crashed attempts labelled
`incomplete`; `--failed` restricts the list to completed failed attempts, and
`--source` filters to one registered source. Both exit 0 on success and 1 on
database/setup failure.

## Cross-source duplicate grouping

CareerOS detects when two vacancies from different sources are the same
underlying job. Detection is deterministic, conservative, and stdlib-only:

- **Never mutates jobs** — the `jobs` table is the source of truth; grouping
  lives only in the derived `job_groups`/`job_group_members` tables.
- **Equality-only matching** — jobs group only when their normalized company
  and title signatures are equal. Location and remote status are vetoes, never
  similarity signals: a remote-class conflict (remote vs onsite/unspecified) or
  a distinct, incompatible non-generic location splits a group. Location
  "compatible" means one signature is a token-prefix of the other (so "Berlin"
  and "Berlin, Germany" group, "Berlin" and "Munich" do not).
- **Content-addressed group IDs** — each group ID is a truncated SHA-256 of the
  group's normalized identity, so adding/removing unrelated jobs never
  renumbers existing groups; only a group whose own identity changes gets a new
  ID (which happens exactly when matching rules evolve).
- **Data-driven suffix stripping** — company legal-entity suffixes
  (`GmbH`, `BV`, `PLC`, `Oy`, `AB`, `SA`, `SAS`, `AG`, `Inc`, `LLC`, `Ltd`,
  `S.A. de C.V.`, ...) are stripped from the trailing edge of a company name.
  Add a locale's suffix to `COMPANY_LEGAL_SUFFIXES` in
  `src/careeros/grouping/normalize.py` and it is picked up with no code change.
- **Generic locations** ("Remote", "Anywhere") carry no signal; they merge with
  the sole located cluster, or form their own singleton group when several
  located clusters exist (ambiguous). Unidentifiable jobs (empty company or
  title after normalization) never group.

`group regroup` recomputes and rewrites the grouping tables and is **not**
read-only: it acquires the collection lock and exits 75 on contention (same
rules as the `collect` family). `group explain` is read-only, takes no lock,
and shows each canonical group with its members, reason, and sources.
`search --grouped` and `rank --grouped` collapse duplicates into one result per
group; filters (`--source`, `--remote`, `--location`) apply to candidate member
rows *before* grouping, and the displayed representative is the lowest-ID
matching member. `collect <source>`, `collect all`, and `collect due`
automatically regroup after each successful collection. Grouping is
non-destructive and deterministic: `group regroup` is safe to run at any time
and `group explain` never writes.

## Collection locking

`collect <source>`, `collect all`, and `collect due` are mutually exclusive per
database. Each holds one OS-level advisory lock (an exclusive `flock`) on a
companion file next to the resolved absolute database path for the whole
command:

    /path/to/careeros.sqlite3.lock

The lock is acquired before any network collection begins and retained through
schema setup, fetching, persistence, and run-history recording. Two different
databases never block one another, and relative and equivalent absolute paths
of the same database map to the same lock. Ownership is managed by the
operating system, so a stale companion file left behind after a crash never
blocks a later run — do not delete lock files as a remedy. Lock-file contents
are informational metadata only (pid, command, acquisition time, database
path); the OS lock, not file contents, determines ownership.

On contention a collection command prints a message to stderr, does not fetch
any source, does not touch run history or the jobs table, and exits with code
75 (`EX_TEMPFAIL`, a conventional temporary-failure code). Example:

    uv run careeros collect due --database /tmp/careeros_lock_test.db
    echo $?

The read-only commands `collect status`, `collect history`, `search`, `show`,
`rank`, `group explain`, `application show`, `application list`,
`application history`, `reminder list`, `reminder show`, `saved-search list`,
`saved-search show`, `saved-search run`, and `report` never acquire the
collection lock and remain available
while a collection is running.

## Application tracking

CareerOS records what you have done with a stored vacancy so handled jobs can
be recognized later. Tracking is **local workflow state only**: CareerOS never
submits applications, never sends email, and never contacts any external
service on your behalf. One current application record is stored per tracked
job, keyed by the stable `jobs.id`, plus an append-only history of every status
transition.

Supported statuses (validated centrally in
`src/careeros/applications/models.py` and exposed as argparse choices):

- `interested`
- `applied`
- `interview`
- `offer`
- `rejected`
- `withdrawn`

`application set JOB_ID STATUS [--note TEXT]` rules:

- First assignment creates the application and one explicit event whose old
  status is empty.
- A real transition updates the current status and appends an event.
- Repeating the identical status with **no** note is an idempotent no-op.
- Repeating the identical status **with** a note appends a same-status event,
  supporting "note without a status transition".

Every write is transactional: the application-row change and its history event
commit together, and any failure rolls the whole operation back. Timestamps are
timezone-aware UTC ISO-8601. `application show` reports the job details, the
current status, first-tracked/last-updated times, and the latest note if any.
`application history` lists events newest first (event ID, timestamp, old ->
new status, note). `application list [--status ...]` lists tracked applications
newest first, optionally filtered by status.

`application set` is a write command: it acquires the per-database collection
lock and exits 75 on contention. The read-only `application show`/`list`/
`history` never lock and never change database contents beyond idempotent
schema initialization.

`search` and `rank` accept the tracking flags `--untracked`, `--status STATUS`
(repeatable), and `--exclude-status STATUS` (repeatable). `--untracked` is
mutually exclusive with the other two, and `--status` is mutually exclusive
with `--exclude-status` (rejected with exit code 2). With no tracking flags the
default output is unchanged. When a tracking flag is active, results carry an
`Application:` line showing the actual state (e.g. `Application: Applied`,
`Application: Untracked`).

For grouped results (`--grouped`), a canonical group is treated as tracked when
**any** current member has an application record; filtering and the
`Application:` line then describe that group status. If the tracked member is
not the displayed representative, the annotation says so, e.g.
`Application: Applied (via job 3, arbeitnow)`. The durable record always stays
on the single concrete `jobs.id` that was tracked; application rows are never
duplicated across group members and source job rows are never mutated.

Application records are anchored to `jobs.id`, **never** to the derived
grouping tables, so `group regroup` (which deletes and rebuilds
`job_groups`/`job_group_members`) can never delete or orphan them. Both
`applications.job_id` and `application_events.application_id` use
`ON DELETE RESTRICT`: application history is durable audit data and cannot be
silently cascade-deleted.

## Reminders

`reminder` manages local follow-up reminders attached to stored jobs so a
planned next step is not lost. Reminders are **local workflow state only**:
CareerOS records and reports them locally, and it does not send messages,
contact employers, submit applications, or infer deadlines. Reminder rows live
in the `reminders` table (schema v6) and are anchored to the stable `jobs.id`
— **never** to the derived grouping tables — so `group regroup` (which deletes
and rebuilds `job_groups`/`job_group_members`) can never delete or orphan them
and regrouping is guaranteed to leave every reminder row byte-identical.

Supported kinds (validated centrally in `src/careeros/reminders/models.py` and
exposed as argparse choices):

- `follow_up`
- `interview`
- `offer`
- `review`
- `custom`

`reminder add JOB_ID KIND DUE_AT [--message TEXT]` rules:

- The job must already exist (unknown job exits with code 1).
- A full ISO-8601 timestamp with an explicit UTC offset is required (`Z`
  accepted). Naive timestamps and date-only values are rejected with exit
  code 2 — CareerOS never guesses a timezone. Values are normalized and stored
  as UTC (`+00:00`).
- Messages must be non-empty (whitespace-only is rejected with exit code 2).

`reminder list` shows reminders joined with their jobs, soonest first, in a
deterministic order (`due_at ASC`, then `id ASC`). With no filter it shows open
reminders (`--limit` defaults to 20). The mutually exclusive state flags are
`--open`, `--overdue` (due before now), `--due` (due now or earlier), and
`--upcoming` (due after now), plus `--completed`. Additional filters: `--job`,
`--kind`, `--after`, `--before` (all ISO-8601 with explicit offsets).

`reminder complete` and `reminder reopen` are idempotent: completing an already
completed reminder (and reopening an already open one) is a no-op that
preserves due time, kind, and message. `reminder update` only changes explicitly
provided fields (`--kind`, `--due-at`, `--message`, or the mutually exclusive
`--clear-message`), a no-op update leaves `updated_at` untouched, and omitted
fields retain prior values. `reminder show` displays one reminder with its job
details.

Every write (`add`, `update`, `complete`, `reopen`, `delete`) is transactional
and commits in one transaction, rolling back completely on failure. Write
commands acquire the per-database collection lock and exit 75 on contention
with no database change; the read-only `list` and `show` never lock and never
change database contents beyond idempotent schema initialization. `reminders`
uses `ON DELETE RESTRICT` on `jobs.id`, matching the application-tracking
tables. Exit codes: `0` success, `1` unknown job/reminder or database failure,
`2` validation/usage error, `75` lock contention.

`application show` appends a `Reminders:` block listing the job's reminders
after its application note, so a job's follow-up context is visible in one
place. Application status changes never create, complete, reschedule, or delete
reminders automatically, and grouped `search`/`rank` results are not annotated
with reminders.

## Candidate profile

`profile` manages the candidate profile file used by `rank` and `report`.
Profiles are plain JSON (`.json`) or YAML (`.yaml`/`.yml`) files whose format
is chosen by file extension (`src/careeros/ranking/profile.py`); unknown
top-level fields are silently ignored. All fields are optional:
`desired_titles`, `skills`, `required_skills`, `preferred_locations`,
`remote_preference` (string or null), `excluded_terms`, `hard_excluded_terms`,
`preferred_employment_types`, `preferred_seniority`.

```bash
uv run careeros profile init                       # create private config/candidate_profile.yaml
uv run careeros profile init --force               # overwrite an existing profile
uv run careeros profile init /tmp/me.json          # JSON starter at an explicit path
uv run careeros profile validate                   # validate the private profile
uv run careeros profile validate config/candidate_profile.example.yaml
uv run careeros profile show config/candidate_profile.example.json
```

The default path is the **private** `config/candidate_profile.yaml`, which is
gitignored and must never be committed; `init` refuses to overwrite an existing
file unless `--force` is passed. The committed fictional examples
`config/candidate_profile.example.json` and
`config/candidate_profile.example.yaml` are safe to use in docs, tests, and
smoke runs — never the private path. `init` writes a commented starter whose
format follows the target file extension (unsupported extensions exit 2).
`show` and `validate` default to the private path when none is given. All three
are file-only commands: they never touch the database and never acquire the
collection lock. Exit codes: `0` success, `1` missing/invalid profile or an
existing `init` target, `2` usage errors (unsupported extension).

## Saved searches

`saved-search` stores a reusable search or rank workflow definition in the
`saved_searches` table (schema v5). A saved search captures **intent — query
and filters — never cached results**: `saved-search run NAME` always executes
the same shared pipeline as the explicit `search`/`rank` commands
(`src/careeros/saved_searches/execution.py`), so its output is byte-identical
and it always reflects the current database. Running a saved search is
read-only: it never fetches jobs, never writes, and never contacts any external
service.

`--mode {search,rank}` is **required** at creation and **immutable** afterwards
(passing `--mode` to `update` is rejected with exit code 2). Mode-specific
rules: `--profile` is invalid for `search` and **required** for `rank`;
`--min-score` is rank-only. All other filter flags mirror the explicit
commands: `--grouped/--no-grouped`, `--untracked/--no-untracked` (tri-state),
`--status`, `--exclude-status`, `--source`, `--remote`, `--location`, `--limit`,
`--profile`. Validation is enforced centrally in
`src/careeros/saved_searches/models.py:validate_fields` and contradicting flag
combinations are rejected at the argparse layer with exit code 2.

Names are case-insensitive for uniqueness and lookup (`UNIQUE COLLATE NOCASE`)
but stored as given; `list` orders case-insensitively by name then id. A
case-only rename is a no-op. Duplicate names and unknown names exit with code 1.

`update` semantics: omitted flags retain prior values; `--grouped/--no-grouped`
and `--untracked/--no-untracked` set or clear explicitly; `--clear-source`,
`--clear-location`, `--clear-remote`, `--clear-min-score`, `--clear-profile`,
`--clear-status-filters` unset stored filters. A `--clear-*` flag combined with
its own set flag is an argparse error, and clearing the profile of a rank
workflow is a validation error (rank always requires a profile). An update that
changes nothing succeeds without touching `updated_at`. Every write commits in
one transaction and rolls back on failure.

Writes (`create`, `update`, `rename`, `delete`) acquire the per-database
collection lock and exit 75 on contention with no database change; the reads
(`list`, `show`, `run`) never lock. Timestamps are timezone-aware UTC ISO-8601.
The nullable `description` column is stored for future extensibility but has no
CLI flag yet.

## Career Snapshot

`report` produces a read-only **Career Snapshot** dashboard over stored jobs,
application tracking, reminders, saved searches, and collection history. It is
a dashboard, not an agent: it never submits applications, never sends email,
never fetches jobs, and never contacts any external service. It never acquires
the collection
lock (so it runs happily while a collection is in progress) and never writes —
not even a `collection_runs` entry. It is built entirely by
`src/careeros/reporting/` (immutable `models.py`, read-only `service.py`
`build_report`, and the `render.py` `render_report`/`render_report_json`
renderers, which share the same report model).

```bash
uv run careeros report                                                     # text
uv run careeros report --format json                                       # same data as JSON
uv run careeros report --profile config/candidate_profile.example.json     # rank untracked jobs
uv run careeros report --saved-search top-roles                            # rank workflow drives scoring
uv run careeros report --grouped                                           # collapse duplicates
uv run careeros report --limit 5                                           # cap action items + recommendations
```

`--format {text,json}` selects the renderer (default `text`); both render the
same immutable `Report`, so the JSON carries exactly the data the text shows.
Report defaults: no profile/no saved search → recommendations are skipped with
an explicit note; `--limit 10` caps the "recently changed" list, each action
category, AND the recommendations.

Sections and semantics:

- **Overview** — stored jobs, canonical groups, grouped members, distinct
  opportunities (`total_jobs - grouped_members + groups`), tracked applications,
  saved searches, a per-source last-run summary, and due sources.
- **Application pipeline** — one count per status in `APPLICATION_STATUSES`
  order (zero-filled), and the most recently changed applications newest first
  (`updated_at DESC, id DESC`), capped by `--limit`. Each entry shows the latest
  non-empty event note.
- **Action items** — active pipeline categories in prominence order: interview
  attention, offer attention, interested (not applied), applied (awaiting
  progress). `rejected`/`withdrawn` appear **only** in the pipeline counts,
  never as action items. CareerOS never invents deadlines or follow-up dates
  from text.
- **Reminders** — overdue count, "due today (UTC)" count, open total, and the
  open reminders due at or after the report's `now`, ordered by urgency
  (`due_at ASC`, then `id ASC`) and capped by `--limit`. "Due today (UTC)"
  means within the current UTC calendar day: reminder times are stored and
  compared in UTC and CareerOS does not assume your local timezone. All
  reminder data comes from the shared reminder service; the report never
  duplicates its logic.
- **Recommended opportunities** — profile-ranked jobs from the shared ranking
  engine. Tracked jobs are excluded (`untracked=True`), so the section only
  proposes new opportunities. `--grouped` collapses cross-source duplicates
  (untracked status then described per canonical group).
- **Collection health** — per-source last run, outcome, fetched/inserted/
  updated, last error, next due, and incomplete-run count; a malformed stored
  timestamp is treated as "never run / due" and never crashes.

`--saved-search NAME` requires a **rank-mode** saved search (search-mode is
rejected); it governs the query, filters, minimum score, and result limit
(`saved_search.result_limit` applies first, report `--limit` only trims).
`--profile` may be passed alongside to override the saved search's stored
profile path. `--grouped` cannot be combined with `--saved-search`.

Exit codes: `0` on success (including an empty database), `1` for an unknown
saved-search name or profile/database/setup failure, `2` for invalid flags or a
search-mode `--saved-search`. The command never exits 75 and never touches the
database beyond idempotent schema initialization.

## systemd user units

The `systemd/` directory holds templates, an installer, and an uninstaller for
a periodic user-manager collection. One timer drives the `careeros-collect`
oneshot service, which runs `careeros collect due` so CareerOS stays
authoritative for source eligibility. Defaults: `OnStartupSec=5min`,
`OnUnitActiveSec=15min`, `RandomizedDelaySec=3min`,
`SuccessExitStatus=75` (exit 75 is the expected temporary lock-contention
result and is treated as success; genuine failures still mark the service
failed).

The installer renders the templates with the absolute `uv` binary path and
absolute project root, writes them to `~/.config/systemd/user`, and runs
`systemctl --user daemon-reload`. It never touches the database. Existing unit
files that differ from the generated content abort the install unless
`--force` is passed. `--enable` enables and starts the timer; without it the
units are installed but not started. `--dry-run` previews everything without
writing, and `--no-systemctl` writes the units while only printing the
systemctl commands (used by tests). The uninstaller removes only files whose
content matches the rendered templates and never touches the database.

Lingering is intentionally never enabled automatically; run
`loginctl enable-linger "$USER"` manually if the timer must run without a login
session. The units embed the absolute project path, so re-run the installer
after moving the repository (uninstall first if the old units should go).

## Project structure

- `src/careeros/` — package root, entrypoint `careeros.cli:main`
- `src/careeros/applications/` — application-tracking workflow (models, service)
- `src/careeros/collectors/` — job-board API clients
- `src/careeros/database/` — SQLite layer (schema, connection, persistence)
- `src/careeros/grouping/` — duplicate detection (normalization, matcher)
- `src/careeros/saved_searches/` — saved workflow definitions and shared execution
- `src/careeros/reminders/` — local follow-up reminders (models, service)
- `src/careeros/reporting/` — Career Snapshot dashboard (models, service, renderers)
- `src/careeros/{job_hunter,privacy,ranking}/` — stubs (empty, not implemented)
- `tests/` — pytest suite, uses `tmp_path` for isolated SQLite databases
- `config/` — YAML config (settings.example.yaml, candidate_profile.example.yaml)
- `data/database/careeros.sqlite3` — default SQLite DB path
- `.env` — not loaded by app yet; reserved for future provider keys

## Toolchain quirks

- **No external runtime dependencies** — only stdlib + dev tools
- **SQLite only** — schema managed in `schema.py`, not via migration tooling
- **No CI/CD**, no pre-commit hooks, no Makefile
- Module sub-packages beyond `collectors`, `database`, `grouping`, `saved_searches`, `reminders`, and `reporting` are stubs; adding real code there is expected
- Ruff: `line-length = 88`, selected rules `E F I UP B`
- `mypy` is strict, scoped to `src/careeros` only (not tests)

## Testing quirks

- All DB tests use `tmp_path` for disposable SQLite files (no fixtures, no external DB)
- Collector unit tests pass without network (test `normalize_job()` only, not `fetch_jobs()`)
- Grouping tests use the pure matcher (`assign_groups`) and a seeded `tmp_path` DB (no network)
- No integration/e2e tests yet
