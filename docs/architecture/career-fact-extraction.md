# Deterministic Career Fact extraction

Career Fact extraction is a local structural pass over one exact successful
résumé extraction. It emits candidates and evidence; it does not establish truth,
ownership, or profile authority. It never calls a model/provider/network and
does not mutate Matching Profile, Matching Preferences, feedback, or learned
state.

## Conservative grammar

Persisted types are `identity.name`, `contact.email`, `contact.phone`,
`portfolio_reference`, `skill`, `employment`, `education`, `certification`,
and `language`. Recognized labels/headings and explicit delimiters are
line-bound. Ambiguous prose, free-form achievements, aliases, seniority,
proficiency, chronology, and inferred meaning are omitted. Dates retain only
the authored precision (`YYYY`, `YYYY-MM`, or `YYYY-MM-DD`). URLs are accepted
only through an explicit portfolio-style label.

Display values preserve authored content. Comparison keys apply the frozen
Unicode normalization, casefolding, whitespace/punctuation, email, phone, and
URL rules. Exact duplicate-equivalent occurrences merge; similar or
conflicting values remain separate.

## Evidence and integrity

Evidence points into the exact extracted raw text using zero-based half-open UTF-8
byte offsets and a SHA-256 of each exact slice. Discontinuous ranges are not
widened. Candidates and evidence have deterministic ordering, IDs, schema and
extractor fingerprints. Cache reuse first validates the complete aggregate;
malformed, truncated, unknown-fingerprint, or tampered records fail closed.
Bounds cover input size, candidates, evidence, field display values, keys and
canonical JSON. Exceeding a bound fails the aggregate without truncation.

`confidence: 1.0` means that a frozen structural rule matched; it does not
mean the candidate is verified. Contact candidates remain non-authoritative.
Only human review can approve, edit, or reject a candidate, and only approved
candidates may be deliberately assembled into a Career Profile.
