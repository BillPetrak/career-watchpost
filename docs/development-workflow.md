# Development workflow

Career WatchPost was developed with human-directed, AI-assisted engineering.
The owner defines scope and acceptance criteria; agents inspect the existing
architecture, make small repository-native changes, and provide evidence.
AI assistance does not guarantee correctness and the workflow is not a
universal requirement for other coding tools.

## Roles

When the environment supports them, the repository skills divide work into a
read-only planning lead, non-overlapping builders, a fresh-context correctness
reviewer, a fresh-context QA/browser reviewer, and an integration lead. A
single builder or reviewer is appropriate when parallelism would add cost
without independence. Optional skills and external tools may be absent; do not
download them without approval.

Reviewers do not inherit builder reasoning or each other's findings. They
compare requirements, source, tests and the actual diff, checking privacy,
determinism, concurrency, failure behavior and scope. Integration resolves
concrete findings before handoff. Production state, credentials, external
providers, commits and pushes remain outside autonomous authority.

## Evidence loop

Inspect first, identify the authoritative implementation and tests, then make
the smallest change. Run focused verification and proportionate lint/type/test
checks. Use temporary databases and fictional fixtures. Report exact files,
commands, results, limitations and any unverified assumption. Human review
decides whether work is accepted, committed, or published.
