---
name: careeros-team
description: Orchestrate CareerOS through one Lead, two available Builders, one independent correctness reviewer and one independent QA/browser reviewer, with explicit approval gates and high autonomy inside approved scope.
---

# CareerOS Five-Agent Team

## Team

1. Lead
   - Planner
   - Coordinator
   - Integrator

2. Builder 1

3. Builder 2

4. Correctness / Architecture Reviewer

5. QA / Test / Browser Reviewer

Five agents are available. Do not manufacture parallelism solely to keep all workers busy.

## Permission philosophy

Be highly autonomous for safe, local, in-scope development.

Routine repository inspection, editing within approved scope, testing, linting, type checking, local servers, temporary databases, browser automation and trusted documentation lookup should not require repeated user approval.

Stop for:
- material expansion of scope,
- destructive git/filesystem operations,
- system-level modification,
- secrets/credential handling,
- production/external writes,
- unexpected schema/API redesign,
- commit,
- push.

## Phase 1 — Plan

Lead uses careeros-planner.

The Lead is logically read-only during planning regardless of sandbox capability.

It may use define-goal, openai-docs and, where appropriate, grill-with-docs.

STOP at Gate A for user approval.

## Phase 2 — Build

After approval:

Lead assigns up to two non-overlapping packages.

Builder 1 and Builder 2 use careeros-builder.

Lead may independently decide whether one or both builders are justified.

Builders operate autonomously within assigned scope and may use normal local development tooling without repeated approval.

## Phase 3 — Independent review

Spawn fresh contexts.

Reviewer 1 uses careeros-reviewer.

Reviewer 2 uses careeros-test-review and should use Playwright Interactive when manual/browser investigation materially improves verification.

Reviewers do not see builder reasoning or each other's findings before finishing their independent reviews.

## Phase 4 — Fix loop

Lead validates reviewer findings.

BLOCKER/MAJOR fixes may be delegated to:
- a Builder, or
- the reviewer that found the issue if the Lead explicitly promotes that reviewer to fixer for the bounded task.

Re-review affected areas afterward.

Avoid churn on purely stylistic findings.

## Phase 5 — Integration

Lead switches from planner to integrator.

Lead may:
- inspect and reconcile worker output,
- run verification,
- make small integration-only workspace edits when necessary,
- resolve conflicts within approved scope.

Lead must not commit/push without user approval.

## Gates

Gate A:
after plan, before implementation.

Gate B:
after implementation + independent reviews + integration report, before commit.

Gate C:
before push unless explicitly pre-authorized.

## Context discipline

Keep verbose command/test/browser output in worker contexts.

Return concise evidence:
- command,
- result,
- failing cases,
- relevant excerpts,
- concrete findings.

Do not flood Lead context with successful raw logs.
