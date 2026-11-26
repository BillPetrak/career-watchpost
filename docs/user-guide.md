# User guide

Career WatchPost is a local application. It does not submit applications,
send email, contact employers, or upload résumé data. The commands and paths
use the stable `careeros` namespace.

## Requirements and installation

Install Python 3.11 or newer and `uv`, then use a public HTTPS checkout:

```bash
git clone https://github.com/BillPetrak/career-watchpost.git
cd career-watchpost
git checkout v1.2.2
uv sync --extra development --extra api
```

The repository is source-only. Development dependencies add pytest, Ruff,
mypy, and HTTP test helpers; the API extra adds FastAPI and Uvicorn.

## Temporary learning session

The examples through “Ending the temporary session” share one private Bash
subshell. Start it from the installed checkout, then run the setup inside it:

```bash
bash --noprofile --norc
```

```bash
umask 077
DEMO_DIR="$(mktemp -d /tmp/careeros-demo.XXXXXX)" || exit
chmod 700 "$DEMO_DIR" || exit
unset CAREEROS_DATABASE CAREEROS_MATERIALS_ROOT CAREEROS_SOURCES_CONFIG
export XDG_DATA_HOME="$DEMO_DIR/data" XDG_CONFIG_HOME="$DEMO_DIR/config"
export CAREEROS_DATABASE="$XDG_DATA_HOME/careeros/careeros.sqlite3"
export CAREEROS_MATERIALS_ROOT="$XDG_DATA_HOME/careeros/materials"
DEMO_PROFILE="$XDG_CONFIG_HOME/careeros/candidate_profile.yaml"
printf 'Temporary state: %s\n' "$DEMO_DIR"
```

This overrides inherited database and Materials selectors, unsets inherited
optional-source selection, and replaces both XDG bases. The fresh default
`sources.yaml` is absent, so no optional source is enabled. The Matching Profile
also lives inside this new directory. Stay in this shell for every demonstration
command below; do not paste individual commands into an unprepared shell.
`--offline --no-sync` prevents uv dependency downloads or environment updates;
it does not prevent application network requests.

## Configuration

Create and validate a candidate profile before ranking:

```bash
uv run --offline --no-sync careeros profile init "$DEMO_PROFILE"
uv run --offline --no-sync careeros profile validate "$DEMO_PROFILE"
```

The optional `sources.yaml` enables explicitly selected Lever, Greenhouse,
Workable, or SmartRecruiters public feeds. It must be a user-owned regular
`0600` file. Provider policy and polling limits are in the [sources reference](reference/job-sources.md).

[`.env.example`](../.env.example) documents supported environment selectors,
all commented out. The application does not load `.env` files: deliberately
export variables or supply them through the execution environment. The
candidate-profile JSON and YAML examples in `config/` are fictional reference
fixtures. File-backed profiles require an owned regular `0600` file with one
hard link; use `profile init` rather than passing a public fixture directly.
Edit only the temporary profile here, using fictional preferences.

## Discovery, search, and Review

First read the [provider-use boundaries](reference/job-sources.md#provider-rights).
Only run the following live Arbeitnow request after confirming authority for
your intended use. It initializes the new schema-14 database; it does not
recreate the [fictional screenshot tour](portfolio-demo.md). `collect all`,
`collect due`, and collection timers may attempt every registered source;
avoid them where any source's collection authority is unresolved.

```bash
uv run --offline --no-sync careeros collect arbeitnow --limit 10
uv run --offline --no-sync careeros collect status
uv run --offline --no-sync careeros collect history --limit 20
```

Search and rank are read-only. `--grouped` collapses conservative cross-source
duplicates; tracking filters operate on applications anchored to concrete job
IDs. Ranking uses the file-backed Matching Profile plus explicit and learned
Matching Preferences. Career Profile snapshots are not silently copied into
that ranking profile.

```bash
uv run --offline --no-sync careeros search engineer --grouped --untracked
uv run --offline --no-sync careeros rank engineer --profile "$DEMO_PROFILE" --format json
uv run --offline --no-sync careeros review next --profile "$DEMO_PROFILE"
```

Review decisions, feedback, and preferences are local workflow data. Learned
preferences require repeated deterministic evidence, can be suppressed or
reset, and can be disabled with `--no-adaptive`; `--no-feedback` disables the
separate direct review-feedback adjustment.

## Applications and reminders

Tracking is local only. Replace `JOB_ID` with a stored job ID returned by search
and `DUE_AT` with your chosen full ISO-8601 timestamp including an offset
(for example, `2026-12-01T10:00:00+00:00` is illustrative, not a deadline):

```bash
uv run --offline --no-sync careeros application set JOB_ID interested
uv run --offline --no-sync careeros reminder add JOB_ID follow_up DUE_AT --message "Check status"
uv run --offline --no-sync careeros reminder list --open
```

Reminder timestamps require an explicit offset. Writes use a per-database
advisory lock; contention exits 75. Application and reminder history is
append-only where specified and remains anchored to concrete job IDs, so
regrouping cannot orphan it.

## Materials and Career Profiles

For this temporary walkthrough, use a fictional résumé you supply. Replace
`FICTIONAL_RESUME.pdf` with its actual path and each uppercase ID with the exact
ID reported by the preceding import, sources, analysis, or candidates command.
Import, audit, and analyze all use the same isolated database and Materials root:

```bash
uv run --offline --no-sync careeros material import FICTIONAL_RESUME.pdf --name resume --category resume
uv run --offline --no-sync careeros material audit
uv run --offline --no-sync careeros career-profile sources
uv run --offline --no-sync careeros career-profile analyze MATERIAL_ID VERSION_ID
uv run --offline --no-sync careeros career-profile candidates FACT_RUN_ID
uv run --offline --no-sync careeros career-profile review CANDIDATE_ID approved
uv run --offline --no-sync careeros career-profile show
```

Extraction is bounded and fail-closed. Evidence coordinates and extraction
identity are retained; ambiguous content is omitted for human review. Career
Facts require deliberate review before inclusion in a saved profile. Approving
candidates does not create a Career Profile version: `career-profile show`
only reads the last saved snapshot, which may not exist yet. Complete the
[deliberate web save](#saving-a-career-profile) below. Saving a complete profile
snapshot uses optimistic revision checks and immutable history.

## Web workspace and ending the temporary session

After successful database initialization, start the server in this same shell:

```bash
uv run --offline --no-sync careeros serve
```

Open `http://127.0.0.1:8000/ui`. The server validates the selected database and
matching Materials root; it refuses missing, uninitialized, old, malformed, or
future-schema databases. Read-only commands do not create absent state.

### A practical web workflow

Use the navigation labels below while the server is running against this same
isolated state:

- **Today:** start with overdue reminders and application work, then inspect
  shortlisted opportunities and the bounded Review next preview.
- **Jobs:** search stored listings and open a job to inspect its details and
  take local workflow actions. Collection remains a separate CLI operation.
- **Matching Preferences:** edit your ranking preferences, preview their effect,
  and deliberately save them with “Save draft as stored preferences.” Inspect,
  suppress, restore, or reset learned evidence as needed.
- **Review:** choose duplicate collapsing and learned-preference options, then
  press “Generate review.” Inspect score explanations before choosing Shortlist,
  Dismiss, or Review later; changing preferences does not rerun an existing view.
- **Reviewed:** revisit prior decisions in the “Reviewed opportunities” workspace.
  Change or clear decisions; shortlisting does not create an application.
- **Applications:** inspect active/completed applications and update the status
  of a concrete job independently of Review decisions.
- **Reminders:** inspect open reminders grouped by urgency. This overview is
  read-only; open the associated job to add, edit, complete, reopen, or delete
  reminders.
- **Materials:** import fictional résumé files for this walkthrough, inspect
  immutable versions, and manage library metadata and retirement. Downloads
  create unmanaged copies outside the library.
- **Career Profile:** analyze an exact résumé version, inspect candidate evidence,
  approve/correct/reject facts, and deliberately assemble a saved profile.

### Saving a Career Profile

After the CLI analysis/review above, or equivalent work in the web interface:

1. Open **Career Profile → Career Facts**, find the résumé version and choose
   “Review facts.” Use “Inspect evidence and review history” to check candidates
   and review any that still need approval or correction.
2. In the candidate list, choose “Add to assembly” for each approved or
   approved-edited fact you intend to include.
3. Open **Current Profile → Open memory-only assembly**. Review the complete
   selection, including retained facts from an existing profile, ordering,
   source choices, and privacy classifications.
4. Press **Save Profile** and wait for “Profile version saved.” Confirm the
   result under **Current Profile** and **Profile History**. The CLI
   `career-profile show` now reads that saved version too.

The assembly exists only in browser memory until saved. If another write causes
a revision conflict, review the refreshed state and explicitly reapply/reconcile
the preserved draft before saving again. Saving a Career Profile never populates
the separate ranking Matching Profile or Matching Preferences.

### Ending the temporary session

After saving any intended changes, stop the server with Ctrl-C, then leave the
temporary shell:

```bash
exit
```

Your parent shell's environment is unchanged. The printed temporary directory
is not automatically deleted, but the OS may clear it. Keep its path if you
want to inspect it again; a new `mktemp` session starts with fresh state.
Do not use this temporary workflow for career data you need to retain.

## Normal persistent local use

For ongoing use, run from your normal shell after leaving the demonstration.
Inspect your environment selectors deliberately: the parent shell may already
select an existing workspace. With no explicit overrides, state is XDG-based
and persists independently of the checkout, virtual environment, and session:

| State | Default | Override |
| --- | --- | --- |
| SQLite database | `${XDG_DATA_HOME:-~/.local/share}/careeros/careeros.sqlite3` | `--database`, then `CAREEROS_DATABASE` |
| Managed Materials | `${XDG_DATA_HOME:-~/.local/share}/careeros/materials` | `--materials-root`, then `CAREEROS_MATERIALS_ROOT` where supported |
| Matching Profile | `${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml` | explicit profile path; no dedicated environment selector |
| Optional sources | `${XDG_CONFIG_HOME:-~/.config}/careeros/sources.yaml` | `--sources-config`, then `CAREEROS_SOURCES_CONFIG` |

Use the same selected database, Materials root, and configuration on later runs.
Keep private files outside the checkout and preserve their owner-only modes.
For a new persistent workspace, after checking these selectors and the
[provider conditions](reference/job-sources.md#provider-rights):

```bash
uv run --offline --no-sync careeros profile init
uv run --offline --no-sync careeros collect arbeitnow --limit 10
uv run --offline --no-sync careeros serve
```

This writes persistent state and the collection contacts the live feed.
`profile init` refuses to overwrite an existing profile; existing users should
validate their profile instead. Existing old databases need explicit migration
before use. Back up SQLite, matching Materials, and private configuration as a
set; reinstalling the application is not a backup and does not relocate them.

## Troubleshooting and safe recovery

Run `uv run --offline --no-sync careeros diagnose` against your intended
environment first. The paths below are placeholders for that workspace and a
private backup destination. For schema upgrades, stop writers and
back up before the explicit migration:

```bash
uv run careeros database backup /safe/path/careeros.sqlite3.backup --database /path/to/careeros.sqlite3
uv run careeros database migrate --database /path/to/careeros.sqlite3
```

SQLite backups do not contain managed Material bytes. Complete private recovery
requires the matching database, Materials root, and applicable profile/source
configuration. Use `database restore ... --confirm-replace` only after
validating the backup and stopping writers. See [operations and recovery](reference/operations-and-recovery.md).

For exact flags, status codes, routes, and provider limits, use the [CLI](reference/cli-reference.md), [API](reference/api-reference.md), and [job sources](reference/job-sources.md) references.
