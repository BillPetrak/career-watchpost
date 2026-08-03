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
`saved-search`, `application`, a read-only `report` (Career Snapshot), and
`diagnose`.
Collection commands are mutually exclusive per database via an OS advisory
lock, while the read-only commands remain available during collection; a lock
conflict exits with code 75. See `AGENTS.md` for the full command reference.

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
carry identical data. The text output has five sections:

- **Overview** — stored jobs, canonical groups, distinct opportunities,
  tracked applications, saved searches, per-source last-run summary, and which
  sources are currently due.
- **Application pipeline** — one count per status (including `rejected` and
  `withdrawn`) plus the most recently changed applications (newest first,
  capped by `--limit`).
- **Action items** — the pipeline items worth acting on, in prominence order:
  interview attention, offer attention, interested (not applied), and applied
  (awaiting progress). `rejected`/`withdrawn` appear only in the pipeline
  counts, never as actions. CareerOS never invents deadlines or follow-up
  dates from text.
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
