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
`collect history`), plus `search`, `show`, `rank`, `group`, and `diagnose`.
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
