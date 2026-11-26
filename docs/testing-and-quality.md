# Testing and quality

Career WatchPost uses focused unit/service/API tests, strict static checks,
disposable private state, and independent review. Run checks from a source
checkout with development dependencies installed:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

Historical counts are preserved for context: the accepted v1.2.1 behavioral
baseline was 4,208 tests; v1.2.2 focused qualification was 567 tests; public
history qualification was 142 tests. These campaigns are not additive and
were not all rerun for this documentation refinement; the 142-test figure is
recorded in the [published v1.2.2 release](https://github.com/BillPetrak/career-watchpost/releases/tag/v1.2.2).

Quality boundaries include deterministic ranking and grouping, schema migration
and recovery, private-file permissions, bounded extraction/provider responses,
loopback request protections, immutable Career Profile provenance, and exact
source/tag mapping. Documentation checks should additionally validate internal
links, image paths, headings, whitespace, and claims against current parser/API
behavior.

No release artifact, live provider request, owner database, or production
service is required for documentation verification.
