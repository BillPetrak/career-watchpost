# Adaptive recommendations

Matching Preferences are a persistent, inspectable, reversible ranking
authority. Explicit preferences and learned evidence are separate. Review
decisions can contribute bounded learned signals for supported dimensions;
they never rewrite the explicit profile or the job corpus.

Evidence is deduplicated by stable decision identity, reversals are represented
as history, and activation requires the implemented strength/contradiction
rules. Learned keys can be inspected, suppressed, restored, or reset while
retaining the relevant audit history. A reset removes learned evidence and
suppression state without deleting explicit preferences or review history.

## Learning algorithm

Only `shortlist` and `dismiss` decisions create a replaceable evidence
snapshot; `review_later` is neutral and creates no learning signal. A changed
shortlist/dismiss decision replaces that job's evidence, while clearing or
reversing a decision removes/replaces the old snapshot. Evidence is derived
from normalized title, company, location, remote (only explicit `remote`),
employment type, and detected seniority. Skills and free-form description are
not learned dimensions. For a canonical duplicate group, only the lowest-ID
member that currently has a decision supplies evidence, so one opportunity
cannot count once per provider.

An aggregate activates positively at least 2 positive examples with net
evidence at least +2, or negatively at least 3 negative examples with net at
least −2. Strength is `min(2, abs(net_evidence) - 1)`, giving levels 1–2;
otherwise polarity is absent and the preference is inactive. Explicit values
lock their matching dimension (`desired_titles`→title, preferred locations,
remote preference, employment types, or seniority), preventing learned values
from contributing there. Explicit skills do not lock a learned dimension.

Per-dimension learned weights are title 2 and all other dimensions 1. Each
dimension is capped at ±4 for title or ±2 for other dimensions; the aggregate
learned adjustment is globally capped at ±8. Legacy review feedback uses
shortlist +5, dismiss −5, and review-later 0, with at most two similar
canonical sources (±10). Learned and legacy feedback share a combined ±10
envelope, so the effective learned contribution is the remaining headroom.
Feedback similarity requires compatible normalized titles and vetoes explicit
remote, location, and employment-type conflicts; company anchors matches and
different companies require specific two-token titles. A candidate's own
decision is excluded.

Suppression removes one learned key from effective ranking while retaining its
evidence and audit event; restore removes only that suppression. Reset deletes
learned evidence and suppressions at a review-event boundary, preserving
explicit authority and review history. Active keys and score components are
deterministic; ranking ties are descending public score, newest publication
first (unknown publication last), then ascending stable job ID.

Ranking reuses deterministic title/company/location/remote/employment-type and
seniority primitives. Adaptive contributions are surface-specific, bounded,
and included in public score-component explanations; hard exclusions remain
vetoes. Stable tie-breaks preserve deterministic ordering. HTTP/web ranking
does not silently reuse CLI-only feedback behavior.

The preference service persists only the authority needed to reproduce state;
private profile values and prepared descriptions are not stored in ranking
generations. Explicit-authority changes invalidate both learned modes;
effective learned changes invalidate only the enabled mode. Semantic no-ops,
inactive evidence, and note-only events do not trigger rebuilds. Read
[`today-materialized-ranking.md`](today-materialized-ranking.md) for durable
Review ranking generations and [`../user-guide.md`](../user-guide.md) for
operator commands.
