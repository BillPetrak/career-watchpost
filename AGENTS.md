# CareerOS — Agent instructions

## Quick start

```bash
uv sync --group development   # install with dev deps (pytest, ruff, mypy)
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
| `uv run careeros search engineer --limit 10` | Search stored jobs |
| `uv run careeros search engineer --remote --location Berlin` | Search with filters |
| `uv run careeros rank engineer --profile config/candidate_profile.example.json` | Rank stored jobs against profile |

Run order: **lint → mypy → test** (all independent; can run in parallel).

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
