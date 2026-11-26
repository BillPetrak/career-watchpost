# Contributing

Career WatchPost is maintained as a privacy-first Python application. Read the
nearest architecture and reference document before changing a subsystem; the
[documentation index](docs/README.md) maps the contracts.

## Development environment

Use Python 3.11+ and `uv` from a source checkout:

```bash
uv sync --extra development --extra api
```

Keep databases, Materials, profiles, provider configuration, exports, and
logs in temporary or private XDG paths. Tests must use fictional fixtures and
must never read owner state or request live providers.

## Verification

Run the smallest relevant tests first, then the standard checks:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

Documentation changes should also validate links, image paths, headings, and
whitespace. Do not add unsupported claims, provider payloads, private paths, or
personal data.

## Scope and review

Keep changes focused and preserve deterministic, server-authoritative behavior,
schema invariants, privacy boundaries, and stable `careeros` identifiers.
Document migrations and recovery implications. Human review is expected for
all changes; independent correctness and QA review are useful for behavioral
work.

The repository includes optional CareerOS agent skills for planning,
implementation, independent review, QA, and integration. They are methodology
documents, not bundled dependencies or a guarantee of autonomous correctness;
use only tooling available in the execution environment and do not download
unapproved dependencies.
