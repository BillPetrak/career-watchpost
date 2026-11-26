# Ranking and duplicate grouping

Career WatchPost keeps source jobs as the authority and derives canonical
opportunity groups and ranking results without mutating those rows.

## Cross-source grouping

Grouping is equality-based and standard-library-only. A candidate pair must
share equal normalized company and title signatures. Company normalization is
case/Unicode/punctuation tolerant, removes a leading `The`, and strips
trailing data-driven legal-entity suffixes (`GmbH`, `BV`, `PLC`, `Oy`, `AB`,
`SA`, `SAS`, `Inc`, `LLC`, `Ltd`, and other listed locale forms). Title
normalization preserves the substantive seniority words.

Remote status is a veto: explicit remote and onsite classes cannot group;
unspecified is its own compatible class. Non-generic locations are compatible
only when one normalized token sequence is a prefix of the other (for example,
`Berlin` and `Berlin Germany`). Generic/empty locations (`Remote`, `Anywhere`,
and global filler terms) carry no geographic signal: they join the sole
located cluster, or form a separate singleton cluster when multiple located
clusters make the target ambiguous. Empty normalized company/title values are
never grouped.

Group IDs are the first 16 hexadecimal characters of SHA-256 over the group’s
normalized company/title/remote/location identity. They are content-addressed,
so unrelated additions never renumber groups. `group regroup` rebuilds only
derived grouping tables; it is deterministic and non-destructive. Grouped
search/rank filters are applied to candidate member rows before grouping, and
the displayed representative is the lowest-ID matching member. Flat rows and
application/reminder state remain anchored to concrete `jobs.id` values.

## Deterministic base ranking

All flat and grouped paths use the shared scorer and expose ordered score
components. The raw score is additive:

| Component | Current contribution and bound |
| --- | --- |
| Desired title | +20 each, at most 2 matches |
| Preferred/required skill | +8 each, at most 6 combined matches |
| Preferred location | +15 for the first matching preferred location |
| Matching remote preference | +10; conflict is neutral |
| Employment type | +5 on match, −5 only for explicit disjoint type |
| Preferred seniority | +5 on match, −5 only for an explicit detected conflict |
| Soft excluded term | −5 each, at most 3 |
| Missing required skill | −4 each, at most 3, only with usable description text |

Missing job fields are neutral. Hard-excluded terms are vetoes: they force the
displayed 0–100 score to zero while preserving the raw score and component
explanation. Otherwise the displayed score is clamped to 0–100. Ranking order
is descending displayed score, newest publication first (unknown publication
last), then ascending stable job ID. Grouped ranking uses the newest member
publication and then the lowest-ID representative.

## Review feedback

Feedback is read-only derived state. Each canonical group contributes once,
represented by its lowest-ID member with a current decision; this prevents
cross-source duplicates from multiplying evidence. `shortlist` contributes
`+5`, `dismiss` `−5`, and `review_later` `0` and never consumes a source slot.
At most two similar sources contribute (a ±10 envelope). Similarity requires
compatible normalized titles; company anchors matches, different companies
require specific two-token titles, and explicit remote, location, or disjoint
employment-type conflicts veto similarity. A candidate's own concrete/group
member decisions are excluded. Feedback is additive only and cannot bypass a
hard-exclusion veto.
