# CLI reference

Invoke commands as `uv run careeros ...`; `--help` is the authoritative
parser-generated detail. Paths default to private XDG state, and explicit
CLI paths take precedence over environment variables.

## Administration and services

- `diagnose` checks environment and notification readiness.
- `serve [--port PORT] [--ipv6] [--database PATH] [--materials-root PATH]`
  serves the protected loopback UI.
- `database backup DEST`, `migrate`, `restore`, and `rebuild-ranking` manage
  SQLite backups, the supported migration, restoration, and durable Review
  generations.
- `systemd install|uninstall [--dry-run] [--units collect|notify|all]
  [--unit-dir PATH] [--python PATH] [--no-systemctl]`; install additionally
  accepts `--enable` and `--force`. These operate only on generated user units.

## Private materials and profiles

- `material list|show|import|add-version|update|retire|restore|export-version|audit`
  manages immutable private Materials and their external bytes.
- `profile init|show|validate` manages the file-backed ranking Matching Profile.
- `career-profile sources|analyze|candidates|candidate|review|show|history|save`
  performs explicit résumé analysis, human review, and immutable Profile saves.

## Collection and discovery

- `collect SOURCE|all|due|status|history` collects or observes registered
  providers. `status` and `history` are read-only; `due` applies polling policy.
- `source stats [--source NAME] [--format text|json]` shows unified read-only
  source statistics and quality diagnostics.
- `search QUERY`, `show JOB_ID`, and `rank QUERY --profile PATH` inspect jobs.
  Search/rank support applicable `--grouped`, `--remote`, `--location`,
  `--source`, `--untracked`, `--status`, `--exclude-status`, and `--limit`;
  rank also supports `--min-score`, `--format`, `--no-feedback`, and
  `--no-adaptive`.
- `group regroup|explain` rebuilds or explains canonical duplicate groups.
- `report` and `export jobs|ranked|review|source-stats|collection-history`
  produce read-only summaries and deterministic exports.

## Review and workflow

- `review set|clear|show|list|history|next` manages decisions and the daily
  Review queue.
- `preference show|explicit replace|explicit clear|suppress|restore|reset-learned`
  manages explicit and learned Matching Preferences.
- `application set|show|list|history|redact-note` tracks local application
  state; `redact-note` deliberately destroys one private event note.
- `reminder add|list|show|update|complete|reopen|delete|notify-due|notify-history`
  manages local follow-ups. Due timestamps require an explicit offset.
- `saved-search create|list|show|run|update|rename|delete` stores reusable
  search/rank definitions.

Writes use the per-database advisory lock and may exit 75 on contention;
read-only commands do not acquire that lock. Validation/usage errors exit 2;
unknown records and ordinary database failures exit 1. Use disposable state in
examples; never put owner data in the checkout.
