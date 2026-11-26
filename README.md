# Career WatchPost

Career WatchPost is a privacy-first, local-first career management application
combining automated job discovery, explainable job matching, adaptive
recommendations, application tracking, and local résumé intelligence.

Built with Python, FastAPI and SQLite, it uses deterministic algorithms, modular
collection adapters and human-reviewed career profiles to provide a transparent
and privacy-conscious career management workflow.

## Capabilities

- Collects from seven built-in public sources with bounded, rate-aware polling;
  optional Lever, Greenhouse, Workable, and SmartRecruiters integrations are
  credential-free and explicitly configured in private state.
- Searches and groups duplicate listings, ranks jobs with deterministic,
  explainable score components, and maintains a materialized Review queue.
- Tracks applications, local reminders, saved searches, review decisions, and
  deterministic learned Matching Preferences in SQLite.
- Provides a protected loopback web workspace and CLI for jobs, Today, Review,
  workflow, exports, diagnostics, and Career Profile review.
- Extracts supported résumé text locally, sanitizes personal information,
  derives Career Facts, and stores human-reviewed immutable Career Profiles.

## Application preview

The [portfolio feature tour](docs/portfolio-demo.md) explains the five
fictional-data captures and links to each image. The captures are reviewed
artifacts, not a fixture generator: a fresh checkout does not recreate them.

![Today workspace](docs/assets/portfolio-screenshots/01-today-workflow.png)

## Quick start

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required. This is a
source-only publication; no PyPI package or wheel/sdist release artifact is
claimed here.

```bash
git clone https://github.com/BillPetrak/career-watchpost.git
cd career-watchpost
git checkout v1.2.2
uv sync --extra development --extra api
```

For a temporary learning session, first read the
[provider-use boundaries](docs/reference/job-sources.md#provider-rights).
The following Bash example contacts Arbeitnow's live feed: run it only after
confirming permission for your intended use. It does not reproduce the fictional
screenshot tour.

```bash
(
  set -e
  umask 077
  DEMO_DIR="$(mktemp -d /tmp/careeros-demo.XXXXXX)"
  chmod 700 "$DEMO_DIR"
  unset CAREEROS_DATABASE CAREEROS_MATERIALS_ROOT CAREEROS_SOURCES_CONFIG
  export XDG_DATA_HOME="$DEMO_DIR/data" XDG_CONFIG_HOME="$DEMO_DIR/config"
  export CAREEROS_DATABASE="$XDG_DATA_HOME/careeros/careeros.sqlite3"
  export CAREEROS_MATERIALS_ROOT="$XDG_DATA_HOME/careeros/materials"
  printf 'Temporary state: %s\n' "$DEMO_DIR"
  uv run --offline --no-sync careeros profile init
  uv run --offline --no-sync careeros collect arbeitnow --limit 10
  uv run --offline --no-sync careeros serve
)
```

Open `http://127.0.0.1:8000/ui`; stop the server with Ctrl-C. The subshell
scopes the environment overrides; its fresh XDG configuration also isolates the
Matching Profile and leaves optional sources unconfigured. `uv --offline`
prevents dependency downloads, not the application's live collection request.
No automatic cleanup is performed. Temporary storage may be cleared by the OS;
use the [persistent installation workflow](docs/user-guide.md#normal-persistent-local-use)
to retain career data between sessions. The [user guide](docs/user-guide.md)
also covers the longer isolated walkthrough, migration, and recovery.

## Architecture

The core is modular Python and SQLite, with bounded collector adapters, a
FastAPI loopback interface, deterministic grouping and ranking, explicit writer
locks, and immutable résumé provenance. The architecture diagram is
[available as SVG](docs/assets/career-watchpost-architecture.svg).

Important boundaries include fail-closed private-file handling, bounded input
and provider responses, no automatic application submission, and no external
résumé model or telemetry call. Career Profile values do not automatically
populate the ranking Matching Profile; these are separate authorities.

## Documentation

- [Documentation index](docs/README.md)
- [User guide](docs/user-guide.md)
- [CLI reference](docs/reference/cli-reference.md) and [API reference](docs/reference/api-reference.md)
- [Job sources and provider boundaries](docs/reference/job-sources.md)
- [Operations and recovery](docs/reference/operations-and-recovery.md)
- [Architecture notes](docs/architecture/)
- [Testing and quality](docs/testing-and-quality.md)
- [Changelog](CHANGELOG.md)
- [Contributor guide](CONTRIBUTING.md)

## Testing and quality

Run the documented checks with disposable state:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

The accepted v1.2.1 regression baseline passed **4,208 tests**; v1.2.2 focused
qualification passed **567 tests**. A separate public-history qualification
passed 142 tests. These historical campaigns are not additive and were not
rerun for this documentation refinement. Scope and provenance are recorded in
[the changelog](CHANGELOG.md) and [testing reference](docs/testing-and-quality.md).

## Privacy, limitations, and rights

Private databases, Materials, profiles, exports, logs, and source
configuration belong outside the checkout. Public provider endpoints do not
grant rights to redistribute job descriptions, payloads, logos, or employer
content. Career WatchPost never submits applications or contacts employers.

Supported résumé inputs are text-based PDF and ordinary DOCX. OCR/image-only
PDF, legacy DOC, and arbitrary PDF column reconstruction are unsupported.
Personal-information detection is deterministic and bounded, not a claim of
comprehensive PII detection or anonymisation.

## License and compatibility

The repository is released under the [MIT License](LICENSE). The public
identity transition intentionally preserves `careeros` as the Python
distribution/import namespace and CLI name, and in storage paths, environment
variables, API headers, and systemd units. XDG private-state conventions remain
stable. The public Git history preserves
the genuine development progression with privacy and presentation
sanitization; it does not claim new functionality for the presentation
release.
