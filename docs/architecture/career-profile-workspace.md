# Career Profile workspace

The local workspace exposes explicit résumé lineage, deterministic fact runs,
candidate review, and immutable profile history through the
`careeros career-profile` CLI family, loopback HTTP routes under
`/career-profile`, and the zero-build web UI. It orchestrates résumé extraction,
Career Fact derivation, and profile assembly; it does not reimplement their
authority.

Navigation selects a résumé Material and exact version, then an extraction
and recognized deterministic fact run. There is no current-résumé singleton,
automatic version substitution, or automatic analysis on page open. Evidence
is independently paginated and returns only exact validated UTF-8 slices with
safe PDF/DOCX source labels, never full text, paths, or surrounding context.

Profile save submits a complete ordered manifest containing candidate IDs,
explicit supporting IDs, identity choices, privacy overrides, or carried
manual/imported logical IDs. It cannot submit authoritative values, hashes,
keys, origins, or sort order. Equal-key merge hints are non-authoritative and
never auto-merge. A stale revision returns a privacy-safe conflict; the UI
preserves the draft and requires explicit reapplication after reconciliation.

Reads use strict read-only connections; writes use the existing database
coordination and lock. Request validation, not-found, corruption, and
conflict responses are privacy-safe. Candidate/profile values are private
loopback data and are not placed in URLs, diagnostics, browser storage,
analytics, or external services. The workspace never changes Matching Profile
or Matching Preferences and never converts Career Profile facts into ranking
authority.

CLI commands and routes are listed in [`../user-guide.md`](../user-guide.md)
and the detailed reference maintained in `docs/reference/`.
