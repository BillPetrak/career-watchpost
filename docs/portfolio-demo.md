# Career WatchPost fictional portfolio demo

This walkthrough is designed for screenshots and review using only disposable
schema-14 state. It never reads the owner database, Materials, profile,
configuration, browser state, or live provider feeds.

## Disposable setup

```bash
DEMO_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/career-watchpost-demo.XXXXXX")"
DEMO_DB="$DEMO_ROOT/careeros.sqlite3"
DEMO_MATERIALS="$DEMO_ROOT/materials"
DEMO_CONFIG="$DEMO_ROOT/config"
DEMO_CACHE="$DEMO_ROOT/cache"
mkdir -m 700 "$DEMO_MATERIALS" "$DEMO_CONFIG" "$DEMO_CACHE"
export CAREEROS_DATABASE="$DEMO_DB"
export CAREEROS_MATERIALS_ROOT="$DEMO_MATERIALS"
export XDG_DATA_HOME="$DEMO_ROOT/data"
export XDG_CONFIG_HOME="$DEMO_CONFIG"
export XDG_CACHE_HOME="$DEMO_CACHE"
unset CAREEROS_SOURCES_CONFIG
```

This walkthrough does not provide a reusable seeding command. Career WatchPost
has no product interface for inventing job listings, and `careeros collect`
accesses live public providers rather than generating fictional data. The
portfolio captures therefore use a separately reviewed, temporary qualification
fixture outside the product. That private fixture procedure is retained with
qualification evidence; it is not a released feature.

For a new capture campaign, prepare an equivalent disposable fixture through a
separately reviewed test harness using reserved names and URLs under
`https://example.com/`. Do not import a real résumé, call a provider, set live
credentials, or fall back to a production database. Without such a fixture,
this document is a walkthrough of the committed captures, not a reproducible
seeding procedure.

## Suggested story

1. Initialize a disposable fictional matching profile with
   `careeros profile init "$DEMO_CONFIG/candidate_profile.yaml"`.
2. Start from the separately reviewed fictional fixture; do not run a collector
   as part of this portfolio walkthrough.
3. Open the loopback UI and show Today / Workflow and Attention Center.
4. Show Review with deterministic score components and a human decision.
5. Track an application and add a local follow-up reminder.
6. Inspect Matching Preferences after synthetic review evidence.
7. Review fictional Career Fact candidates and provenance, if synthetic
   Material input is available.
8. Export a bounded review result to a temporary path under `DEMO_ROOT`.

The five portfolio captures are:

- `docs/assets/portfolio-screenshots/01-today-workflow.png`
- `docs/assets/portfolio-screenshots/02-review-scoring.png`
- `docs/assets/portfolio-screenshots/03-applications-reminders.png`
- `docs/assets/portfolio-screenshots/04-matching-preferences.png`
- `docs/assets/portfolio-screenshots/05-career-profile-provenance.png`

They were captured from the actual loopback UI with a temporary qualification
fixture that used seven fictional jobs, reserved `jobs.example.com` URLs, a
generated fictional text-based PDF résumé, and disposable database, Materials,
HOME, XDG, cache, runtime, and browser-profile paths. The browser made no live
provider request. Each image was visually reviewed and checked for embedded
local-path or owner identifiers before inclusion.

## Truthful demo boundary

The demo shows stored fictional jobs, deterministic matching, review, workflow
tracking, reminders, exports, and Career Profile provenance. It does not show a
live collection run, provider authorization, application submission, LLM
extraction, OCR, arbitrary PDF column reconstruction, hosted deployment, or
import of private profile data. Career WatchPost remains local-first and
model-free for these workflows.
