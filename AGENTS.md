# Career WatchPost repository instructions

Career WatchPost is the public product name; `careeros` remains the stable
Python/package and CLI namespace. This repository is a local-first job search,
matching, review, application and reminder workspace. Do not introduce
private owner data, provider payloads, credentials, private paths, or research
artifacts.

## Authority and architecture

Read the relevant public contract before changing a subsystem:

- [`docs/architecture/resume-extraction.md`](docs/architecture/resume-extraction.md)
- [`docs/architecture/privacy-and-pii-sanitization.md`](docs/architecture/privacy-and-pii-sanitization.md)
- [`docs/architecture/career-fact-extraction.md`](docs/architecture/career-fact-extraction.md)
- [`docs/architecture/career-profile.md`](docs/architecture/career-profile.md)
- [`docs/architecture/career-profile-workspace.md`](docs/architecture/career-profile-workspace.md)
- [`docs/architecture/adaptive-recommendations.md`](docs/architecture/adaptive-recommendations.md)
- [`docs/architecture/workflow-and-attention-center.md`](docs/architecture/workflow-and-attention-center.md)
- [`docs/architecture/today-materialized-ranking.md`](docs/architecture/today-materialized-ranking.md)
- [`docs/architecture/ranking-and-duplicate-grouping.md`](docs/architecture/ranking-and-duplicate-grouping.md)
- [`docs/reference/cli-reference.md`](docs/reference/cli-reference.md) and
  [`docs/reference/api-reference.md`](docs/reference/api-reference.md), when present.

Career Profile is immutable, human-reviewed authority. It is separate from
the file-backed ranking `CandidateProfile` (Matching Profile) and never
automatically populates Matching Profile or Matching Preferences. Ranking and
Today are deterministic and server-authoritative; derived grouping and ranking
must not mutate source job/application/reminder rows.

Schema 14 is current. Explicit migration accepts structurally valid schemas
8–13 and applies each ordered step through 14; v1.2.0 introduced the 13 → 14
step. Stop writers and retain the documented backup. Unsupported, malformed,
and future schemas fail closed; there is no down migration. A SQLite backup excludes managed Materials and private
configuration/profile state; complete recovery needs all applicable authorities.

Private defaults are independent of the checkout: database and Materials use
`${XDG_DATA_HOME:-~/.local/share}/careeros`; the private matching profile uses
`${XDG_CONFIG_HOME:-~/.config}/careeros/candidate_profile.yaml`. Explicit CLI
paths take precedence, then `CAREEROS_DATABASE`/`CAREEROS_MATERIALS_ROOT`,
then XDG defaults. Read-only commands must not create absent persistent state.

The HTTP application is loopback-only. Preserve strict host/origin and
forwarding-header protections, write-intent checks, safe error responses,
no-store policy, and request bounds. Never test against a production database.
Collection and other writers coordinate through the per-database lock.

## Development

```bash
uv sync --extra development
uv sync --extra development --extra api
uv run careeros diagnose
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

Use temporary databases/material roots for tests and privacy-safe fictional
fixtures. Run focused tests first, then the proportionate regression suite.
Keep changes scoped, deterministic, and server-authoritative. Do not add
dependencies or alter schema/API contracts without explicit approval.

## Agent workflow

The six repository skills retain stable identifiers:
`careeros-team`, `careeros-planner`, `careeros-builder`, `careeros-reviewer`,
`careeros-test-review`, and `careeros-integration`. They describe a useful
planning → implementation → independent correctness/QA review → integration
workflow; use only the roles available in the execution environment. Optional
externally installed skills/tools are not bundled dependencies. Missing tools
do not authorize downloads or network access without approval.

Use the smallest non-overlapping work package. Preserve reviewer context
separation, avoid unnecessary parallelism, and report evidence rather than
assuming autonomous correctness. Planning is read-only; reviewers initially
edit nothing. Commit and push always require explicit owner approval.

## Scope and safety

Do not modify production installations, owner state, credentials, historical
Git objects, tags, remotes, or unrelated files. Do not stage, commit, push,
reset, clean, or rewrite history as part of ordinary work. Preserve MIT
license/attribution and existing assets. When a requirement conflicts with
current behavior, inspect source and focused tests, document the evidence, and
ask before broadening scope.
