# CareerOS — Agent instructions

Product 1 v1.0 is RELEASED. Product 1 v1.05 adds local résumé/CV extraction,
sanitization, deterministic Career Facts, human review, and the immutable
Career Profile foundation/workspace. Its package version is `1.0.5` and its
eventual release tag is `v1.0.5`. The existing seven job sources remain the
frozen Product 1 set. Product 1 v1.1 then adds job-source expansion.

R3, R4, and R5 remain closed absent a concrete regression. There is no R6. MCP
and Superpowers remain deferred, no new sources belong in v1.0.

Schema-12 state upgrades only through the explicit supported schema-13
migration after a backup; there is no down migration. SQLite backup does not
contain managed Material bytes, so complete private recovery requires the
database, matching Materials root, and applicable private configuration/profile
state. See `docs/v1.05-release-qualification.md` for the release boundary and
qualification record.

Stable private defaults are XDG-based and independent of the checkout or
installed environment: database
`${XDG_DATA_HOME:-~/.local/share}/careeros/careeros.sqlite3`, Materials
`${XDG_DATA_HOME:-~/.local/share}/careeros/materials`, and private profile
`${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml`. Explicit CLI
paths win, then `CAREEROS_DATABASE` / `CAREEROS_MATERIALS_ROOT`, then XDG
defaults. Read-only operations do not create absent persistent state.

## Quick start

```bash
uv sync --extra development   # install with dev deps (pytest, ruff, mypy)
uv sync --extra development --extra api   # also install FastAPI/Uvicorn for the API slice
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
| `uv run uvicorn careeros.api:app` | Serve the local FastAPI application |
| `uv run careeros serve --database PATH` | Serve the protected local web application against an explicit database |
| `uv run careeros database backup DESTINATION --database PATH` | Create a supported SQLite-only backup; Materials/profile remain separate |
| `uv run careeros database migrate --database PATH` | Explicitly migrate a supported old schema; default backup suffix is `.pre-v13.backup.sqlite3` |
| `uv run careeros database restore BACKUP --database PATH --confirm-replace` | Replace a SQLite database from a validated backup (managed Material files are separate) |
| `uv run careeros collect arbeitnow --limit 10` | Collect jobs |
| `uv run careeros collect arbeitnow --database /tmp/test.db` | Override DB path |
| `uv run careeros collect remotive --limit 10` | Collect jobs from Remotive |
| `uv run careeros collect workingnomads --limit 10` | Collect jobs from Working Nomads |
| `uv run careeros collect himalayas --limit 10` | Collect jobs from Himalayas |
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
| `uv run careeros rank engineer --profile config/candidate_profile.example.json --format json` | Rank results as JSON with a `score_components` breakdown per result |
| `uv run careeros rank engineer --profile config/candidate_profile.example.json --no-feedback` | Rank without review-feedback adjustments |
| `uv run careeros rank engineer --profile config/candidate_profile.example.json --no-adaptive` | Rank without persistent learned-preference adjustments |
| `uv run careeros profile init [PATH] [--force]` | Create a starter candidate profile (default: private XDG config path; refuses to overwrite without `--force`) |
| `uv run careeros profile validate [PATH]` | Validate a JSON/YAML candidate profile (default: private path) |
| `uv run careeros profile show [PATH]` | Show a candidate profile readably (default: private path) |
| `uv run careeros career-profile sources [--format {text,json}] [--database PATH]` | Inspect résumé Material/version/extraction/fact-run lineage for the immutable Career Profile workspace |
| `uv run careeros career-profile analyze MATERIAL_ID VERSION_ID [--materials-root PATH] [--database PATH]` | Explicitly run closed M3/M5 authority for one exact résumé version (write; locks the DB) |
| `uv run careeros career-profile candidates FACT_RUN_ID [--state STATE] [--format {text,json}] [--database PATH]` | Inspect deterministic Career Fact candidates from one recognized run |
| `uv run careeros career-profile candidate CANDIDATE_ID [--evidence-offset N] [--history-offset N] [--format {text,json}] [--database PATH]` | Inspect one candidate with bounded exact evidence and note-free review history |
| `uv run careeros career-profile review CANDIDATE_ID {approved,approved_edit,rejected} [--value-file PATH] [--database PATH]` | Deliberately review or re-review one candidate (write; locks the DB) |
| `uv run careeros career-profile show [--version VERSION_ID] [--format {text,json}] [--database PATH]` | Inspect the current or one immutable historical Career Profile version |
| `uv run careeros career-profile history [--offset N] [--limit N] [--format {text,json}] [--database PATH]` | Inspect immutable Career Profile version history |
| `uv run careeros career-profile save MANIFEST --expected-revision N [--database PATH]` | Save one deliberate complete profile snapshot through M6 CAS (write; locks the DB) |
| `uv run careeros preference show [--format {text,json}] [--database PATH]` | Inspect stored explicit and learned matching preferences |
| `uv run careeros preference explicit replace PROFILE [--database PATH]` | Replace the complete stored explicit profile, including configured-empty state |
| `uv run careeros preference explicit clear [--database PATH]` | Remove stored explicit authority without changing learned state |
| `uv run careeros preference suppress KEY [--database PATH]` | Suppress one learned preference while retaining evidence |
| `uv run careeros preference restore KEY [--database PATH]` | Restore one learned preference |
| `uv run careeros preference reset-learned --confirm [--database PATH]` | Delete learned evidence/suppressions while preserving explicit preferences and review history |
| `uv run careeros report [--format {text,json}] [--profile PATH] [--saved-search NAME] [--limit N] [--grouped] [--no-feedback] [--no-adaptive]` | Read-only Career Snapshot dashboard (default format: text) |
| `uv run careeros export jobs [--query Q] [--grouped] [--remote] [--location L] [--source S] [--employment-type ET] [--clean-descriptions] [--format {json,jsonl,csv}] [--limit N] [--output PATH] [--overwrite] [--database PATH]` | Deterministic export of stored jobs (read-only) |
| `uv run careeros export ranked --query Q --profile PATH [--grouped] [--min-score N] [--no-feedback] [--no-adaptive] [--remote] [--location L] [--source S] [--employment-type ET] [--clean-descriptions] [--format {json,jsonl,csv}] [--limit N] [--output PATH] [--overwrite] [--database PATH]` | Deterministic export of profile-ranked jobs (read-only) |
| `uv run careeros export review --profile PATH [--state S] [--min-score N] [--include-tracked] [--grouped\|--no-grouped] [--no-feedback] [--no-adaptive] [--remote] [--location L] [--source S] [--employment-type ET] [--clean-descriptions] [--format {json,jsonl,csv}] [--limit N] [--output PATH] [--overwrite] [--database PATH]` | Deterministic export of the review queue (read-only) |
| `uv run careeros export source-stats [--source S] [--format {json,jsonl,csv}] [--limit N] [--output PATH] [--overwrite] [--database PATH]` | Deterministic export of unified per-source statistics (read-only) |
| `uv run careeros export collection-history [--source S] [--failed] [--format {json,jsonl,csv}] [--limit N] [--output PATH] [--overwrite] [--database PATH]` | Deterministic export of collection run history (read-only) |
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
| `uv run careeros reminder notify-due [--backend auto\|desktop\|stdout] [--dry-run] [--limit N] [--database PATH]` | Deliver local desktop (or stdout) notifications for due reminders (write; takes the notification lock) |
| `uv run careeros reminder notify-history [--reminder ID] [--failed] [--limit N] [--database PATH]` | Show notification delivery history (read-only) |
| `uv run careeros diagnose` | Verify environment; includes notification readiness (notify-send, auto backend, notify timer state) |
| `uv run careeros saved-search create NAME QUERY --mode {search,rank} [--profile PATH] [FILTERS...]` | Create a reusable search/rank workflow (write; locks the DB) |
| `uv run careeros saved-search list [--database PATH]` | List saved workflows, ordered case-insensitively by name |
| `uv run careeros saved-search show NAME [--database PATH]` | Show a saved workflow definition |
| `uv run careeros saved-search run NAME [--no-adaptive] [--database PATH]` | Execute a saved workflow against the current DB (read-only; opt-out applies to rank mode) |
| `uv run careeros saved-search update NAME [--query TEXT] [--grouped/--no-grouped] [--untracked/--no-untracked] [--status S] [--exclude-status S] [--source S] [--location S] [--remote] [--min-score N] [--limit N] [--profile PATH] [--clear-*]` | Change a saved workflow (write; locks the DB) |
| `uv run careeros saved-search rename NAME NEW_NAME [--database PATH]` | Rename a saved workflow (write; locks the DB) |
| `uv run careeros saved-search delete NAME [--database PATH]` | Delete a saved workflow (write; locks the DB) |
| `uv run careeros review set JOB_ID DECISION [--note TEXT] [--database PATH]` | Set/update a job's review decision (write; locks the DB) |
| `uv run careeros review clear JOB_ID [--database PATH]` | Clear a job's review decision (write; locks the DB) |
| `uv run careeros review show JOB_ID [--database PATH]` | Show the current review decision for one job (read-only) |
| `uv run careeros review list [--decision D] [--limit N] [--database PATH]` | List review decisions, optionally filtered (read-only) |
| `uv run careeros review history JOB_ID [--limit N] [--database PATH]` | Show the decision history for one job (read-only) |
| `uv run careeros review next --profile PATH [--state S] [--min-score N] [--include-tracked] [--source S] [--location L] [--remote] [--grouped\|--no-grouped] [--limit N] [--format {text,json}] [--no-feedback] [--no-adaptive] [--database PATH]` | Rank the personalized daily review queue (read-only) |
| `uv run careeros systemd install --dry-run` | Preview packaged systemd user-unit install (writes nothing) |
| `uv run careeros systemd install [--units collect\|notify\|all] [--enable]` | Install wheel/source-packaged user units and optionally enable timers |
| `uv run careeros systemd uninstall --dry-run [--units collect\|notify\|all]` | Preview exact-owned unit removal |
| `uv run careeros systemd uninstall [--units collect\|notify\|all]` | Remove exact generated user units |
| `loginctl enable-linger "$USER"` | Manual only: run the timer without a login session |

The web Applications workspace uses `GET /application-pipeline` (default
`view=active`, server pagination) and reuses `PUT /applications/{job_id}`.
Pipeline records remain flat and owned by concrete `jobs.id`; active statuses
are interested/applied/interview/offer and terminal statuses are
rejected/withdrawn. Application mutations refresh only this narrow read model
and do not change Review decisions, reminders, or duplicate-group members.

Run order: **lint → mypy → test** (all independent; can run in parallel).

`collect all` runs each registered source sequentially, once per invocation, in
registry order, and applies `--limit` per source. Respect provider polling
limits — do not run repeated all-source collections more often than the most
restrictive source permits (Himalayas: at most once per day).

`collect due` collects sources whose last completed run (success or failure)
is older than their polling interval. A source with no completed run is due
immediately, and a failed run delays the next automatic attempt by exactly one
interval. If the newest attempt is incomplete, it is due immediately even when
an older completion is recent. Polling intervals live in
`src/careeros/collection_policies.py` (jobicy: 60 minutes, documented;
remotive: 6 hours, documented per Remotive's "no more than four times a day"
guidance; himalayas: 24 hours, documented because the feed refreshes every 24
hours and the provider states there is no benefit to polling more than once
per day; arbeitnow/remoteok/weworkremotely/workingnomads: provisional
defaults). The workingnomads collector targets an undocumented internal
Elasticsearch search endpoint (`POST https://www.workingnomads.com/jobsapi/_search`)
that powers the public listing pages; it may change without notice, and its
6-hour polling floor is a conservative provisional default because Working
Nomads publishes no rate-limit guidance. Manual
`collect <source>` and `collect all` bypass scheduling, but every collection
attempt is recorded in the `collection_runs` table. `collect due` exits 0 when
nothing is due or all due sources succeed, 1 when any due source fails or setup
fails, and 75 when another collection already holds the database lock.

A source's start marker commits first for crash observability. After fetch,
job upserts, deterministic regrouping, and successful run bookkeeping commit
atomically. An interruption before that commit leaves only an incomplete run;
an interruption after it leaves an unambiguous success. On restart, a
collection command repairs derived grouping when the newest attempt is
incomplete and `collect due` retries it immediately. Source upserts remain
idempotent; failed retries remain completed failures, distinct from the older
incomplete attempt.

All shared collector GET/POST helpers enforce a 16 MiB response ceiling from
both `Content-Length` and a bounded actual read. Oversize bodies are explicit
collection failures. Jobicy's request limit is the documented provider maximum
of 200 results. The existing seven-source registry is frozen for v1.0;
additional sources belong to v1.1.

Product 1 v1.1 M1 adds optional credential-free Lever collection. Its private
configuration is `${XDG_CONFIG_HOME:-~/.config}/careeros/sources.yaml` and must
be a user-owned regular `0600` file; `--sources-config`, then
`CAREEROS_SOURCES_CONFIG`, then the XDG default determine CLI configuration
selection. A missing default is valid and preserves exactly the released seven
sources. Lever's six-hour polling floor is provisional CareerOS policy.

Product 1 v1.1 M2 adds optional credential-free Greenhouse collection. It uses
the same private `sources.yaml` with `greenhouse.enabled` and up to eight
strictly validated board tokens. Greenhouse calls only the public Job Board
metadata endpoint and its `jobs?content=true` endpoint, uses board metadata
`name` as company, and identifies jobs by encoded board token plus public job
id. The six-hour Greenhouse polling floor is provisional CareerOS policy.
Requests are sequential and bounded to two per attempted board, with no
pagination, retries, backoff, concurrency, authentication, or per-job calls;
the shared 16 MiB response ceiling applies independently to each response.

`collect status` and `collect history` are read-only observability commands:
they never fetch jobs, never write to the database, and never alter scheduling
state. `collect status` shows, per registered source in registry order: the
polling interval (documented or provisional), the last completed attempt and
its outcome, fetched/inserted/updated counts, the last error when present, the
next due timestamp, and the current due state. It captures one timezone-aware
UTC `now` per invocation and uses the same scheduling rules as `collect due`
(a source with no completed attempt or a newest incomplete attempt is due, and
a malformed stored timestamp is treated as due). `collect history` lists
collection runs newest first
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

Every lock family uses the same fail-closed companion open: symlinks,
directories/non-regular objects, link counts other than one, files not owned by
the effective user, and pre-existing modes other than `0600` are rejected
before metadata can be written. `O_NOFOLLOW`/`O_CLOEXEC` are used when
available and the opened descriptor is validated with `fstat`; unsafe paths
and their targets remain unmodified. A normal owned stale regular `0600` file
continues to work.

On contention a collection command prints a message to stderr, does not fetch
any source, does not touch run history or the jobs table, and exits with code
75 (`EX_TEMPFAIL`, a conventional temporary-failure code). Example:

    uv run careeros collect due --database /tmp/careeros_lock_test.db
    echo $?

The read-only commands `collect status`, `collect history`, `search`, `show`,
`rank`, `group explain`, `application show`, `application list`,
`application history`, `reminder list`, `reminder show`, `saved-search list`,
`saved-search show`, `saved-search run`, `review next`, `review show`,
`review list`, `review history`, `report`, and the whole `export` family never
acquire the
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

## Reminder notifications

`reminder notify-due` delivers a **local** notification when a stored reminder
comes due, so a due reminder actually surfaces without keeping the web UI open.
Notifications are **local delivery only**: CareerOS never uses web push, email,
a cloud provider, or any external service, and delivery is a separate fact from
reminder completion — it never completes a reminder and never touches
application or review state. Delivery history lives in the `reminder_notifications`
table (schema v7), anchored to `reminders.id` with `ON DELETE RESTRICT`, so the
audit trail is never cascade-deleted.

Occurrence identity: a reminder is notified **once per occurrence**, where an
occurrence is the pair `(reminder_id, due_at)`. A delivered occurrence is unique
(partial unique index on `(reminder_id, due_at)` where `status = 'delivered'`),
so repeated runs never re-notify. Rescheduling a reminder (`reminder update
--due-at`) creates a new occurrence and notifies again; reopening a reminder
does **not** re-notify an already-delivered occurrence; completing or deleting a
reminder makes it ineligible (`completed_at IS NULL` and row presence gate the
candidate query). Overdue reminders notify exactly once when first observed
past due, including across a reboot that spanned the due time.

Concurrency and crash safety (`src/careeros/notifications/service.py`): a worker
claims up to `--limit` due occurrences inside one `BEGIN IMMEDIATE` transaction
under the per-database **notification lock** (`careeros.locking.notification_lock`,
a `.notify.lock` `flock` distinct from the collection lock; contention exits
75). It then dispatches with **no lock held**, and each outcome is finalized
under the lock with a `WHERE status = 'attempted'` guard. A worker that crashes
between claim and finalize leaves an `attempted` row that is expired after
`CLAIM_TIMEOUT` (5 minutes) and the occurrence becomes eligible again; a failed
delivery is recorded `failed` and retried on the next run with no cap.
A `delivered` outcome means notify-send (the desktop notification service)
accepted the notification with exit code 0; CareerOS cannot prove that a
visible banner was rendered, so a transient or unnoticed desktop pop-up is
presentation behavior after a successful delivery, not a delivery failure.

Backends (`src/careeros/notifications/backend.py`): `desktop` shells out to
`notify-send` (argv-only, bounded timeout, sanitized text, never `shell=True`,
never the network); `stdout` prints a deterministic line (for headless/test
environments); `auto` (default) selects `desktop` when `notify-send` is on PATH
**and** a session bus is reachable (`DBUS_SESSION_BUS_ADDRESS` set, or the
standard `$XDG_RUNTIME_DIR/bus` socket present), otherwise falls back to
`stdout` with an explanatory note. The bus — not the `DISPLAY`/`WAYLAND_DISPLAY`
variables — is the delivery signal, so the systemd user timer delivers visible
pop-ups inside a graphical login session even when the user manager inherits no
display environment. `--dry-run` previews the exact candidate set without
writing anything. `reminder notify-history` lists attempts newest first
(`--failed` narrows to failures, `--reminder` to one reminder); `reminder show`
prints a reminder's recent attempts, and `report` renders a Notifications
summary (pending/delivered/failed/last times). The HTTP slice deliberately
exposes **no** delivery state (`notification_summary` is pruned), keeping the
delivery audit local.

Readiness: `careeros diagnose` reports notify-send availability, the backend
`auto` resolves to and why, and whether the reminder-notification systemd timer
is installed/enabled/active, with actionable guidance when notifications are
not operational.

## Daily review queue

`review` manages a personalized daily review queue over stored jobs so the day's
candidates can be triaged in one ranked pass. Review state is **local workflow
state only**: CareerOS records your decision locally and never submits
applications, contacts employers, or infers deadlines. Decisions live in the
`review_decisions` table (schema v8) anchored to the stable `jobs.id` — **never**
to the derived grouping tables — so `group regroup` (which deletes and rebuilds
`job_groups`/`job_group_members`) can never delete or orphan them. Each
transition is recorded in the append-only `review_events` table, and both tables
use `ON DELETE RESTRICT` on `jobs.id`.

Supported decisions (validated centrally in `src/careeros/review/models.py` and
exposed as argparse choices):

- `shortlist` — worth pursuing now
- `dismiss` — not worth pursuing now
- `review_later` — maybe, revisit later

`review set JOB_ID DECISION [--note TEXT]` rules:

- The job must already exist (unknown job exits with code 1).
- Repeating the identical decision with **no** note is an idempotent no-op.
- Repeating the identical decision **with** a note appends a same-decision
  event, supporting "note without a decision change".
- Repeating a different decision is a real transition that updates the current
  decision and appends an event (the full decision history is preserved).
- `review clear JOB_ID` removes the current decision and appends a clearing
  event; clearing an untracked job is an idempotent no-op.

`review next` ranks the daily review queue: it scores the current unreviewed
jobs (`--state unreviewed` by default; also `shortlist`, `dismiss`,
`review_later`, `any`) against the required `--profile`, optionally excluding
tracked applications (`--include-tracked` re-includes them) and filtering by
`--source`, `--location`, `--remote`. Results are **grouped by default**
(`--grouped` on, `--no-grouped` disables), collapsed cross-source duplicates
into one result per canonical group. `--min-score` drops low-scoring results and
`--limit` caps the queue; a queue with no matching jobs exits 0 with a clean
message. Output is text by default (`--format json` renders the same data).
`review show`, `review list [--decision D]`, and `review history JOB_ID` are
read-only views of the current decision, all decisions (newest first), and one
job's decision history.

Every write (`set`, `clear`) is transactional and commits in one transaction,
rolling back completely on failure. Write commands acquire the per-database
collection lock and exit 75 on contention with no database change; the read-only
`next`, `show`, `list`, and `history` never lock and never change database
contents beyond idempotent schema initialization. Exit codes: `0` success,
`1` unknown job or database failure, `2` validation/usage error, `75` lock
contention. The `report` Career Snapshot renders the review queue and its
Overview counters using the shared review service (see below).

## Review feedback

`rank`, rank-mode `saved-search run`, `review next`, and the `report`
recommendations/review-queue sections apply a conservative, bounded score
adjustment derived from stored review decisions, so past judgments influence
future recommendations without ever overriding the profile match. Feedback is
**local workflow state only** and never contacts any external service.

Fixed weights (documented constants in `src/careeros/ranking/feedback.py`,
never candidate-profile fields):

- `shortlist` contributes `+5`
- `dismiss` contributes `-5`
- `review_later` contributes `0` and never occupies a slot
- at most **2** reviewed sources contribute, so the total adjustment is capped
  at `+10` / `-10`

Similarity is deliberately conservative and mirrors the duplicate-grouping
primitives in `src/careeros/grouping/normalize.py` without modifying them:

- normalized title compatibility is required (equal signature, or one a
  token-prefix of the other);
- company anchors similarity: equal companies are similar on compatible titles;
  with different companies only sufficiently specific (two-or-more-token)
  compatible titles count;
- an explicit remote-status conflict vetoes similarity;
- incompatible non-empty locations veto similarity;
- explicitly disjoint employment types veto similarity;
- skills are never used as a feedback-similarity channel;
- a candidate never receives feedback from itself;
- feedback never hard-excludes a candidate and can never bypass an existing
  hard-exclusion veto (it only adds/subtracts bounded points from the raw
  score).

Sources are resolved per canonical duplicate group: cross-source duplicates
contribute exactly once, represented by the lowest decided `jobs.id` (the same
deterministic rule the review queue uses). The read-only adapter
`src/careeros/review/feedback.py` builds the set once per command/report
operation; nothing is written and no lock is taken.

Opt-out: `rank`, `review next`, and `report` accept `--no-feedback`
(feedback is on by default). There is no stored feedback table and no schema
change; decisions stay anchored to `jobs.id`.

Structured output: `rank --format {text,json}` (default text). JSON output
exposes a `score_components` list per result (objects with `kind`, `label`,
`points`, `is_veto`) plus the existing `score`/`match_score`/`reasons` keys.
The default text output keeps the existing layout and adds a `Feedback:` line
only when a non-zero feedback adjustment applies; it never shows the full
component breakdown. `review next --format json` and `report --format json`
carry the same `score_components` data.

## R4 adaptive matching preferences

R4 introduced schema 12 with four preference tables; the current
`SCHEMA_VERSION` is 13:
`preference_state`, `explicit_preferences`, `preference_evidence`, and
`learned_preference_suppressions`. Learned aggregates are derived on read and
must not be materialized. Fresh v12 databases and v11-to-v12 migration create
one singleton state row; migration creates no explicit, evidence, or
suppression rows and sets `learning_reset_review_event_id` to the pre-upgrade
maximum `review_events.id`. Historical reviews are never seeded into learned
state.

Explicit preference authority preserves all nine `CandidateProfile` fields,
including list order and duplicates. Invocation-scoped supplied profiles
replace stored explicit state for that invocation. Otherwise a configured
stored profile is authoritative; `explicit_configured` distinguishes a saved
empty profile from absent stored state. Only populated explicit title,
location, remote, employment-type, and seniority dimensions lock the matching
learned dimension. Company has no explicit lock.

Meaningful review transitions update current structured evidence atomically
with the review decision and event. `shortlist` is positive, `dismiss` is
negative, and `review_later`/clear remove current evidence. Same-decision notes
remain audit-only. Evidence uses structured title, company, non-generic
location, explicit remote status, stated employment type, and title-derived
seniority; never descriptions, skills inferred from descriptions, notes,
paths, or sensitive attributes. Canonical duplicate opportunities contribute
once using the existing lowest-decided-job authority.

Activation requires at least two positives with net evidence at least `+2`, or
three negatives with net evidence at most `-2`; contradiction cancels
arithmetically and there is no time decay. Suppression preserves evidence but
makes the preference score-neutral. Restore removes suppression. Global reset
deletes evidence and suppressions, advances the review-event boundary, and
preserves explicit preferences, current review decisions, and review history.

The learned scoring layer is deterministic: title contributes up to `±4`, each
other learned dimension up to `±2`, and raw learned contribution is capped at
`±8`. Each surface preserves its pre-R4 direct `FeedbackSet` policy; effective
learned points use the combined legacy-feedback-plus-learned `±10` envelope.
`--no-feedback` controls only legacy direct review feedback, while
`--no-adaptive` controls only persistent learned preferences. HTTP requests
use `use_learned_preferences` (default true). Explanations expose only the
effective learned points applied after headroom/caps and never expose raw notes,
descriptions, filesystem paths, or preference provenance.

Adapted ranked surfaces are CLI rank/review/report, rank-mode saved-search
execution, ranked/review exports, `POST /ranked-jobs`, `POST /review-queue`,
personalized `POST /report`, and their web views. HTTP/web scoring keeps legacy
direct feedback disabled. Chronological
search, search-mode saved searches, Reviewed Opportunities, Application
Pipeline, administrative/audit views, Today/Attention, and profile-less
`GET /report` remain unchanged and non-adaptive. Ranked Job Detail renders the
originating response explanation and must never rerank.

Preference writes use the collection lock and existing local write-header
protections. The Matching Preferences UI keeps unsaved drafts local, then
refetches server-authoritative state after explicit save/clear,
suppress/restore, or reset. Request generations prevent stale responses from
clobbering newer edits. Learning, aggregation, ranking, and explanations are
local deterministic code with no model call or recurring model cost.

The current administration flow creates a SQLite backup before migration at
`<database>.pre-v13.backup.sqlite3` unless the operator supplies
`--backup` or explicitly acknowledges `--no-backup`. The migration validates
the target structure before advancing metadata and rolls back DDL/data/metadata
together on failure. Re-running administration against a current valid v13
database is idempotent. There is no
down migration; recovery is:

```bash
uv run careeros database restore \
  /path/to/careeros.sqlite3.pre-v13.backup.sqlite3 \
  --database /path/to/careeros.sqlite3 \
  --confirm-replace
```

Recovery intentionally discards changes newer than the selected backup.
Complete private state is a coordinated set: SQLite, the matching managed
Materials root (including `.careeros-store`), and the private profile/config.
SQLite backup/restore never includes the other two.

The production runbook is: (1) stop the server, collection/notification
workers, and enabled timers; (2) run `careeros database backup DEST --database
DB`; (3) while all writers remain stopped, copy the entire matching Materials
root with private permissions and marker intact, plus the private profile when
applicable; (4) record the selected paths and backup time together; (5) restore
SQLite only with `database restore` (which has no Materials-root option), then
separately restore the matching Materials root to its XDG location and select
it for subsequent operation with `CAREEROS_MATERIALS_ROOT`, or pass
`--materials-root` only to a supporting command such as `material audit` or
`serve`; restore/select the profile separately; (6) run `careeros material audit
--database DB --materials-root ROOT` before restarting writers. The DB
`material_store.store_id` and root marker are one binding. Missing/mismatched
markers, corrupt/missing objects, unsafe paths/permissions, and inconsistent
metadata fail closed; never edit markers or the DB to bypass the audit. Always
rehearse migration/recovery on temporary copies; never mutate real private
state during development or qualification.

## Candidate profile

`profile` manages the candidate profile file used by `rank` and `report`.
Profiles are plain JSON (`.json`) or YAML (`.yaml`/`.yml`) files whose format
is chosen by file extension (`src/careeros/ranking/profile.py`); unknown
top-level fields are silently ignored. All fields are optional:
`desired_titles`, `skills`, `required_skills`, `preferred_locations`,
`remote_preference` (string or null), `excluded_terms`, `hard_excluded_terms`,
`preferred_employment_types`, `preferred_seniority`.

```bash
uv run careeros profile init                       # create the private XDG profile
uv run careeros profile init --force               # overwrite an existing profile
uv run careeros profile init /tmp/me.json          # JSON starter at an explicit path
uv run careeros profile validate                   # validate the private profile
uv run careeros profile validate config/candidate_profile.example.yaml
uv run careeros profile show config/candidate_profile.example.json
```

The default path is the **private**
`${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml`, which must
never be committed; `init` refuses to overwrite an existing file unless
`--force` is passed. The committed fictional examples
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
  saved searches, review-queue counters (reviewed/unreviewed/shortlisted/
  dismissed/review-later groups), a per-source last-run summary, due sources,
  and a **source quality** summary computed by the shared source-statistics
  service (`src/careeros/source_stats/`). Source quality reports, per registered
  source in registry order: stored-job counts, eight missing-field statistics
  (`source_job_id`, `company`, `location`, `description`, `url`,
  `employment_type`, `remote_status`, `published_at` — a field counts as
  "not provided by source" when it is NULL or an empty string), field-coverage
  percentages derived from those counts, and `never_groupable_jobs` (stored
  rows whose normalized company or title signature is empty, so they can never
  participate in duplicate grouping). The text renderer summarizes a source in
  one line when nothing is missing; the JSON always carries the full counts and
  coverage for all eight fields.
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
- **Review queue** — the ranked daily review queue for the current unreviewed
  jobs, built by the shared review service (`build_queue` with
  `state="unreviewed"`, `min_score=0`, tracked applications excluded) against
  the report's effective profile. A profile or rank-mode `--saved-search` is
  required to produce ranked results; without one the section says skipped and
  the review counters still render from the shared review service. Results are
  grouped per canonical group, capped by `--limit`, and scored with the same
  engine as `review next`.
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

## Deterministic exports

`export` projects stored CareerOS state into deterministic JSON/JSONL/CSV
output, on stdout or into a file. It is **read-only**: it never fetches jobs,
never writes to the database, never acquires the collection lock (so it runs
while a collection is in progress), never performs schema initialization, and
never contacts any external service. A database path that does not exist yields
a valid empty export without creating the file. The five subcommands are
`jobs`, `ranked`, `review`, `source-stats`, and `collection-history`.

Record contract (shared by every format):

- **JSON** — one document `{"records": [...], "total": N, "truncated": bool}`,
  `indent=2`, `ensure_ascii=False`. Record keys appear in a fixed canonical
  column order.
- **JSONL** — one compact JSON record per line in the same order (no envelope).
- **CSV** — a header row of the canonical column names, then one row per
  record. Nested values (lists/dicts) render as stable JSON strings with
  sorted keys; `None` renders as an empty cell; booleans render as
  `true`/`false`.

`total` is the number of records the underlying pipeline produced before the
export cap; `truncated` is True exactly when the cap dropped records. `--limit
N` defaults to 20 and `0` means "no cap"; every value is hard-bounded by
`MAX_EXPORT_RECORDS = 1_000_000` (`src/careeros/export/models.py`). A broken
pipe (e.g. `careeros export jobs | head`) is not an error: the process exits 0.

Shared flags: `--format {json,jsonl,csv}` (default `json`), `--limit N`,
`--output PATH` (default stdout; refuses to overwrite without `--overwrite`),
`--overwrite`, and `--database`. Per-command flags:

- **`jobs`** — `--query`, `--remote`, `--location`, `--source`,
  `--employment-type`, `--grouped`, `--clean-descriptions`. Flat mode exports
  one record per stored job in search order (published desc, then id), each
  carrying its canonical `group_id` (nullable, resolved with the batch helper
  `group_ids_for_jobs`). Grouped mode exports one record per canonical group
  (representative plus `member_count`/`members`/`sources`/`urls`), ordered by
  ascending representative job id; filters apply to candidate member rows
  before grouping, matching the grouped-search semantics.
- **`ranked`** — requires `--query` and `--profile`; ranks through the shared
  `rank_candidates` pipeline with `limit=None`, then applies `--min-score`
  (already enforced by the pipeline) and the export cap. One-based `rank`
  reflects the final output order. Records carry `score`, `match_score`,
  `reasons`, and `score_components` (the `component_to_dict` form used by the
  JSON rank/review/report renderers). `--no-feedback` disables review feedback;
  `--grouped` collapses duplicates.
- **`review`** — requires `--profile`; reuses `build_queue` with the same
  defaults as `review next` (state `unreviewed`, grouped by default, tracked
  applications excluded unless `--include-tracked`). Each record carries
  `review_decision` (the resolved group decision or null). `--state`,
  `--min-score`, `--no-feedback`, `--grouped`/`--no-grouped` mirror
  `review next`.
- **`source-stats`** — one record per registered source in registry order with
  the eight per-field `{field}_missing`/`{field}_coverage_pct` columns;
  `--source` narrows to one source.
- **`collection-history`** — collection runs newest first (`started_at DESC,
  id DESC`) with `--source` and `--failed` filters.

Implementation: the CLI (`cli.run_export`) validates flags, builds an
`ExportRequest`, opens the database only when the file exists (skipping the
`connect()` parent-dir/file creation otherwise), and dispatches through
`careeros.export.service.build_records` (per-command builders), then renders
with `careeros.export.serializers.render_export` and writes via
`careeros.export.writer.write_export` (broken-pipe policy). `build_records`
accepts a `None` connection for the missing-file case; the shared
`empty_source_stats` helper feeds the empty source-stats projection. Exit
codes: `0` success, `1` output/profile/database failure, `2` usage errors
(negative limit, missing required `--query`/`--profile`).

## HTTP API

`src/careeros/api/` is a FastAPI slice (optional `api` extra). Serve
with `uv run uvicorn careeros.api:app`. The slice never fetches jobs, never
creates a database file, directory, or schema, and never contacts external
services. Its three POST endpoints, `POST /ranked-jobs`,
`POST /review-queue`, and `POST /saved-searches/{name}/execute`, are
computation-only: `POST /ranked-jobs` and `POST /review-queue` rank stored jobs
against a candidate profile submitted inline in the request body, and
`POST /saved-searches/{name}/execute` executes a stored saved-search workflow
(search or rank mode) through the shared execution pipeline. All three create
no resource. `PUT /review-decisions/{job_id}`,
`DELETE /review-decisions/{job_id}`,
`PUT /applications/{job_id}`,
`POST /reminders`,
`PATCH /reminders/{reminder_id}`,
`PUT /reminders/{reminder_id}/complete`,
`PUT /reminders/{reminder_id}/reopen`, and
`DELETE /reminders/{reminder_id}`,
and `POST /saved-searches`,
and `DELETE /saved-searches/{name}`,
and `PATCH /saved-searches/{name}` are the slice's mutation endpoints:
`PUT /review-decisions/{job_id}` sets or replaces one job's current review
decision through the shared `careeros.review.service.set_decision` domain
service, `DELETE /review-decisions/{job_id}` clears one job's current review
decision through the shared `careeros.review.service.clear_decision` domain
service, `PUT /applications/{job_id}` starts tracking a job or moves it
through the application lifecycle through the shared
`careeros.applications.service.set_application_status` domain service,
`POST /reminders` adds a local follow-up reminder for a stored job through the
shared `careeros.reminders.service.add_reminder` domain service
(intentionally non-idempotent, never accepts a message, and never creates
notification attempts or triggers notification delivery), and
`PUT /reminders/{reminder_id}/complete` marks an open reminder completed
through the shared `careeros.reminders.service.complete_reminder` domain
service (idempotent, never accepts a message, and never creates notification
attempts or triggers notification delivery), and
`PUT /reminders/{reminder_id}/reopen` reopens a completed reminder through the
shared `careeros.reminders.service.reopen_reminder` domain service (idempotent,
never accepts a message, and never creates notification attempts or triggers
notification delivery), and
`DELETE /reminders/{reminder_id}` deletes one reminder through the
shared `careeros.reminders.service.delete_reminder` domain service (returning
the deleted `ReminderItem`; a repeated DELETE is a canonical `404`, and a
reminder referenced by `reminder_notifications` is protected by the existing
`ON DELETE RESTRICT` constraint), and
`PATCH /reminders/{reminder_id}` updates a reminder's kind and/or due time
through the shared `careeros.reminders.service.update_reminder` domain service
(a genuine partial update that requires at least one of `kind`/`due_at`, is
idempotent for repeated identical values, and never accepts, clears, or
exposes the stored reminder message), and
`POST /saved-searches` creates a new **search-mode** saved-search definition
through the shared `careeros.saved_searches.service.create_search` domain
service (search-mode authoring only: the request schema restricts `mode` to the
single literal `"search"`, which also defaults it, so rank-mode creation is
impossible over HTTP because a rank workflow requires a local `profile_path`;
`profile_path`, `description`, and `min_score` are never accepted, duplicate
names are a domain validation conflict mapped to `422`, and creation is
intentionally non-idempotent — a repeated identical POST fails with the
canonical duplicate-name `422` and creates no second row), and
`DELETE /saved-searches/{name}` deletes one saved-search definition through the
shared `careeros.saved_searches.service.delete_search` domain service
(body-less, returning the deleted canonical `SavedSearchItem`; a repeated
DELETE is a canonical `404`, name lookup is case-insensitive through the shared
service and the response preserves the stored/original name casing, and
`profile_path`/`description` remain structurally absent), and
`PATCH /saved-searches/{name}` updates or renames one saved-search definition
through the shared `careeros.saved_searches.service.update_search` and
`careeros.saved_searches.service.rename_search` domain services (a rename-only
body carrying exactly the replacement `name`; source-name lookup is
case-insensitive through the shared service, a same-name or case-only rename
is an idempotent no-op that preserves the stored casing and `updated_at`, a
genuine rename updates the stored name and `updated_at`, and a duplicate target
is a domain validation conflict mapped to `422` with the canonical domain text
— never `409`; a definition-update body updates `query`, `grouped`, `source`,
`remote`, `location`, `untracked`, `include_statuses`, `exclude_statuses`,
`min_score`, `result_limit`, and/or clears filters via `clears`; the route
never accepts or exposes `profile_path`, `description`, or any free text);
each
domain service owns the transaction and audit event. HTTP writes run on a
`writable_connection` (see
`src/careeros/api/deps.py`): the configured database file must already exist (a
missing/uninitialized/malformed database is the canonical privacy-safe `500`
and nothing is ever created), the per-database `collection_lock` is acquired
before the connection is opened and held for the whole request (cross-process
`flock`, same lock the CLI/collector writers use), lock contention maps to a
privacy-safe `503`, and the domain service — never the connection dependency —
owns commit/rollback. The mutations return the same privacy-safe
`ReviewDecisionItem` / `ApplicationItem` / `ReminderItem` / `SavedSearchItem`
representations as
`GET /review-decisions/{job_id}` / `GET /applications/{job_id}` /
`GET /reminders/{reminder_id}` / `GET /saved-searches/{name}` and
never accept or expose review or application notes (`note` in either request
body is a `422`), nor reminder messages (`message` in a `POST /reminders` or
`PATCH /reminders/{reminder_id}` body is a `422`).
The database path comes
from `CAREEROS_DATABASE` (see
`src/careeros/settings.py`), read per request with the standard library; a
missing database is treated as empty by the read endpoints. Each request opens
one SQLite connection (`mode=ro` for reads, `mode=rw` under the lock for the
write endpoints) and closes it deterministically; no connection is shared across
requests. Both dependencies open connections with `check_same_thread=False`
because FastAPI drives generator-dependency steps (enter, use, exit) through
the anyio threadpool and may execute them in different threads; each connection
is scoped to one request and its steps run strictly sequentially, so no thread
ever touches a connection concurrently. The app-level `sqlite3.Error` handler
distinguishes transient contention (`database is locked`, `database is busy`,
`database schema is locked`, `database schema has changed`), which maps to the
retryable privacy-safe `503` `LOCK_CONTENTION_MESSAGE`, from the canonical
unavailable-schema errors (an uninitialized or malformed database, e.g. `no
such table` or `file is not a database`), which keep the privacy-safe `500`
`DATABASE_UNAVAILABLE_MESSAGE`. Endpoints reuse the shared database/presenter
layers and the canonical
job projection (`presenters.job_dict`). API code must never import `careeros.cli`
(an AST test enforces this); the `/jobs/{job_id}` 404 detail reproduces the
canonical `No job found with ID {id}.` message locally.

- `GET /health` — `200` with `status`, `database` (`present`/`absent`), and
  `schema_version` (`null` when absent or uninitialized).
- `GET /jobs` — canonical job order (`published_at DESC NULLS LAST, id ASC`)
  with deterministic `offset`/`limit` pagination (defaults 0/20, `limit`
  capped at 100) plus optional `source`, `remote`, `location`,
  `employment_type` filters and an optional `query` keyword (case-insensitive
  substring match across title, company, and description; descriptions
  participate in matching but are never returned); returns
  `{items, total, offset, limit}`. The listing also accepts the same
  application-tracking filters as `POST /ranked-jobs` and the CLI search
  vocabulary: `untracked` (boolean), `include_statuses` (repeatable), and
  `exclude_statuses` (repeatable). With no tracking rule active the response is
  byte-identical to the pre-tracking listing. With a rule active, the base
  keyword/filter set is narrowed through the shared application filter
  (`application_statuses_for_jobs` + `apply_filter_to_flat`) **before**
  `offset`/`limit` slicing: `untracked=true` keeps only untracked jobs,
  `include_statuses` keeps only tracked jobs whose current status is in the
  selected set (untracked jobs are excluded while it is active),
  `exclude_statuses` hides tracked jobs with the selected statuses while
  untracked jobs remain eligible. Duplicate status values are harmless
  (deduplicated through the domain representation); omitted/empty lists act as
  no filter. Every supplied status is validated with the shared
  `validate_status` vocabulary (invalid value returns `422` with the canonical
  domain text), and the pairwise conflicts `untracked`+`include_statuses`,
  `untracked`+`exclude_statuses`, and `include_statuses`+`exclude_statuses`
  return `422` with the canonical ranked-jobs detail strings. `total` is the
  full post-filter count before pagination; the endpoint is strictly read-only,
  never takes the collection lock, never creates a database file/directory/
  schema when the database is missing (missing database returns a `200` empty
  envelope), and the `Job` projection is unchanged (no per-item status field is
  added). The listing also accepts the select-only review-state filters
  `unreviewed` (boolean), `include_decisions` (repeatable), and
  `exclude_decisions` (repeatable), applied after the application-tracking
  filter and before `offset`/`limit` slicing: `unreviewed=true` keeps only jobs
  with no current review decision, `include_decisions` keeps only reviewed jobs
  whose current decision is in the set (unreviewed jobs are excluded while it
  is active), and `exclude_decisions` removes jobs whose current decision is in
  the set while unreviewed jobs remain eligible. Current decisions are resolved
  in ONE batched lookup (`careeros.review.service.decisions_for_jobs`) only
  when a review filter is active — never per job and never when no filter is
  active — and no review decision, status, note, or history is exposed: the
  `Job`/`JobListResponse` schema is unchanged. Duplicate decision values are
  deduplicated (semantic union) and omitted/empty lists act as no filter. Every
  supplied decision is validated with the shared
  `careeros.review.models.validate_decision` vocabulary (canonical:
  `shortlist`, `dismiss`, `review_later`; invalid or empty-string values return
  `422` with the canonical domain text) and the pairwise conflicts
  `include_decisions`+`exclude_decisions` and `unreviewed`+either decision list
  return `422` with the canonical detail strings; mixing with the
  application-tracking filters composes by AND with no cross-dimension
  conflict.
- `GET /jobs/{job_id}` — one job or `404` with the canonical message.
- `GET /jobs/{job_id}/group` — the canonical distinct opportunity containing one
  stored job, using the shared `group_dict` projection (exact field order
  `group_id`, `representative`, `members`, `sources`, `urls`). Resolves
  membership through the shared `group_ids_for_jobs` helper, so a non
  representative grouped member returns the same canonical group as its
  representative; an ungrouped or never-grouped job returns a one-member
  singleton group with `group_id` `null` (matching the `GET /groups` singleton
  convention), and members are ordered ascending by job ID with `sources`/`urls`
  in ordered-distinct form. An unknown job returns `404` with the canonical
  `No job found with ID {job_id}.` message; non-integer path input returns
  FastAPI's normal `422`. Read-only: takes no query parameters, never acquires
  the collection lock, never reads profile files or the filesystem, and a
  missing database file returns the same canonical `404` without creating a
  file, directory, or schema. Job descriptions, matcher diagnostics, and local
  workflow state are never exposed.
- `GET /sources/stats` — unified per-source statistics in collector registry
  order (one record per registered source, including zero-count sources),
  computed by the shared `compute_source_stats` service. Each record carries
  `source`, `total_jobs`, `never_groupable_jobs`, and one `fields` entry per
  tracked field in `MISSING_FIELD_COLUMNS` order (`field`, `missing_count`,
  `provided_count`, `coverage_pct`; `coverage_pct` is `null` when the source
  has no stored jobs). Supports the same deterministic `offset`/`limit`
  pagination as `/jobs` (defaults 0/20, `limit` capped at 100) plus an
  optional equality `source` filter; an unknown `source` value returns `200`
  with empty `items` and `total` 0. A missing database file returns all
  registered sources with zero counts and `null` coverage without creating the
  file.
- `GET /collection-runs` — collection-run history newest first, reusing the
  shared `database.runs.list_runs` service. Deterministic ordering is
  `started_at DESC, id DESC` (ties broken by `id`). Supports the same
  deterministic `offset`/`limit` pagination as `/jobs` (defaults 0/20, `limit`
  capped at 100) plus an optional equality `source` filter and a boolean
  `failed` filter (completed failed runs only, matching the CLI `--failed`
  flag; incomplete runs are excluded). `total` is the count after filtering
  and before `offset`/`limit` slicing. Items carry `run_id`, `source`,
  `started_at`, `finished_at`, `succeeded` (`1` success, `0` failed, `null`
  incomplete), `fetched`, `inserted`, and `updated`; `started_at`/`finished_at`
  are raw stored ISO-8601 strings so malformed stored values survive without
  crashing. The provider `error` text is intentionally **omitted** (it can
  embed URLs, hostnames, or local paths); the CLI `collect history` and
  `export collection-history` remain the full-diagnostic channels. An unknown
  `source` value returns `200` with empty `items` and `total` 0, and a missing
  database file returns an empty envelope without creating the file.
- `GET /applications` — tracked applications newest first (`created_at DESC,
  id DESC`, the shared `database.applications.list_applications` order) with
  deterministic `offset`/`limit` pagination (defaults 0/20, `limit` capped at
  100), an optional repeatable `status` filter, and an optional `job_id` query
  filter (positive integer; `0`/negative/non-integer values return `422`, an
  absent value leaves the unfiltered listing unchanged). A `job_id` filter
  narrows the listing to one job's current application: a known-but-untracked
  job and an unknown job both return a `200` empty collection (the list style
  never distinguishes absence from an unknown job; the canonical singular
  `GET /applications/{job_id}` retains that distinction). Every `status` value is
  validated against `APPLICATION_STATUSES` (via `validate_status`); an invalid
  value returns `422`. Repeated statuses use inclusive OR semantics. The
  filter is applied before pagination; `total` is the filtered count before
  `offset`/`limit` slicing. The envelope is `{items, total, offset, limit}`.
  Each item uses the shared `presenters.application_item_dict` projection with
  the exact field order `application_id`, `job_id`, `current_status`,
  `created_at`, `updated_at`, `job`; the nested `job` is the canonical
  `job_dict` projection. Application notes, the event history, reminders, and
  review decisions are deliberately omitted from the application list and
  detail responses.
- `GET /applications/{job_id}` — one application item using the exact same
  shape as a list item. An unknown job returns `404` with the canonical
  `No job found with ID {job_id}.` message; an existing-but-untracked job
  returns `404` with `No application found for job {job_id}.`. Non-integer
  path input returns FastAPI's normal `422`.
- `GET /applications/{job_id}/history` — a job's append-only application
  status-transition history newest first (`created_at DESC, id DESC`, the
  shared `database.applications.list_application_events` order) with
  deterministic `offset`/`limit` pagination (defaults 0/20, `limit` capped at
  100). An unknown job returns the canonical job `404`; an existing-but-untracked
  job returns `404` with `No application found for job {job_id}.`. Each item
  uses the shared `presenters.application_event_dict` projection with the exact
  field order `event_id`, `job_id`, `old_status` (`null` for the first
  assignment), `new_status`, `created_at`; event `note`s and the internal
  `application_id` are deliberately omitted. The envelope is
  `{items, total, offset, limit}`; `total` is the event count before
  `offset`/`limit` slicing.
- `PUT /applications/{job_id}` — the slice's application-status mutation
  endpoint. Starts tracking a job or moves it through the application lifecycle
  through the shared `careeros.applications.service.set_application_status`
  domain service, which owns the transaction, commit/rollback, the append-only
  `application_events` audit, and idempotent same-status no-op behavior; the
  route performs no SQL. The request body is `{"status": "applied"}` via a
  dedicated `ApplicationStatusRequest` model with `extra="forbid"`: unknown
  body fields — including any `note` — are rejected with `422`, and application
  notes are never accepted or exposed by this endpoint. `status` is validated
  by the existing shared `validate_status` vocabulary (`interested`, `applied`,
  `interview`, `offer`, `rejected`, `withdrawn`); an invalid value returns
  `422` with the canonical domain validation text. An unknown job returns
  `404` with the canonical `No job found with ID {job_id}.` message;
  non-integer path input returns FastAPI's normal `422`. Every successful
  `PUT` returns `200` with the canonical `ApplicationItem` projection
  (identical to the `GET` detail shape, exact field order `application_id`,
  `job_id`, `current_status`, `created_at`, `updated_at`, `job`). First
  assignment creates the application and one event with `old_status` `null`;
  a real transition updates the current status and appends one event while
  preserving `application_id`; repeating the identical status is idempotent
  under the domain model (no new event, no `updated_at` mutation, equivalent
  response body). The mutation runs on `writable_connection`: the configured
  database must already exist (missing/uninitialized/malformed databases are
  the canonical privacy-safe `500` and nothing is ever created), the
  per-database `collection_lock` is held for the whole request (lock
  contention maps to a privacy-safe `503` with a fixed retry-later message
  that never exposes lock internals), and the domain service — never the
  dependency — owns commit/rollback. A failure between writing the current
  status and its audit event rolls the entire mutation back, and the lock is
  always releasable.
- `GET /reminders` — reminders soonest first (`due_at ASC, id ASC`, the shared
  `database.reminders.list_reminders` order) with deterministic
  `offset`/`limit` pagination (defaults 0/20, `limit` capped at 100) and
  optional `job_id` (>= 1; an unknown job returns `200` with empty `items` and
  `total` 0), `kind` (validated against `REMINDER_KINDS`), `state` (`open`,
  `overdue`, `due`, `upcoming`, `completed`), `after`/`before` (ISO-8601
  timestamps with explicit UTC offsets, due-time windows), and `as_of` filters.
  Invalid `kind`, `state`, `after`, `before`, or `as_of` values return `422`.
  With no `state` the endpoint returns both open and completed reminders. The
  time-dependent states `overdue`, `due`, and `upcoming` are evaluated against
  `as_of` (an explicit ISO-8601 timestamp with a UTC offset; `Z` is accepted
  and naive/date-only values return `422`); when `as_of` is omitted the
  server's current UTC time is used, matching the CLI default. `as_of` has no
  effect on `open`/`completed` or when no `state` is given. Filtering is
  applied before pagination; `total` is the filtered count before
  `offset`/`limit` slicing. The envelope is `{items, total, offset, limit}`.
  Each item uses the shared `presenters.reminder_item_dict` projection with
  the exact field order `reminder_id`, `job_id`, `kind`, `due_at`,
  `completed_at`, `created_at`, `updated_at`, `job`; the nested `job` is the
  canonical `job_dict` projection. Reminder messages, notification delivery
  history, application notes, and review decisions are deliberately omitted
  from every response.
- `GET /reminders/{reminder_id}` — one reminder item using the exact same
  shape as a list item. An unknown reminder returns `404` with the canonical
  `No reminder found with ID {reminder_id}.` message; a defensively-detected
  reminder whose job is missing returns `404` with `No job found with ID
  {job_id}.`. Non-integer path input returns FastAPI's normal `422`.
- `POST /reminders` — the slice's reminder-creation mutation endpoint. Adds a
  local follow-up reminder for an existing stored job through the shared
  `careeros.reminders.service.add_reminder` domain service, which owns the
  insert, the transaction commit, and rollback on any `sqlite3.Error`; the
  route performs no SQL. The request body is
  `{"job_id": N, "kind": "interview", "due_at": "2026-08-10T14:00:00Z"}` via a
  dedicated `ReminderCreateRequest` model with `extra="forbid"`: unknown body
  fields — including any `message` or `note` — are rejected with `422`, and
  reminder free text is never accepted, persisted, or returned by this
  endpoint. `job_id` must be `>= 1` (`422` otherwise); `kind` is validated by
  the shared `validate_kind` vocabulary (`follow_up`, `interview`, `offer`,
  `review`, `custom`) and `due_at` by the shared `parse_due_at` rules (a full
  ISO-8601 timestamp with an explicit UTC offset; `Z` accepted; naive and
  date-only values rejected), so an invalid value returns `422` with the
  canonical domain validation text. An unknown job returns `404` with the
  canonical `No job found with ID {job_id}.` message. Every successful `POST`
  returns `200` with the canonical `ReminderItem` projection (identical to the
  `GET` detail shape, exact field order `reminder_id`, `job_id`, `kind`,
  `due_at`, `completed_at` (`null` on creation), `created_at`, `updated_at`,
  `job`). Creation is **intentionally non-idempotent**: two identical valid
  POSTs create two distinct reminder rows with distinct `reminder_id`s (there
  is no idempotency key, uniqueness constraint, or duplicate suppression, and
  multiple reminders per job are supported). Creation never creates
  `reminder_notifications` rows, never triggers notification delivery, and
  never contacts any external service. The mutation runs on
  `writable_connection` like the other write endpoints: the configured database
  must already exist (missing/uninitialized/malformed databases are the
  canonical privacy-safe `500` and nothing is ever created), the per-database
  `collection_lock` is held for the whole request (lock contention maps to a
  privacy-safe `503` with a fixed retry-later message that never exposes lock
  internals), and the domain service — never the dependency — owns
  commit/rollback. The lock is always releasable after 404/422/500 responses.
- `PUT /reminders/{reminder_id}/complete` — the slice's reminder-completion
  mutation endpoint. Marks an open reminder completed through the shared
  `careeros.reminders.service.complete_reminder` domain service, which owns the
  `UPDATE`, the transaction commit, and rollback on any `sqlite3.Error`; the
  route performs no SQL, no commit, and no rollback. The route declares no
  request body and no request schema: a sent HTTP request body is never
  parsed, validated, stored, logged, or used, so reminder free text (and any
  other sentinel field) can never influence the operation or appear in any
  response. An unknown reminder returns `404` with the canonical `No reminder
  found with ID {reminder_id}.` message; non-integer path input returns
  FastAPI's normal `422`. Every successful `PUT` returns `200` with the
  canonical `ReminderItem` projection (identical to the `GET` detail shape,
  exact field order `reminder_id`, `job_id`, `kind`, `due_at`, `completed_at`,
  `created_at`, `updated_at`, `job`); `completed_at` equals `updated_at` after
  a first completion (the domain sets both to the same completion timestamp).
  Completion is **idempotent**: repeating the identical PUT on an already
  completed reminder is a true domain no-op that mutates no row, preserves the
  original `completed_at`/`updated_at`, and returns a byte-deterministic
  response for identical stored database state. Completion never creates
  `reminder_notifications` rows, never triggers notification delivery, never
  accepts a message, and never contacts any external service. The mutation
  runs on `writable_connection` like the other write endpoints: the configured
  database must already exist (missing/uninitialized/malformed databases are
  the canonical privacy-safe `500` and nothing is ever created), the
  per-database `collection_lock` is held for the whole request (lock
  contention maps to a privacy-safe `503` with a fixed retry-later message
  that never exposes lock internals), and the domain service — never the
  dependency — owns commit/rollback. The lock is always releasable after
  404/500/503 responses.
- `PUT /reminders/{reminder_id}/reopen` — the slice's reminder-reopen mutation
  endpoint. Reopens a completed reminder through the shared
  `careeros.reminders.service.reopen_reminder` domain service, which owns the
  `UPDATE`, the transaction commit, and rollback on any `sqlite3.Error`; the
  route performs no SQL, no commit, and no rollback. The route declares no
  request body and no request schema: a sent HTTP request body is never
  parsed, validated, stored, logged, or used, so reminder free text (and any
  other sentinel field) can never influence the operation or appear in any
  response. An unknown reminder returns `404` with the canonical `No reminder
  found with ID {reminder_id}.` message; non-integer path input returns
  FastAPI's normal `422`. Every successful `PUT` returns `200` with the
  canonical `ReminderItem` projection (identical to the `GET` detail shape,
  exact field order `reminder_id`, `job_id`, `kind`, `due_at`, `completed_at`,
  `created_at`, `updated_at`, `job`); `completed_at` is `null` after a first
  reopen (the domain clears it) and `updated_at` is set to the domain's
  current UTC timestamp. Reopen is **idempotent**: reopening an already-open
  reminder is a true domain no-op that mutates no row, preserves the original
  `completed_at`/`updated_at`, and returns a byte-deterministic response for
  identical stored database state. Reopen never creates
  `reminder_notifications` rows, never triggers notification delivery, never
  accepts a message, never touches `reminder.message`, and never contacts any
  external service. The mutation runs on `writable_connection` like the other
  write endpoints: the configured database must already exist
  (missing/uninitialized/malformed databases are the canonical privacy-safe
  `500` and nothing is ever created), the per-database `collection_lock` is
  held for the whole request (lock contention maps to a privacy-safe `503`
  with a fixed retry-later message that never exposes lock internals), and the
  domain service — never the dependency — owns commit/rollback. The lock is
  always releasable after 404/500/503 responses.
- `DELETE /reminders/{reminder_id}` — the slice's reminder-deletion mutation
  endpoint. Deletes one reminder through the shared
  `careeros.reminders.service.delete_reminder` domain service, which owns the
  `DELETE`, the transaction commit, and rollback on any `sqlite3.Error`; the
  route performs no SQL, no commit, and no rollback. The route declares no
  request body and no request schema: a sent HTTP request body is never
  parsed, validated, stored, logged, or used, so reminder free text (and any
  other sentinel field) can never influence the operation or appear in any
  response. An unknown reminder returns `404` with the canonical `No reminder
  found with ID {reminder_id}.` message; non-integer path input returns
  FastAPI's normal `422`. Every successful `DELETE` returns `200` with the
  canonical `ReminderItem` projection of the **deleted** reminder (identical to
  the `GET` detail shape, exact field order `reminder_id`, `job_id`, `kind`,
  `due_at`, `completed_at`, `created_at`, `updated_at`, `job`), built from the
  row the domain service returns — the reminder is never re-read after
  deletion. Deletion is genuinely destructive, not an idempotent no-op: a
  repeated `DELETE` of an already-deleted reminder is a canonical `404` (the
  resource no longer exists), and `204` is never used. Deleting a completed
  reminder is allowed and the returned representation preserves its pre-delete
  `completed_at`. A reminder referenced by `reminder_notifications` is
  protected by the existing `ON DELETE RESTRICT` constraint, so its deletion
  raises `sqlite3.IntegrityError`, the domain service rolls back, and the
  app-level `sqlite3.Error` handler returns the canonical privacy-safe `500`
  with both rows preserved (an existing domain/schema constraint, not a new
  HTTP status). Deletion never creates `reminder_notifications` rows, never
  triggers notification delivery, never accepts a message, and never contacts
  any external service. The mutation runs on `writable_connection` like the
  other write endpoints: the configured database must already exist
  (missing/uninitialized/malformed databases are the canonical privacy-safe
  `500` and nothing is ever created), the per-database `collection_lock` is
  held for the whole request (lock contention maps to a privacy-safe `503`
  with a fixed retry-later message that never exposes lock internals), and the
  domain service — never the dependency — owns commit/rollback. The lock is
  always releasable after 404/500/503 responses.
- `PATCH /reminders/{reminder_id}` — the slice's reminder-update mutation
  endpoint. Updates a reminder's `kind` and/or `due_at` only through the
  shared `careeros.reminders.service.update_reminder` domain service, which
  owns the `UPDATE`, the transaction commit, and rollback on any
  `sqlite3.Error`; the route performs no SQL, no commit, and no rollback.
  The request body is `{"kind": "offer"}` and/or `{"due_at":
  "2026-08-11T09:00:00Z"}` via a dedicated `ReminderUpdateRequest` model with
  `extra="forbid"`; at least one non-null field is required, so `{}`,
  `{"kind": null}`, `{"due_at": null}`, and `{"kind": null, "due_at": null}`
  all return `422`, and unknown body fields — including any `message`, `note`,
  `description`, `profile_path`, `clear_message`, `completed_at`, or `job_id`
  — are rejected with `422`. Reminder free text is never accepted, cleared,
  inspected, or returned by this endpoint: the route always calls the domain
  with `message=None` and `clear_message=False`, so a message previously
  created through the CLI survives HTTP updates byte-identically. `kind` is
  validated by the shared `validate_kind` vocabulary (`follow_up`, `interview`,
  `offer`, `review`, `custom`) and `due_at` by the shared `parse_due_at` rules
  (a full ISO-8601 timestamp with an explicit UTC offset; `Z` accepted; naive
  and date-only values rejected), so an invalid value returns `422` with the
  canonical domain validation text. An unknown reminder returns `404` with the
  canonical `No reminder found with ID {reminder_id}.` message; non-integer
  path input returns FastAPI's normal `422`. Every successful `PATCH` returns
  `200` with the canonical `ReminderItem` projection (identical to the `GET`
  detail shape, exact field order `reminder_id`, `job_id`, `kind`, `due_at`,
  `completed_at`, `created_at`, `updated_at`, `job`); omitted fields retain
  their stored values and a genuine change updates `updated_at` per the domain
  semantics. Re-PATCHing identical values is an **idempotent no-op**: the
  domain issues no `UPDATE`, `updated_at` is preserved, and the response is
  byte-deterministic for identical stored database state. Updating never
  creates `reminder_notifications` rows, never triggers notification delivery,
  and never contacts any external service. The mutation runs on
  `writable_connection` like the other write endpoints: the configured database
  must already exist (missing/uninitialized/malformed databases are the
  canonical privacy-safe `500` and nothing is ever created), the per-database
  `collection_lock` is held for the whole request (lock contention maps to a
  privacy-safe `503` with a fixed retry-later message that never exposes lock
  internals), and the domain service — never the dependency — owns
  commit/rollback. The lock is always releasable after 404/422/500/503
  responses. The update introduces no schema change and no migration.
- `GET /review-decisions` — current review decisions joined with their jobs,
  newest first (`updated_at DESC, id DESC`, the shared
  `database.review.list_decisions` order) with deterministic `offset`/`limit`
  pagination (defaults 0/20, `limit` capped at 100), an optional repeatable
  `decision` filter, and an optional `job_id` query filter (positive integer;
  `0`/negative/non-integer values return `422`, an absent value leaves the
  unfiltered listing unchanged). A `job_id` filter narrows the listing to one
  job's current decision: a known-but-unreviewed job and an unknown job both
  return a `200` empty collection (the list style never distinguishes absence
  from an unknown job; the canonical singular `GET /review-decisions/{job_id}`
  retains that distinction). Every `decision` value is validated against the
  shared `review.models.validate_decision` vocabulary (`shortlist`, `dismiss`,
  `review_later`); an invalid value returns `422`. The filter is applied before
  pagination; `total` is the filtered count before `offset`/`limit` slicing.
  The envelope is `{items, total, offset, limit}`. Each item uses the shared
  `presenters.review_decision_item_dict` projection with the exact field order
  `decision_id`, `job_id`, `decision`, `created_at`, `updated_at`, `job`; the
  nested `job` is the canonical `job_dict` projection. Decision notes, the
  decision event history, reminders, application notes, and review-feedback
  detail are deliberately omitted from every response. A missing database file
  returns an empty envelope without creating the file.
- `GET /reviewed-opportunities` — narrow, read-only reviewed-workspace model.
  Query parameters are `decision` (`shortlist` by default), `grouped` (`true`
  by default), and deterministic `offset`/`limit` pagination (0/20 by default,
  limit capped at 100). The response is `{items, counts, grouped, decision,
  total, offset, limit}`; counts for `shortlist`, `review_later`, and `dismiss`
  are calculated before selected-state filtering and pagination. Each item
  contains the canonical public `group`, `review_decision`, concrete
  `decision_job_id`, `decision_updated_at`, and `conflict`. Grouped resolution
  reuses `review.service.resolve_group_decision`: the lowest decided job ID is
  authoritative and conflicting member decisions are surfaced. Ordering is
  owner decision `updated_at DESC`, decision-row ID DESC, then representative
  job ID ASC. Flat mode treats decided concrete jobs independently. The
  endpoint performs no ranking, feedback, report, review-queue, application,
  or reminder work and never creates a missing database.
- `GET /review-decisions/{job_id}` — one current review decision using the
  exact same shape as a list item. An unknown job returns `404` with the
  canonical `No job found with ID {job_id}.` message; an existing-but-unreviewed
  job returns `404` with `No review decision found for job {job_id}.`.
  Non-integer path input returns FastAPI's normal `422`.
- `PUT /review-decisions/{job_id}` — the slice's first mutation endpoint. Sets
  or replaces the current review decision for one existing job through the
  shared `review.service.set_decision` domain service, which owns the
  transaction, audit event, and rollback; the route performs no SQL. The
  request body is `{"decision": "shortlist"}` via a dedicated
  `ReviewDecisionRequest` model with `extra="forbid"`: unknown body fields —
  including any `note` — are rejected with `422`, and review notes are never
  accepted or exposed by this endpoint. `decision` is validated by the existing
  shared `validate_decision` vocabulary (`shortlist`, `dismiss`,
  `review_later`); an invalid value returns `422`. An unknown job returns `404`
  with the canonical `No job found with ID {job_id}.` message; non-integer path
  input returns FastAPI's normal `422`. Every successful `PUT` returns `200`
  with the canonical `ReviewDecisionItem` projection (identical to the `GET`
  detail shape, exact field order `decision_id`, `job_id`, `decision`,
  `created_at`, `updated_at`, `job`). Repeating the same decision is idempotent
  under the domain model: no new event is appended and timestamps are not
  mutated, so repeated identical `PUT` requests return equivalent bodies. The
  mutation runs on `writable_connection` like `PUT /applications/{job_id}`: the
  configured database must already exist (missing/uninitialized/malformed
  databases are the canonical privacy-safe `500` and nothing is ever created),
  the per-database `collection_lock` is held for the whole request (lock
  contention maps to a privacy-safe `503` with a fixed retry-later message that
  never exposes lock internals), and the domain service — never the dependency
  — owns commit/rollback. A failure between writing the current decision and its
  audit event rolls the entire mutation back, and the lock is always
  releasable.
- `DELETE /review-decisions/{job_id}` — clear one job's current review decision,
  returning it to unreviewed. All logic lives in the shared
  `review.service.clear_decision` domain service, which owns the job existence
  check, the decision-row deletion, the append-only audit event, transaction
  commit, and rollback; the route performs no SQL and declares no request body
  (a sent body is never parsed, stored, or logged, and `clear_decision` always
  writes a `note` of `NULL`). A real clear removes the current `review_decisions`
  row and appends exactly one `review_events` row with `old_decision` set to the
  prior value and `new_decision` `NULL`, committing atomically; afterwards
  `GET /review-decisions/{job_id}` returns the canonical no-current-decision
  `404`, the history endpoint shows the clear as the newest event, and the job
  becomes eligible again under the unreviewed review-queue semantics. Clearing
  an already-unreviewed job is an idempotent no-op that appends no event and
  mutates no row. Every successful `DELETE` on a valid existing job returns
  `200` with the exact `ClearReviewDecisionResponse` field order `job_id`,
  `changed` (`true` when a decision was actually cleared, `false` for the
  idempotent no-op); `204` is never used. An unknown job returns `404` with the
  canonical `No job found with ID {job_id}.` message; non-integer path input
  returns FastAPI's normal `422`. The mutation runs on `writable_connection`
  like the other write endpoints (missing/uninitialized/malformed databases are
  the canonical privacy-safe `500`, lock contention a privacy-safe `503`, and
  the domain service — never the dependency — owns commit/rollback).
- `GET /review-decisions/{job_id}/history` — a job's append-only review-decision
  transition history newest first (`created_at DESC, id DESC`, the shared
  `database.review.list_decision_events` order) with deterministic
  `offset`/`limit` pagination (defaults 0/20, `limit` capped at 100). An
  existing job with no history returns `200` with an empty envelope; an unknown
  job returns the canonical job `404`. Each item uses the shared
  `presenters.review_decision_event_dict` projection with the exact field order
  `event_id`, `job_id`, `old_decision` (`null` for the first assignment),
  `new_decision` (`null` for a clearing transition), `created_at`; event `note`s
  are deliberately omitted. The envelope is `{items, total, offset, limit}`;
  `total` is the event count before `offset`/`limit` slicing.
- `GET /saved-searches` — stored saved-search **definitions** for viewing only
  (execution is a separate computation endpoint; see
  `POST /saved-searches/{name}/execute` below, or `saved-search run` for the CLI
  path). Listing
  is deterministic: ascending case-insensitive `name` then ascending `id` (the
  shared `saved_searches.service.list_searches` order, `name COLLATE NOCASE, id`),
  with standard `offset`/`limit` pagination (defaults 0/20, `limit` capped at
  100) and no filters. The envelope is `{items, total, offset, limit}`; `total`
  is the number of stored saved searches before `offset`/`limit` slicing. Each
  item uses the shared `presenters.saved_search_item_dict` projection with the
  exact field order `id`, `name`, `mode`, `query`, `grouped`, `source`, `remote`
  (`null` = no filter, `true` = remote only), `location`, `untracked`,
  `include_statuses`, `exclude_statuses`, `min_score`, `result_limit`,
  `created_at`, `updated_at`. `query` is intentionally public — it is the
  essential search definition being viewed. Saved-search `profile_path` (a local
  filesystem path) and `description` (unused free text) are deliberately omitted
  from every response and are never read, dereferenced, or loaded by these
  endpoints; no profile loading or filesystem access occurs as a consequence of a
  read. A missing database file returns an empty envelope without creating the
  file.
- `GET /saved-searches/{name}` — one stored saved-search definition using the
  exact same shape as a list item, resolved case-insensitively through the
  shared `saved_searches.service.get_search` (`name COLLATE NOCASE`; the API
  never re-folds case itself). An unknown name returns `404` with the canonical
  `No saved search named '{name}'.` message; a missing database file returns the
  same `404` without creating the file. Viewing only: the request never
  dereferences `profile_path`, never loads a profile, and never executes the
  saved search.
- `POST /saved-searches` — the slice's saved-search-creation mutation
  endpoint. Creates a new **search-mode** saved-search definition through the
  shared `careeros.saved_searches.service.create_search` domain service, which
  owns the insert, the transaction commit, and rollback on any `sqlite3.Error`;
  the route performs no SQL, no commit, and no rollback. The request body is
  `{"name": "x", "query": "python"}` (optionally plus the search-mode filter
  fields) via a dedicated `SavedSearchCreateRequest` model with
  `extra="forbid"`: the accepted fields are `name`, `query`, `mode` (the single
  literal `"search"`, defaulting to it), `grouped` (default `false`), `source`,
  `remote`, `location`, `untracked` (default `false`), `include_statuses`,
  `exclude_statuses`, and `result_limit` (default 20). `mode="rank"` and any
  other mode value are rejected with `422` by the schema, so rank-mode
  authoring is impossible over HTTP and `create_search` is never called;
  unknown body fields — including any `profile_path`, `description`,
  `min_score`, `message`, `note`, `id`, `created_at`, or `updated_at` — are
  likewise rejected with `422` and never persisted. All definition validation
  (name normalization, query validation, source/location validation, status
  validation, include/exclude and untracked conflicts, `result_limit`
  validity) lives in the domain (`validate_fields`), which the schema
  deliberately does not duplicate; the route only forces the safe HTTP
  contract (`mode="search"`, `profile_path=None`, `description=None`,
  `min_score=None`) when building the domain input. A duplicate name is a
  domain validation conflict mapped to `422` with the canonical
  `A saved search named '{name}' already exists.` text — **never `409`** — and
  creation is intentionally non-idempotent, so a repeated identical POST
  creates no second row. Every successful `POST` returns `200` with the
  canonical `SavedSearchItem` projection (identical to the `GET` detail shape,
  exact field order `id`, `name`, `mode`, `query`, `grouped`, `source`,
  `remote`, `location`, `untracked`, `include_statuses`, `exclude_statuses`,
  `min_score`, `result_limit`, `created_at`, `updated_at`) with
  `profile_path`/`description` structurally absent, `mode` `"search"`,
  `min_score` `null`, and `created_at`/`updated_at` following the domain's
  creation semantics. Creation involves no filesystem access (no profile file
  is ever read, and `profile_path` cannot be supplied), no schema change, no
  notification rows, and no external side effects. The mutation runs on
  `writable_connection` like the other write endpoints: the configured database
  must already exist (missing/uninitialized/malformed databases are the
  canonical privacy-safe `500` and nothing is ever created), the per-database
  `collection_lock` is held for the whole request (lock contention maps to a
  privacy-safe `503` with a fixed retry-later message that never exposes lock
  internals), and the domain service — never the dependency — owns
  commit/rollback. The lock is always releasable after 422/500/503 responses.
- `POST /saved-searches/{name}/execute` — execute a stored saved-search
  workflow (search or rank mode) through the shared execution pipeline
  (`execute_search` / `execute_rank`), computation-only with the same guarantees
  as `POST /ranked-jobs`: it never creates a resource, never writes, never reads
  a profile file (no `profile_path` is ever accepted), and never persists or
  logs the submitted profile. The stored definition is authoritative: its
  query, filters, `grouped` flag, rank `min_score`, and `result_limit` cap are
  honored exactly as `saved-search run` does, and only `offset`/`limit`
  pagination is accepted — no query override of the workflow is possible. The
  request body carries the optional `{"profile": {...}}` (same
  `CandidateProfileRequest` semantics as `POST /ranked-jobs`) and is itself
  optional: rank mode requires a non-null inline profile (`422` when absent;
  a body of `{}` or no body at all is equivalent and still rejected for rank
  mode), while search mode rejects a non-null profile (`422`). Unknown body
  fields, including `profile_path` probes, are silently ignored and never cause
  filesystem access; the stored `profile_path` of a rank-mode saved search is
  never read or dereferenced. Review feedback is always excluded
  (`feedback=None`), so stored review decisions never affect scores. The
  envelope is `{items, total, offset, limit}` wrapped with the echoed `mode`
  (`search`/`rank`) and `grouped` flags, so the response is self-describing;
  `total` is the pipeline output length after the stored `result_limit` (and
  stored rank `min_score`) caps and before slicing. `items` are exactly one of
  the four approved shapes per mode/grouping combination: canonical `job_dict`
  projections (search, flat), canonical `group_dict` projections (search,
  grouped), `ranked_job_dict` values (rank, flat), or `ranked_group_dict`
  values (rank, grouped). Deterministic order matches the shared pipeline:
  search order (`published_at DESC NULLS LAST, id ASC`; grouped: ascending
  representative job ID) for search mode, and `match_score DESC`, then newest
  member publication time (`NULLS LAST`), then representative (lowest member)
  job ID for rank mode. Names resolve case-insensitively; an unknown name
  returns `404` with the canonical `No saved search named '{name}'.` message,
  and a missing database returns the same `404` without creating a file,
  directory, or schema. An uninitialized or malformed database returns the
  canonical privacy-safe `500`. Ranking `reasons`, `score_components`, profile
  terms, review decisions, application status, notes, and all other private
  free text are deliberately omitted from every response.
- `DELETE /saved-searches/{name}` — the slice's saved-search-deletion mutation
  endpoint. Deletes one saved-search definition through the shared
  `careeros.saved_searches.service.delete_search` domain service, which owns the
  `DELETE`, the transaction commit, and rollback on any `sqlite3.Error`; the
  route performs no SQL, no commit, and no rollback. The route declares no
  request body and no request schema: a sent HTTP request body is never
  parsed, validated, stored, logged, or used, so saved-search free text (and
  any other sentinel field) can never influence the operation or appear in any
  response. Name lookup is case-insensitive through the shared service (`name
  COLLATE NOCASE`; the API never re-folds case itself), exactly like
  `GET /saved-searches/{name}`; no artificial regex/length/name validation is
  applied to the path parameter. An unknown name returns `404` with the
  canonical `No saved search named '{name}'.` message. Every successful
  `DELETE` returns `200` with the canonical `SavedSearchItem` projection of the
  **deleted** definition (identical to the `GET` detail shape, exact field
  order `id`, `name`, `mode`, `query`, `grouped`, `source`, `remote`,
  `location`, `untracked`, `include_statuses`, `exclude_statuses`,
  `min_score`, `result_limit`, `created_at`, `updated_at`), built from the row
  the domain service returns — the definition is never re-read after deletion,
  and the response preserves the stored/original name casing. `profile_path`
  (a local filesystem path) and `description` (unused free text) remain
  structurally absent. Deletion is genuinely destructive, not an idempotent
  no-op: a repeated `DELETE` of an already-deleted saved search is a canonical
  `404` (the resource no longer exists), and `204` is never used. Deletion
  introduces no schema change and no migration. The mutation runs on
  `writable_connection` like the other write endpoints: the configured database
  must already exist (missing/uninitialized/malformed databases are the
  canonical privacy-safe `500` and nothing is ever created), the per-database
  `collection_lock` is held for the whole request (lock contention maps to a
  privacy-safe `503` with a fixed retry-later message that never exposes lock
  internals), and the domain service — never the dependency — owns
  commit/rollback. The lock is always releasable after 404/500/503 responses.
- `PATCH /saved-searches/{name}` — the slice's saved-search update and rename
  mutation endpoint. Dispatches on the request body exactly: (A) a rename-only
  body (only the replacement `name`, with no definition-update field and no
  non-empty `clears`) is handled by the shared
  `careeros.saved_searches.service.rename_search` domain service, which owns
  the `UPDATE`, the transaction commit, and rollback on any `sqlite3.Error`;
  (B) a definition-update body (one or more of `query`, `grouped`, `source`,
  `remote`, `location`, `untracked`, `include_statuses`, `exclude_statuses`,
  `min_score`, `result_limit`, and/or a non-empty `clears` list, with no
  non-null `name`) is handled by the shared
  `careeros.saved_searches.service.update_search` domain service; (C) a body
  mixing `name` with any definition change or non-empty `clears` is rejected
  with `422`. The route performs no SQL, no commit, and no rollback. The
  request body is via a dedicated `SavedSearchUpdateRequest` model with
  `extra="forbid"`; the accepted fields are `name`, `query`, `grouped`,
  `source`, `remote`, `location`, `untracked`, `include_statuses`,
  `exclude_statuses`, `min_score`, `result_limit`, and `clears` (a list of
  literals restricted to `source`, `location`, `remote`, `min_score`,
  `status_filters` — never `profile_path`/`description`), so unknown body
  fields — including any `mode`, `message`, `note`, `id`, `created_at`,
  `updated_at`, `profile_path`, or `description` — are rejected with `422`.
  Omitted fields and explicit `null` retain stored values; provided non-null
  values update; `[]` empties a stored status list; `false` is a real supplied
  boolean; `clears` omitted/null/empty resets nothing. A field supplied
  non-null while also named in `clears`, or a `status_filters` clear combined
  with `untracked`/`include_statuses`/`exclude_statuses`, is rejected with
  `422`, as are empty/no-op bodies. Name lookup is case-insensitive through
  the shared service (`name COLLATE NOCASE`; the API never re-folds case
  itself), exactly like `GET /saved-searches/{name}`; no artificial
  regex/length/name validation is applied to the path parameter. An unknown
  name returns `404` with the canonical `No saved search named '{name}'.`
  message; a duplicate rename target is a domain validation conflict mapped to
  `422` with the canonical `A saved search named '{name}' already exists.`
  text — **never `409`**. All definition validation (empty/whitespace
  `query`/`source`/`location`, invalid statuses, include/exclude and untracked
  conflicts, `min_score` bounds and rank-only rule, `result_limit` ≥ 1) is
  delegated to the domain. `mode` is immutable and `profile_path`/`description`
  are never accepted, loaded, cleared, or returned; the stored `profile_path`
  of a rank-mode definition is carried forward byte-identically by the domain
  merge and the route never dereferences it. Every successful `PATCH` returns
  `200` with the canonical `SavedSearchItem` projection (identical to the
  `GET` detail shape, exact field order `id`, `name`, `mode`, `query`,
  `grouped`, `source`, `remote`, `location`, `untracked`, `include_statuses`,
  `exclude_statuses`, `min_score`, `result_limit`, `created_at`, `updated_at`),
  built from the row the domain service returns — the definition is never
  re-read after the update. Renaming to the stored name (or a case-only
  variant) and re-PATCHing identical definition values are idempotent domain
  no-ops: no `UPDATE`, `updated_at` preserved, and the response preserves the
  stored/original name casing; a genuine change updates `updated_at`. The
  update introduces no schema change and no migration. The mutation runs on
  `writable_connection` like the other write endpoints: the configured database
  must already exist (missing/uninitialized/malformed databases are the
  canonical privacy-safe `500` and nothing is ever created), the per-database
  `collection_lock` is held for the whole request (lock contention maps to a
  privacy-safe `503` with a fixed retry-later message that never exposes lock
  internals), and the domain service — never the dependency — owns
  commit/rollback. The lock is always releasable after 404/422/500/503
  responses.
- `GET /groups` — distinct opportunities (canonical duplicate groups plus
  ungrouped jobs as singletons), reusing the shared `database.groups`
  `list_all_groups` service with the canonical `presenters.group_dict`
  projection. Deterministic `offset`/`limit` pagination (defaults 0/20,
  `limit` capped at 100). Optional `source`, `remote`, `location`, and
  `employment_type` filters apply to candidate member rows BEFORE grouping,
  matching the grouped-search semantics; unknown `source`/`employment_type`
  values are ordinary equality filters that yield an empty `200`. Ordering is
  deterministic: ascending representative (lowest member) job ID, with each
  group's members ascending by job ID and `sources`/`urls` in ordered-distinct
  form. `total` is the count after filtering/grouping and before
  `offset`/`limit` slicing. The envelope is `{items, total, offset, limit}`;
  each item has the exact field order `group_id` (null for singletons),
  `representative`, `members`, `sources`, `urls`, with the nested jobs using
  the canonical `job_dict` projection. The listing also accepts the three
  select-only review-state filters as `GET /jobs` — `unreviewed` (boolean),
  `include_decisions` (repeatable), and `exclude_decisions` (repeatable) —
  applied AFTER the application-tracking filter and before
  `offset`/`limit` slicing. A group's review decision is resolved with the
  canonical `careeros.review.service.resolve_group_decision` (the group is
  reviewed when any member has a decision; disagreeing members resolve to the
  lowest `jobs.id`, so a decision on a non-representative member marks the
  whole group reviewed). `unreviewed=true` keeps only groups whose resolved
  decision is `None`, `include_decisions` keeps only reviewed groups whose
  resolved decision is in the set (unreviewed groups are excluded while it is
  active), and `exclude_decisions` removes groups whose resolved decision is in
  the set while unreviewed groups remain eligible. Review-state filtering only
  removes whole groups and never re-sorts survivors. Decisions are resolved in
  ONE batched lookup (`careeros.review.service.decisions_for_jobs`) only when a
  review filter is active over the surviving candidate members; no review
  decision, status, note, or history is exposed and the `Group`/`GroupListResponse`
  schema is unchanged. Duplicate decision values are deduplicated (semantic
  union) and omitted/empty lists act as no filter. Every supplied decision is
  validated with the shared `careeros.review.models.validate_decision`
  vocabulary (canonical: `shortlist`, `dismiss`, `review_later`; invalid or
  empty-string values return `422` with the canonical domain text) and the
  pairwise conflicts `include_decisions`+`exclude_decisions` and
  `unreviewed`+either decision list return `422` with the canonical detail
  strings; mixing with `query`, the base filters, and the application-tracking
  filters composes by AND with no cross-dimension conflict. Job descriptions,
  matcher diagnostics
  (`reason`, `remote_sig`, `location_sig`), group `created_at`, and all
  workflow state (notes, decisions, reminders, notification data) are
  deliberately omitted from every response.
- `GET /groups/summary` — aggregate grouping counts reusing the shared
  `database.groups.grouping_summary` service, with no query parameters and no
  pagination. The exact field order is `total_jobs`, `groups`,
  `grouped_jobs`, `distinct_opportunities` (derived as `total_jobs -
  grouped_jobs + groups`). A missing database returns all four values as zero
  without creating a file or directory.
- `GET /groups/{group_id}` — one canonical group using the same item shape as
  a list item, resolved through `database.groups.list_group_infos` (members
  ascending by job ID). `group_id` is a plain string path parameter with no
  artificial regex or length validation. An unknown or arbitrary group ID
  returns `404` with the canonical `No group found with ID {group_id}.`
  message; singleton entries (`group_id` null) are list-only and not
  addressable here. A missing database returns the same canonical `404`
  without creating a file or directory.
- `POST /ranked-jobs` — rank stored jobs against a candidate profile submitted
  inline in the request body (`{"profile": {...}}`). Computation-only: it never
  creates a resource, never writes, never reads a profile file (no
  `profile_path` is ever accepted), and never persists or logs the submitted
  profile. Profile fields mirror the candidate-profile file semantics: all
  optional, unknown fields silently ignored, list entries whitespace-stripped
  (empty-after-strip rejected), up to 100 entries per list and 100 characters
  per entry, `remote_preference` up to 50 characters (empty-after-strip
  collapses to null), duplicates preserved, empty profile valid. Query
  parameters mirror the `/jobs` filters plus `grouped` (default `false`),
  `minimum_score` (0–100, default 0) and `query` (default `""`, which ranks
  every stored job); `offset`/`limit` paginate with defaults 0/20 (`limit`
  capped at 100). The optional body fields `untracked` (`bool | null`),
  `include_statuses` (`list[str] | null`), and `exclude_statuses`
  (`list[str] | null`) mirror the CLI search/rank tracking filters: omitted or
  explicit `null` means no filter, `untracked=true` keeps only untracked jobs,
  `include_statuses`/`exclude_statuses` filter tracked jobs by current status
  (empty lists mean no filter, duplicates are deduplicated by the shared
  domain path), and the tracking filter is applied by the shared
  `rank_candidates` pipeline before scoring in both flat and grouped modes.
  Pairwise conflicts (`untracked` with either status list, and the two status
  lists together) and every supplied status value are validated at the route
  with the canonical shared messages, returning `422`. The optional body fields
  `unreviewed` (`bool | null`), `include_decisions` (`list[str] | null`), and
  `exclude_decisions` (`list[str] | null`) mirror the `GET /jobs`/`GET /groups`
  review-state filters, applied as a route-local post-rank selection hook:
  omitted/explicit-`null`/`false` and empty lists mean no filter;
  `unreviewed=true` keeps only results whose resolved current review decision
  is absent, `include_decisions` keeps only reviewed results whose resolved
  decision is one of the supplied values (unreviewed results are excluded
  while it is active), and `exclude_decisions` drops reviewed results with one
  of the supplied resolved decisions while unreviewed results remain eligible.
  Duplicate decision values are deduplicated (semantic union) and the
  decision vocabulary is validated with the shared `validate_decision`
  (including empty strings). In flat mode the surviving ranked job IDs are
  resolved in ONE batched `decisions_for_jobs` lookup; in grouped mode the
  surviving ranked groups' member IDs are resolved in ONE such lookup and each
  group's canonical decision uses the shared `resolve_group_decision` (a
  decision on ANY member makes the whole group reviewed, and conflicting
  member decisions resolve to the lowest decided `jobs.id`), keeping/dropping
  whole ranked groups without touching members, the representative, scores, or
  the ranked ordering — the review filter never re-sorts. `total` is the
  post-review count (after `minimum_score` filtering) before `offset`/`limit`
  slicing. The three pairwise conflicts
  (`include_decisions`+`exclude_decisions` and `unreviewed`+either decision
  list) return `422` with the canonical `GET /jobs` wording, and these
  review-state validations run after the connection-is-None early return so a
  missing database still yields the `200` empty envelope even with an
  otherwise-invalid review body. Review decisions are FILTER-ONLY: no
  decision, application status, note, or history field is ever added to a
  `RankedJobItem`/`RankedGroupItem`. Review feedback is
  always excluded (`feedback=None`), so
  stored review decisions never affect API scores. With `grouped=false`
  (the default) results are flat raw jobs only; deterministic
  order is `match_score DESC, published_at DESC NULLS LAST, id ASC` and each
  item carries exactly `job` (the canonical `job_dict` projection),
  `match_score`, and `score`. With `grouped=true` results are one entry per
  canonical duplicate group through the shared grouped ranking pipeline
  (`rank_candidates(... grouped=True)` / `score_groups`): the `/jobs` filters
  and `query` apply to candidate member rows BEFORE grouping, deterministic
  order is `match_score DESC`, then newest member publication time
  (`NULLS LAST`), then representative (lowest member) job ID, and each item
  carries exactly `group` (the canonical `group_dict` projection with nested
  `job_dict` members), `match_score`, and `score`. In both modes `reasons`,
  `score_components`, profile terms, review decisions, application status,
  notes, and all other workflow state are deliberately omitted. The envelope is
  `{items, total, offset, limit}`; `total` is the count after `minimum_score`
  filtering and before pagination. A missing database returns `200` with an
  empty envelope without
  creating a file or directory; an uninitialized or malformed database returns
  the canonical privacy-safe `500`.
- `POST /review-queue` — rank the personalized daily review queue (opportunities
  needing review) against a candidate profile submitted inline in the request
  body (`{"profile": {...}}`). Computation-only with the same guarantees as
  `POST /ranked-jobs`: it never creates a resource, never writes, never reads a
  profile file (no `profile_path` is ever accepted), and never persists or logs
  the submitted profile. The request body reuses the `CandidateProfileRequest`
  semantics (all optional, unknown fields ignored, whitespace-stripped list
  entries, up to 100 entries per list and 100 characters per entry,
  `remote_preference` up to 50 characters, duplicates preserved, empty profile
  valid) through a dedicated `ReviewQueueRequest` wrapper. Query parameters
  mirror `review next`: optional non-empty `query` (free-form keyword, default
  `""`; an omitted or empty `query` keeps the no-keyword behavior
  byte-identical) narrows the queue to jobs matching the term across title,
  company, and description through the shared `search_grouped` pipeline on the
  candidate member rows BEFORE grouping, so a matching member pulls in its
  complete canonical duplicate group exactly as `GET /groups?query=...` does
  and a whitespace-only non-empty query follows the same canonical search
  path; `state` (one of `unreviewed`, `shortlist`, `dismiss`,
  `review_later`, `any`; default `unreviewed`), `minimum_score` (0–100, default
  0), `include_tracked` (default `false`), `grouped` (default `true`), the
  `/jobs` filters (`source`, `location`, `remote`, `employment_type`) applied to
  candidate member rows BEFORE grouping, all composing with `query` using AND
  semantics, and `offset`/`limit` pagination
  (defaults 0/20, `limit` capped at 100). Review feedback is always excluded
  (`feedback=None`), so stored review decisions never alter queue scores.
  Results are grouped per canonical duplicate group by default (`grouped=false`
  expands to singleton groups using the `/groups` convention of a null
  `group_id`); deterministic order is `match_score DESC`, then newest member
  publication time (`NULLS LAST`), then representative (lowest member) job ID.
  Each item carries exactly `group` (the canonical `group_dict` projection with
  nested `job_dict` members), `match_score`, `score`, `review_decision`
  (resolved group decision value or null), and `application_status` (tracked
  status or null); `reasons`, `score_components`, profile terms, decision notes,
  application notes, conflict/owner detail, and all other private free text are
  deliberately omitted. The envelope is `{items, total, offset, limit}`;
  `total` is the count after `query`/`state`/application/`minimum_score`
  filtering and
  before pagination. A missing database returns `200` with an empty envelope
  without creating a file or directory; an uninitialized or malformed database
  returns the canonical privacy-safe `500`; an invalid `state` returns `422`.
- `GET /report` — a **profile-less** Career Snapshot dashboard backed by the
  shared `reporting.service.build_report` domain service. It accepts a single
  query parameter, `limit` (default 10, `ge=1`, `le=100`), which caps the
  report's list-oriented sections (recently changed applications, each action
  category, upcoming reminders) exactly as the CLI `report --limit` does. The
  endpoint never accepts a candidate profile, `profile_path`, or profile
  contents, never performs profile scoring, and never applies review feedback.
  It is strictly read-only: it runs on the same `read_only_connection` as the
  other read endpoints, never acquires the collection lock, never writes,
  commits, or rolls back, never initializes the schema, and never touches the
  filesystem or network. The route is orchestration + projection only: it
  calls `build_report(connection, now=..., limit=...)` with no profile/saved
  search and projects the immutable `Report` onto an explicit HTTP-safe
  allow-list of response models (`ReportResponse` and friends in `api/schemas.py`)
  using shared privacy-pruned presenters in `presenters.py` — never by
  serializing the internal `Report` wholesale. The response is stable and
  deterministic except for the server-captured `as_of` timestamp: top-level
  `as_of`, `overview` (the eleven counters, plus `sources` = privacy-pruned
  per-source health, `due_sources`, and `source_stats`), `application_pipeline`
  (`status_counts` zero-filled in canonical `APPLICATION_STATUSES` order and
  `recently_changed` newest first), `action_items` (`interview`, `offer`,
  `interested`, `awaiting_progress`), `reminders` (`overdue_count`,
  `due_today_utc_count`, `open_total`, `upcoming` soonest first), `review_queue`,
  `recommendations`, and `collection_health` (`sources`). Because the endpoint
  is profile-less, the profile-dependent `review_queue` and `recommendations`
  sections stay structurally present with `source` `"none"`, `source_name`
  `null`, empty `items`/`ranked_jobs`/`ranked_groups`, zero `candidate_count`
  and `min_score`, and the domain's canonical explanatory `note` — no profile
  scoring is invented. Privacy/pruning rules are absolute: the HTTP projection
  omits job descriptions, application/review/reminder notes, reminder messages,
  `notification_summary` (worker/operational observability), the provider
  `last_error` text (and any raw/exception/traceback diagnostics), scoring
  `reasons`/`score_components`, saved-search `description`/`profile_path`,
  database/filesystem/lock paths, and PID/command metadata. The per-source
  health projection (`source_status_item_dict`) exposes only the safe
  operational fields `source`, `min_interval_seconds`, `documented`,
  `last_attempt`, `last_succeeded`, `fetched`, `inserted`, `updated`,
  `next_due`, `due`, and `incomplete_runs`. Database semantics match the other
  read endpoints: a missing database file returns `200` with an all-zero/empty
  report (and creates no file, parent directory, or lock file), a
  present-but-uninitialized or malformed database yields the canonical
  privacy-safe `500`, and `sqlite3.Error` is handled by the existing
  application-level handler. The endpoint introduces no schema change and no
  migration (`SCHEMA_VERSION` unchanged).
- `POST /report` — a Career Snapshot dashboard that accepts an optional inline
  candidate profile so a frontend can obtain the complete personalized snapshot
  in one request. It reuses the exact `ReportResponse` allow-list and the same
  shared `reporting.service.build_report` domain service as `GET /report`.
  The request body is optional and carries at most a `profile` field
  (`{"profile": {...}}`) via the same `CandidateProfileRequest` request-model
  semantics as `POST /ranked-jobs` (all fields optional, unknown fields
  silently ignored, list entries whitespace-stripped, duplicates preserved, an
  empty profile valid); unknown body fields — including an attempted
  `profile_path` probe — are ignored and never touch the filesystem. Query
  parameters are `limit` (default 10, `ge=1`, `le=100`, governing the report's
  list-oriented sections exactly as `GET /report`) and `grouped` (default
  `false`, collapsing cross-source duplicates per canonical group in the
  review-queue/recommendation items). With no profile supplied (no body, `{}`,
  or `{"profile": null}`) the response is byte-identical to `GET /report` for
  equivalent `limit`/`grouped` settings. With a profile the endpoint calls
  `build_report(connection, now=..., profile=_to_profile(...), limit=...,
  grouped=..., feedback=None)`: `now` is captured once per request so every
  section observes one timestamp; review feedback is always excluded
  (`feedback=None`), stored review decisions never affect scores; tracked
  applications stay excluded exactly as the report already does; `source` is
  `"profile"` per the report-domain convention with `source_name`/`note`
  `null`; `candidate_count` is populated by the existing reporting logic; and
  `grouped=false` produces flat recommendation/review items while
  `grouped=true` produces one scored entry per canonical duplicate group. The
  submitted inline profile is never persisted, logged, or echoed in the
  response, and the projection follows the same strict privacy/pruning rules
  as `GET /report` (no descriptions, notes, reminder messages,
  `notification_summary`, `last_error`, scoring reasons/components, feedback
  detail, `profile_path`, paths, or lock/PID metadata). The endpoint is
  strictly read-only: it runs on the same `read_only_connection`, never
  acquires the collection lock, never writes/commits/rolls back, never
  initializes the schema, and never touches the filesystem or network. Database
  semantics match `GET /report`: a missing database yields `200` with the
  canonical all-zero/empty report (no file, parent directory, or lock file
  created), and a present-but-uninitialized or malformed database yields the
  canonical privacy-safe `500` via the existing app-level `sqlite3.Error`
  handler. The endpoint introduces no schema change and no migration.

A present-but-uninitialized database yields a `500` with instructions to run a
collection, matching the strict no-DDL policy (no DDL on read paths, and the
write endpoint never initializes the database either).

## Static UI (`/ui`)

`src/careeros/api/static/` holds a zero-build, no-framework local frontend
served by the FastAPI slice at `GET /ui` (`index.html`), `GET /ui/app.js`
(vanilla JavaScript), and `GET /ui/style.css` (plain CSS). The assets are read
from the packaged `careeros.api` resources via `importlib.resources` — never
from `cwd`-relative paths — and are included in installed wheels by the
`[tool.setuptools.package-data]` entry for `"careeros.api"` in
`pyproject.toml`; a wheel built without that entry would silently drop the
static files. `uv build --wheel` then
`unzip -l dist/*.whl | grep 'careeros/api/static'` verifies the packaged HTML,
JavaScript, CSS, and favicon assets.

Architecture boundary: the frontend is **HTTP-only and computation-free**. It
calls the documented endpoints — `GET /report` (Home view), `GET /today`
(Today view), `GET /jobs` (Jobs
view, including the `query` keyword parameter), and the computation-only
ranking endpoints (`POST /ranked-jobs`, `POST /review-queue`, and
`POST /saved-searches/{name}/execute?snapshot=true`) only after an explicit
user action. Matching Preferences exposes all nine CandidateProfile fields;
unsaved drafts stay in browser memory, while explicit save/clear and learned
inspection/control use server-authoritative preference endpoints. List entries
accept
comma- and/or newline-separated values that are trimmed and empty tokens
removed, while preserving order and duplicates; client checks mirror the
server limits (`<=100` entries, `<=100` chars/entry, `<=50` chars for
`remote_preference`) and server validation remains authoritative.
`remote_preference` is intentionally an arbitrary trimmed string (or null),
and `preferred_seniority` accepts arbitrary list terms; neither is narrowed by
the UI. Results render server-returned order with `textContent`/`createElement`
(never unsafe `innerHTML` with API values). It manages only local presentation,
navigation (`#home`/`#today`/`#jobs`/`#review`/`#matching` hash routing;
unknown hashes default to `#home`), loading/error/empty/retry state, and
formatting of already-returned values. It must never re-implement ranking,
grouping, tracking, review-state, reporting, or database logic, and never read
SQLite, config files, candidate profiles, `profile_path`, filesystem paths,
private notes/messages, or logs.
Workflow mutations live in the Job Detail view (see below) — the
Review-decision actions call `PUT /review-decisions/{job_id}` /
`DELETE /review-decisions/{job_id}`, the Application-status actions call
`PUT /applications/{job_id}`, and the Reminders actions call
`POST /reminders` (create), `PUT /reminders/{reminder_id}/complete`,
`PUT /reminders/{reminder_id}/reopen`, and a confirmed
`DELETE /reminders/{reminder_id}` (the Delete control always shows a native
`window.confirm` dialog before removing a reminder; cancelling is a true
no-op, a DELETE 404 is reconciled as success via a reminder-only refresh, and
a Retry re-issues the exact DELETE without re-confirming) — and the Review
view's Review Queue cards (see below), which call
`PUT /review-decisions/{representative job id}` followed, after success, by
the existing authoritative `POST /review-queue` refresh; every other endpoint
call outside Matching Preferences is read-only
(`GET /report`, `GET /jobs`, `GET /jobs/{id}` and its section companions) or
the Review view's computation-only `POST /report`.
Matching Preferences additionally uses `GET /preferences`,
`PUT`/`DELETE /preferences/explicit`, `GET /preferences/learned`,
`PUT /preferences/learned/{key}`, and confirmed
`DELETE /preferences/learned`; successful writes always trigger an
authoritative refetch rather than an optimistic state change.
Job descriptions are not rendered because the public `GET /jobs` projection
intentionally omits them. Navigation uses semantic hash links so deep-link
refresh of `/ui/#jobs` works with no server-side route per view. Browser
acceptance invariants: no unexpected HTTP `5xx` responses, no uncaught
JavaScript exceptions, and no incorrect/failed UI state; the Job Detail
application/review reads use their job-scoped list endpoints
(`GET /applications?job_id={id}`, `GET /review-decisions?job_id={id}`), whose
empty 200 collection is the canonical absent state, so normal browsing issues
NO expected `404`s — the only `404` a Job Detail page can produce is the core
job "not found" state. A basic CSP (`default-src 'self'`) is set in
`index.html` with no inline script/style.

The top-level **Reviewed opportunities workspace** at `#reviewed` is the durable
destination for current decisions. It defaults to Shortlist, with Review later
and Dismissed tabs, authoritative counts, grouped display by default, a
Collapse duplicates toggle, and 20-item server pagination. Cards change or
clear decisions through the canonical `PUT`/`DELETE /review-decisions/{id}`
routes, always targeting the response's concrete `decision_job_id`; the group
representative is never guessed as the owner. No card or count changes
optimistically. Successful mutations refresh only the current
`GET /reviewed-opportunities` region, while mutation retry and refresh-only
retry remain distinct. Per-card operation identities, view/request/data
generations, conditional cleanup, and a dirty refresh coordinator protect
double-click, overlapping mutation, navigation, re-entry, and stale-response
cases. API strings render only through safe DOM creation/text content.
Shortlist is evaluation intent, not an application status.

The **Review Queue card actions** let the Review view's queue cards decide the
opportunity inline. Each card in the review-queue section (never the
recommendations section) renders the three canonical decision controls —
`Shortlist`, `Dismiss`, `Review later` — wired to the SAME canonical endpoint
and semantics as the Job Detail Review section:
`PUT /review-decisions/{id}` with the exact vocabulary values, sent unchanged.
Grouped mode sends the representative job id (the same id the card's
"View details" link targets), which the shared review service resolves to the
whole canonical opportunity; flat mode sends that job's own id. The flow is
strictly server-authoritative: there is NO optimistic removal and NO
client-side ranking or re-ranking — after a successful PUT the frontend
refreshes only the existing `POST /review-queue` endpoint with the captured
profile, grouping mode, and display limit. The refreshed authoritative queue
response alone removes the decided opportunity and updates its count;
Recommendations remain the last authoritative report section and are not
recomputed. Protection is per card: while one card's PUT is in
flight only that card's buttons are disabled (plus `aria-busy` on the card) and
its job id is marked in a `reviewCardInFlight` map whose duplicate activations
are rejected outright; every other queue card stays usable, and no view-wide
queue mutation lock exists. A single queue-refresh coordinator coalesces
overlapping successful decisions and reruns once if another decision lands
while a refresh is active. A captured Review view generation and per-operation
identity tokens invalidate stale work on navigation/re-entry and make marker
cleanup conditional, so an old operation cannot overwrite a new view or clear
its same-job marker. A failed PUT never triggers that refresh: the card stays
visible with its buttons re-enabled and ONE recoverable inline error (the
shared `replaceSectionError` boundary) whose Retry reissues the exact failed
request. If a concurrent refresh already replaced the originating card, that
failure is reported at the durable Review status boundary so it remains
visible; hidden or older Review generations remain untouched. After a
successful PUT, the stale card stays disabled until authoritative replacement
but drops `aria-busy` because the mutation itself has settled. Queue-refresh
failure has its own refresh-only Retry and never repeats a successful PUT. Entering/leaving
`#review` clears the results area together with the per-card markers.

The **Job Detail view** (`#job/<positive-integer-id>`) is the third milestone:
a same-origin detail page opened via the "View details" action on every job
card (Jobs list, Review queue, Recommendations, and grouped member cards; the
"Open original" external link is retained as the second action). It consumes
exactly five read GET route families —
`GET /jobs/{id}`, `GET /jobs/{id}/group`,
`GET /applications?job_id={id}`, `GET /review-decisions?job_id={id}`,
`GET /reminders?job_id={id}` — issued
concurrently through `Promise.allSettled`, with the members of the Opportunity
section coming from the embedded group response (no per-member requests).
The core `GET /jobs/{id}` response is authoritative: a canonical `404` renders
the whole-page "Job not found" state, and a network/`500`/malformed core
failure renders a whole-page error whose Retry reruns the entire load.
Ancillary sections (Opportunity, Application, Review, Reminders) fail
independently with a local error + Retry that refetches only that section; a
one-member singleton group is the normal "no duplicates" state that omits the
Opportunity section entirely (a group failure is a section error, never a
404-tolerated absence). The Application/Review sections render "Not tracked." /
"Not reviewed." on the empty 200 collections from their job-scoped list
endpoints (never a canonical `404`); Reminders render in server order
(`due_at ASC, id ASC`) with Open/Completed derived from `completed_at`. The
Back link uses an in-memory origin remembered when entering from Jobs or
Review (defaulting to Jobs) — no storage APIs, and browser Back/Forward works
naturally because every navigation is a real hash change. Only public fields
are rendered (title, company, location, remote, employment type, source,
published, current application status/decision/reminder kind+due); descriptions,
notes, histories, reminder messages, `source_job_id`, `group_id`, scorer
diagnostics, and all other workflow state are never requested or rendered.

The **Application-status actions** (fourth milestone) let the Job Detail view
start tracking a job or move it through the application lifecycle, reusing the
same mutation infrastructure as the Review-decision actions. The Application
section renders the server-authoritative state: for a tracked job the friendly
`Status:` label plus `First tracked:`/`Last updated:`, and for an untracked
job the canonical "Not tracked." empty state — the six status buttons render in
both cases so a first assignment is always possible. The six canonical values
are sent unchanged (`interested`, `applied`, `interview`, `offer`, `rejected`,
`withdrawn`) with the friendly labels `Interested`, `Applied`, `Interview`,
`Offer`, `Rejected`, `Withdrawn` in exactly that order, mapped by the shared
`applicationStatusLabel` helper; the current status (if any) is highlighted
with `.current` and `aria-pressed="true"`. There is no client-side state
machine: any status may be sent for any current status (including repeating the
current one), because the backend permits any-to-any transitions and repeats
are idempotent no-ops. Each click issues exactly one
`PUT /applications/{job_id}` with body `{"status": "<canonical>"}` and no
optimistic UI; while a mutation is in flight every status button is disabled
(`:disabled` plus `aria-busy` on the section) and a shared
`applicationOperationActive` guard prevents overlapping mutations, reset on
every Job Detail load. A successful `PUT` re-fetches only
`GET /applications?job_id={id}` and re-renders only the Application section
(never the whole view, and never the Opportunity/Review/Reminders sections);
the newly-current status button receives focus. On failure the previously
rendered state is preserved and a single section-local error panel with Retry
replaces any prior one (`replaceSectionError`, one panel per section); Retry
re-issues exactly the failed `PUT`. The empty-list absent state ("Not
tracked.") is canonical and never an error (the singular `GET
/applications/{id}` 404 keeps its authoritative untracked semantics for API
clients, but the view does not call it), while a `PUT` 404 is a genuine
mutation failure routed through the error panel. There is no untrack/reset
control and no `DELETE` call; a tracking filter, `notes`, and all other
workflow state are never exposed.
`tests/test_api_frontend_job_detail.py` covers the view and
`tests/test_api_frontend_job_detail_actions.py` /
`tests/test_api_frontend_job_detail_application_actions.py` /
`tests/test_api_frontend_job_detail_reminder_actions.py` /
`tests/test_api_frontend_job_detail_reminder_delete.py` cover the
Review-decision, Application-status, and Reminder create/complete/reopen/
delete mutations; `tests/test_api_frontend_review_queue_actions.py` covers
the Review Queue card decisions; the shared
forbidden-endpoint invariants in `tests/test_api_frontend.py` and
`tests/test_api_frontend_review.py` allow the five canonical read GET route
families plus the mutation routes (`PUT`/`DELETE /review-decisions/{job_id}`,
`PUT /applications/{job_id}`, `POST /reminders`,
`PUT /reminders/{reminder_id}/complete`, `PUT /reminders/{reminder_id}/reopen`,
`DELETE /reminders/{reminder_id}`) while still forbidding every other
mutation/computation route.

The `/ui` routes take no database dependency: they never acquire the
collection lock, never create/initialize/write the database, and a missing
database never affects them. `tests/test_api_frontend.py` and
`tests/test_api_frontend_review.py` verify static serving, invariant
endpoints, `SCHEMA_VERSION`, the real-default-database rule, the
no-`careeros.cli`-import guarantee, and static-asset hygiene.

## systemd user units

Unit templates ship as `careeros.systemd` package resources, so wheel and
source installations expose the same `careeros systemd install|uninstall`
contract. The top-level `systemd/*.py` files are compatibility wrappers only.
Two independent unit sets exist:

- **`collect`** — the `careeros-collect` oneshot service runs
  `careeros collect due` so CareerOS stays authoritative for source
  eligibility. Defaults: `OnStartupSec=5min`, `OnUnitActiveSec=15min`,
  `RandomizedDelaySec=3min`, `SuccessExitStatus=75`.
- **`notify`** — the `careeros-reminder-notify` oneshot service runs
  `careeros reminder notify-due --backend auto` so due reminders surface as
  desktop pop-ups. Defaults: `OnStartupSec=1min`, `OnUnitActiveSec=5min`,
  `RandomizedDelaySec=1min`, `SuccessExitStatus=75`. It is **independent** of
  the web UI/server: reminders fire whether or not a browser tab or the FastAPI
  server is open, as long as the timer is installed and enabled. Notification
  delivery needs a graphical login session with a reachable session bus (see
  "Reminder notifications"); the notifier keeps retrying failed deliveries, so
  a reminder that comes due while no desktop is available pops up once the next
  run finds one.

`SuccessExitStatus=75` (exit 75 is expected temporary lock contention and is
treated as success; genuine failures still mark the service failed). The
installer renders `ExecStart` with the absolute active Python interpreter and
`-m careeros`, writes only to
`${XDG_CONFIG_HOME:-~/.config}/systemd/user`, and runs `systemctl --user
daemon-reload`. It never assumes a checkout or `uv`, never uses `sudo`, never
touches system-wide units, and never touches the database. Differing existing
unit files abort unless `--force` is passed. `--units
{collect,notify,all}` selects the unit set (default `collect`); `--enable`
starts selected timers. `--dry-run` writes nothing, and `--no-systemctl` is the
isolated-test mode. Uninstall removes only exact generated content. Reinstall
after deliberately replacing/moving the Python environment, using `--force`
when replacing the old generated interpreter path.

Lingering is intentionally never enabled automatically; run
`loginctl enable-linger "$USER"` manually if a timer must run without a login
session.

## Project structure

- `src/careeros/` — package root, entrypoint `careeros.cli:main`
- `src/careeros/api/` — protected local FastAPI application with Product-1 read and workflow-write endpoints
- `src/careeros/api/static/` — zero-build local Product-1 workspace (`/ui`): `index.html`, `app.js`, `style.css`
- `src/careeros/applications/` — application-tracking workflow (models, service)
- `src/careeros/collectors/` — job-board API clients
- `src/careeros/database/` — SQLite layer (schema, connection, persistence)
- `src/careeros/grouping/` — duplicate detection (normalization, matcher)
- `src/careeros/notifications/` — local notification delivery (models, backend, service)
- `src/careeros/presenters.py` — shared presentation helpers (job/group/ranked/coverage dicts, location display)
- `src/careeros/saved_searches/` — saved workflow definitions and shared execution
- `src/careeros/settings.py` — XDG defaults and explicit runtime overrides
- `src/careeros/systemd/` — packaged user-unit templates and installed administration
- `src/careeros/reminders/` — local follow-up reminders (models, service)
- `src/careeros/review/` — daily review queue (models, service, render)
- `src/careeros/source_stats/` — unified per-source statistics and quality diagnostics
- `src/careeros/reporting/` — Career Snapshot dashboard (models, service, renderers)
- `src/careeros/export/` — deterministic read-only exports (models, service, serializers, writer)
- `tests/` — pytest suite, uses `tmp_path` for isolated SQLite databases
- `config/` — YAML config (settings.example.yaml, candidate_profile.example.yaml)
- `data/database/` — legacy checkout-local/development directory, not the runtime default
- `.env` — not loaded by app yet; reserved for future provider keys

## Shared presentation helpers

`src/careeros/presenters.py` is the single home for the display/JSON projections
that the CLI and the text/JSON renderers all share: `job_dict`, `group_dict`,
`ranked_job_dict`, `ranked_group_dict`, `location_display`,
`field_coverage_dict`, and `source_stats_dict`. The reporting, review, and
source-stats renderers plus `cli.py` use these helpers exclusively, so the exact
key order and values of every job/group/ranked/coverage dict live in exactly one
place. Never reimplement these dict builders in a renderer or command; add or
adjust the shared helper instead. The richer export record schemas in
`src/careeros/export/service.py` intentionally do NOT use these helpers — export
records are defined by their own deterministic contract (full stored columns
plus export metadata) and must not be conflated with the display projections.

## Public database row mapper

`src/careeros/database/jobs.py` exposes the canonical public row-mapper API:
`JOB_COLUMNS` (the full column list string used in `SELECT` statements) and
`job_from_row(row)` (builds a `StoredJob` from a `sqlite3.Row`). All modules
that read stored jobs — `database/review.py`, `database/applications.py`,
`database/reminders.py`, `database/groups.py`, `reporting/service.py`, and
`review/feedback.py` — import these public names. The old private
`_SELECT_COLUMNS`/`_stored_job_from_row` names are removed; do not reintroduce
them. Note that `saved_searches/database.py` has its own unrelated
`_SELECT_COLUMNS` for the `saved_searches` table — leave it alone.

## CLI error-message conventions

User-facing errors follow fixed conventions so output is predictable and tests
can assert on stable strings:

- Profile loading goes through the shared `load_profile_or_error(Path)` helper
  in `cli.py`, which always prints exactly `Failed to load profile: {error}` to
  stderr and signals failure by returning `None` (callers return exit code 1).
  Do not inline `load_profile` + its own try/except at new call sites.
- The "no such job" message is produced by `no_job_found_message(job_id)` and
  renders as `No job found with ID {id}.`; every command that reports a missing
  job uses it.
- Failure prefixes are domain-labelled and consistent: `Search failed:`,
  `Show failed:`, `Application failed:`, `Reminder failed:`, `Review failed:`,
  `Report failed:`, `Source stats failed:`, followed by
  `{type(error).__name__}: {error}`.
- Database/setup failures are caught as `(OSError, sqlite3.Error)`. Never use a
  bare `except Exception` to report command failures — narrowing keeps genuine
  programming errors visible.

## Toolchain quirks

- **Bounded runtime dependencies** — PyYAML for profiles; FastAPI/Uvicorn only in the optional `api` extra; collector HTTP remains stdlib-only
- **SQLite only** — schema managed in `schema.py`, not via migration tooling
- **No CI/CD**, no pre-commit hooks, no Makefile
- Product-1 packages are implemented; do not describe `ranking`, `job_hunter`, or the API/static workspace as stubs
- Ruff: `line-length = 88`, selected rules `E F I UP B`
- `mypy` is strict, scoped to `src/careeros` only (not tests)

## Testing quirks

- All DB tests use `tmp_path` for disposable SQLite files (no fixtures, no external DB)
- Collector unit tests pass without network (test `normalize_job()` only, not `fetch_jobs()`)
- Grouping tests use the pure matcher (`assign_groups`) and a seeded `tmp_path` DB (no network)
- Static frontend contracts run in Node harnesses; integrated browser
  qualification uses a temporary deterministic database and real local browser
  without adding a project Playwright dependency

## Today read model

`GET /today` is the bounded read-only projection for the one Workflow /
Attention Center. Home remains the separate profile-less operational/system
overview. Today uses one UTC `as_of` and one SQLite read transaction and returns,
in order: overdue Reminders, overdue Application work, Reminders due later
today, Application work due later today, non-temporal Application attention,
shortlisted opportunities not yet started, and a three-item score-qualified
Review next preview. One concrete Application produces at most one card.

Review next reuses R4 stored explicit and learned preference authority;
`use_learned_preferences` is an invocation-scoped opt-out for only the learned
contribution. Completing a Reminder through its existing endpoint is the only
inline Today write. Detailed Application, Preparation, Package, and Review work
stays in the authoritative workspaces. Today never creates Reminders or
notifications, executes saved searches, escalates source health, uses direct
`FeedbackSet`, reads a private profile path, or exposes private workflow text,
Package/Material details, raw preference evidence, provider errors, or private
scorer diagnostics. It never initializes a missing database or makes model or
network calls.

## Ranking explanation API boundary

Ranked web projections embed a deterministic public `explanation` derived from
the canonical `MatchResult`; no scorer rerun or SQL query is performed. The
projection contains structured matches, gaps, unknowns, other factors, and a
score reconciliation (`contribution_total`, `match_score`, and a canonical
resolution). It never exposes raw reasons, score components, profile paths,
descriptions, notes, matcher diagnostics, or feedback-source provenance.
Explanations are invocation-scoped ranked-result data, not generic job data;
there is no explanation endpoint or persistence. Grouped explanations belong
to the union-scored opportunity and retain the exact response member count.
## R3 M2 private Materials library

Materials were introduced in schema v11; the current `SCHEMA_VERSION` is 13.
They are global reusable private records with
immutable versions; they are not owned by jobs or applications. Managed bytes
live outside the checkout under an XDG-configured root, with a DB/root marker
pair, mode 0700 directories, mode 0600 files, no arbitrary durable paths, and
no destructive deletion or history erasure. Browser uploads are raw bounded
`application/octet-stream` streams; multipart, parsing, OCR, indexing,
preview, cloud storage, and AI generation are out of scope.

Material writes require an explicit current-schema database and enforce the
managed-root A–G binding state machine; the first successful import may safely
create and bind an absent/empty root. Startup and read-only commands never
migrate or initialize databases or create roots. Scheduled `collect due`
retains its existing fresh-database bootstrap behavior, but never migrates or
changes an existing old-schema database. Use temporary databases and roots for
development and tests. SQLite backup/restore/migration operations contain
database pages only; managed material bytes and their root must be preserved
separately as a matching pair. Production migration remains an explicit
operator decision.

Material commands are available as `careeros material list`, `show`, `import`,
`add-version`, `update`, `retire`, `restore`, `export-version`, and `audit`.
The audit is read-only, certifies only a stable snapshot, and returns 0 for a
coherent store, 1 for stable inconsistency, and 75 for contention/repeated
instability. Private names, hashes, storage keys, local paths, and content do
not appear in ordinary diagnostics or URLs.

## R3 M3 job-specific Application Packages

Schema v11 adds one logical Application Package per concrete tracked
`applications.id`, with at most one mutable draft and at most 100 retained
versions. Drafts snapshot the semantic job tuple and current M1 requirements;
finalized versions preserve immutable requirements, exact Material-version
manifests, confirmed answers, approval, and ordered completeness reasons.
Package state never references duplicate groups and never mutates M1
acknowledgements, Application status/events, Review, reminders, jobs, or
Materials.

Package and Preparation revisions are independent CAS authorities. Rebase is
explicit and uses the frozen compatibility matrix; semantic job changes require
the user to clear and renew M1 review/ready acknowledgement after the captured
reconfirmation time. Approval binds to the exact current draft revision without
incrementing it. Finalization hashes each distinct selected exact Material once
before acquiring the collection lock, retains safe descriptors/root identity,
then uses `BEGIN IMMEDIATE` and a complete server-side reread before atomically
freezing the same version row. A retry against the frozen version and revisions
is idempotent and does not repeat preflight mutation hooks.

The package HTTP surface is Job Detail-centric. Summary, history, and Pipeline
are answer-free; only explicit version detail returns answers. Pipeline exposes
only `has_draft`, approval-for-current-revision, finalized count, and latest
finalization time—it never claims byte-verified completeness. There is no M3
CLI, generic export, filesystem package store, Material deletion, submission,
outreach, or frontend implementation in the backend work package.

## Product 1 v1.05 M7 Career Profile workspace

The private loopback `#career-profile` workspace exposes closed M3 extraction,
closed deterministic M5 Career Facts, and closed M6 review/immutable Career
Profile authority. Its panels are Career Facts, Current Profile, and Profile
History. Analyze always names one résumé Material ID and immutable version ID;
opening the workspace never analyzes or chooses a current résumé implicitly.
Candidate evidence is independently paginated exact M3 UTF-8 evidence with
verified byte ranges/hashes and bounded friendly PDF/DOCX labels. Review notes,
normalized keys, storage keys, paths, surrounding résumé context, and complete
raw extracted text are never exposed.

The Career Profile is distinct from the file-backed ranking Matching Profile
(`careeros profile`) and stored Matching Preferences (`careeros preference`).
Review and complete-snapshot save operations call M6 under the shared writer
lock and never mutate either matching authority, ranking, feedback, or adaptive
state. Profile saves use the observed revision as CAS, never auto-merge or
silently replay stale drafts, and may deliberately create empty or semantic
no-op immutable versions. The browser draft is memory-only. The frozen product
and privacy contract is `docs/m7-career-profile-workspace-contract.md`.
