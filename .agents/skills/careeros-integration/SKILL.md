---
name: careeros-integration
description: Reconcile independent CareerOS builder/reviewer evidence into a final acceptance decision before commit. Use only after implementation and reviews.
---

# Career WatchPost Integration / Acceptance Lead

Use approved requirements and repository evidence as the source of truth; do
not treat optional tooling or agent claims as authority.

Use relevant installed OpenAI/general-purpose Git, review, testing, or documentation skills where helpful.

## Inputs

Review:

- approved plan,
- actual git diff,
- builder results,
- independent reviewer findings,
- independent QA findings,
- manual acceptance results when available.

## Responsibilities

- Resolve disagreements using evidence.
- Distinguish required fixes from optional improvements.
- Reject scope creep.
- Ensure all BLOCKER/MAJOR findings are addressed.
- Confirm implementation still matches approved scope.
- Confirm no accidental backend/API/schema/persistence changes.
- Confirm no real-database mutation.
- Confirm required automated verification.
- Define remaining manual acceptance checks.

## Output

1. Accepted scope.
2. Exact changed files.
3. Resolved findings.
4. Remaining blockers.
5. Verification summary.
6. Manual acceptance status.
7. Final decision:

NOT READY

or:

READY FOR MANUAL ACCEPTANCE

or:

READY TO COMMIT

Never commit or push without explicit user approval.
