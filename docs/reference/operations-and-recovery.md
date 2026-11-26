# Operations and recovery

## State, locks, and private exports

Use XDG defaults or explicit selectors; never place owner state in the
checkout. Collection and workflow writes coordinate through the companion
`<absolute-database-path>.lock` advisory lock. The lock is fail-closed for
symlinks, non-regular files, unsafe ownership/modes, and link counts; stale
owned regular `0600` files are harmless. Contention exits 75 without fetching
or changing database state. Never delete a lock file as a remedy.

Exports are read-only but can contain private profile, ranking, application, or
job data. With `--output`, Career WatchPost creates an owner-only `0600` regular
file in an existing owner-controlled directory and rejects symlinks, hard links,
unsafe ownership/modes, and replacement without `--overwrite`. Overwrite uses a
same-directory temporary file and atomic publication. Shell redirection bypasses
these checks, so prefer `--output`. Materials `export-version` is an explicit
verified copy and should likewise target a private path. Never commit or
redistribute provider payloads.

## Migration, backup, and complete recovery

Schema 14 is current. Version v1.2.0 introduced the schema-13-to-14 step;
the current migration dispatcher accepts structurally valid schemas 8–13
and applies the remaining steps in order: 8 → 9 → 10 → 11 → 12 → 13 → 14.
A version number alone is insufficient: each source and target boundary is
validated, and failure rolls back the entire upgrade transaction. Schemas
below 8, unknown/malformed structures, and future versions fail closed.
There is no down migration or automatic startup migration.

Stop writers before this explicit operation; the administration command takes
the database maintenance lock. Back up first. These are operational templates:
replace every path with the intended database and private backup location.

```bash
uv run careeros database backup /safe/path/careeros.sqlite3.backup \
  --database /path/to/careeros.sqlite3
uv run careeros database migrate --database /path/to/careeros.sqlite3
```

The normal migration creates `/path/to/careeros.sqlite3.pre-v14.backup.sqlite3`
unless an alternative backup is selected with `--backup`. The CLI permits
skipping it only with both `--no-backup` and `--acknowledge-data-loss-risk`;
the recommended recovery workflow retains a backup. Restore only a validated SQLite
backup, with writers stopped:

```bash
uv run careeros database restore /safe/path/backup.sqlite3 \
  --database /path/to/careeros.sqlite3 --confirm-replace
```

SQLite backup does not contain managed Material bytes. A complete recovery is
a coordinated set:

1. Stop the web process, collection/notification workers, and user timers.
2. Back up SQLite with `database backup` and copy the quiescent matching
   Materials root, including `.careeros-store` and all objects.
3. Preserve the applicable private candidate profile and `sources.yaml` with
   their private permissions and record which database/root belong together.
4. Restore SQLite with `database restore`, restore the matching Materials tree
   separately, and select both explicitly.
5. Before restarting writers, run `careeros material audit --database TARGET
   --materials-root RESTORED_ROOT`.

Do not edit a binding marker or database to force a mismatched pair. Rebuild
durable Review ranking with `database rebuild-ranking` only after the restored
database and inputs are consistent.

## Services and diagnostics

`careeros diagnose` reports environment and notification readiness. Preview
user-unit changes with:

```bash
uv run careeros systemd install --dry-run --units all
uv run careeros systemd uninstall --dry-run --units all
```

Before enabling a collection timer, review the
[provider-use boundaries](job-sources.md#provider-rights). The timer runs
`collect due` and can initiate collection from registered sources, including
Working Nomads where authority remains unresolved. Do not enable collection
indiscriminately: establish permission for every source it may attempt first.
The notification timer is separate and does not collect jobs.

Installation supports `--units collect|notify|all`, `--unit-dir PATH`,
`--python PATH`, `--no-systemctl`, `--enable`, and deliberate `--force` when
an existing generated unit differs. Use `--python` to pin the interpreter
that owns the installed package; do not replace foreign or modified units
without reviewing them. `--enable` enables timers immediately; optional
`loginctl enable-linger "$USER"` allows user timers without an active login.
Uninstall removes only exact generated units and skips modified/foreign files.
Installation is user-level, never uses `sudo`, and never writes system-wide
units. The collection timer checks after five minutes and every 15 minutes;
the independent notification timer checks after one minute and every five
minutes (both add a bounded randomized delay).

The web service binds to `127.0.0.1` (or `::1` with `--ipv6`), disables proxy
header trust, and runs one worker. Keep it loopback-only. For collection
failures inspect `collect status` and `collect history`; incomplete attempts
are retried by `collect due`. Do not execute these procedures against owner
production state while troubleshooting documentation.
