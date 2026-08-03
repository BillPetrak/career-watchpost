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
| `uv run careeros collect all --limit 25` | Collect from all sources sequentially |
| `uv run careeros collect all --limit 25 --database /tmp/careeros_all.db` | Collect all sources into a custom DB |
| `uv run careeros collect due --limit 25` | Collect only sources whose polling interval has expired |
| `uv run careeros collect status [--database PATH]` | Show per-source polling status |
| `uv run careeros collect history [--source NAME] [--limit N] [--failed] [--database PATH]` | Show recent collection run history |
| `uv run careeros search engineer --limit 10` | Search stored jobs |
| `uv run careeros search engineer --remote --location Berlin` | Search with filters |
| `uv run careeros rank engineer --profile config/candidate_profile.example.json` | Rank stored jobs against profile |

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
arbeitnow/remoteok/weworkremotely: provisional defaults). Manual
`collect <source>` and `collect all` bypass scheduling, but every collection
attempt is recorded in the `collection_runs` table. `collect due` exits 0 when
nothing is due or all due sources succeed, and 1 when any due source fails or
setup fails.

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

## Project structure

- `src/careeros/` — package root, entrypoint `careeros.cli:main`
- `src/careeros/collectors/` — job-board API clients
- `src/careeros/database/` — SQLite layer (schema, connection, persistence)
- `src/careeros/{job_hunter,privacy,ranking,reporting}/` — stubs (empty, not implemented)
- `tests/` — pytest suite, uses `tmp_path` for isolated SQLite databases
- `config/` — YAML config (settings.example.yaml, candidate_profile.example.yaml)
- `data/database/careeros.sqlite3` — default SQLite DB path
- `.env` — not loaded by app yet; reserved for future provider keys

## Toolchain quirks

- **No external runtime dependencies** — only stdlib + dev tools
- **SQLite only** — schema managed in `schema.py`, not via migration tooling
- **No CI/CD**, no pre-commit hooks, no Makefile
- Module sub-packages beyond `collectors` and `database` are stubs; adding real code there is expected
- Ruff: `line-length = 88`, selected rules `E F I UP B`
- `mypy` is strict, scoped to `src/careeros` only (not tests)

## Testing quirks

- All DB tests use `tmp_path` for disposable SQLite files (no fixtures, no external DB)
- Collector unit tests pass without network (test `normalize_job()` only, not `fetch_jobs()`)
- No integration/e2e tests yet
