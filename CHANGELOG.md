# Changelog

This is a concise public history of released behavior. Qualification numbers
are campaign results, not additive totals or a claim that every campaign used
the same suite.

## v1.2.2

Public Career WatchPost identity and source-only portfolio publication. The
stable `careeros` namespace, schema 14, behavior, and tags were preserved; no
wheel or source distribution was rebuilt or published for this source-only
release. The accepted v1.2.1 behavioral baseline remained 4,208 tests; the
v1.2.2 presentation qualification recorded 567 focused tests; the independent
public-history qualification recorded 142 tests. These are separate campaigns
and are not additive. The public-history figure is preserved from the
[published v1.2.2 release record](https://github.com/BillPetrak/career-watchpost/releases/tag/v1.2.2).

## v1.2.1

Private Matching Profile/export hardening and a deterministic Career Fact
correction for a narrowly defined wrapped-skill case. The focused campaign
passed 366 tests, synthetic workflow qualification passed 405 tests, and the
accepted full behavioral baseline passed 4,208 tests with zero failures and two
accepted warnings. These results describe distinct scopes, not additive test
counts.

## v1.2.0

Schema 14 and explicit schema-13-to-schema-14 migration after backup. Today
Review ranking became durable through persisted materialized generations,
bindings, integrity checks, and local rebuild/recovery behavior. The corrected
authoritative full suite passed 4,013 tests with zero failures and two accepted
warnings.

## v1.1.1

Career Profile assembly provenance was frozen so later reviews cannot rewrite
historical projections. The focused campaign passed 102 tests; the historical
full suite passed 3,895 tests with zero failures and two accepted warnings.

## v1.1.0

Added optional credential-free, owner-curated Lever, Greenhouse, Workable, and
SmartRecruiters sources while retaining the seven built-in defaults. The
historical full suite passed 3,889 tests with zero failures and two accepted
warnings.

## v1.0.5

Added local bounded résumé extraction, deterministic Career Facts, human review,
and immutable Career Profile snapshots. OCR/image-only PDF, legacy DOC,
arbitrary layout reconstruction, hosted extraction, and submission remained
outside scope.

## v1.0.0

Initial released CareerOS workflow: local SQLite job collection from the seven
built-in public sources, search, deterministic matching, review workflow, and
local-first privacy boundaries. The original release evidence is retained in
the annotated Git tag rather than repeated as a current qualification claim.
