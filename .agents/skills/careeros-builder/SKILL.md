---
name: careeros-builder
description: Implement an approved CareerOS work package autonomously inside the repository using the smallest repo-native change, with local development tools and focused verification.
---

# CareerOS Builder

Own one approved implementation work package.

Use relevant installed skills when useful, including:
- cli-creator
- playwright
- openai-docs
- security-best-practices when implementation risk warrants it

## Operational freedom

Within the approved work package you may freely:

- read and edit assigned repository files,
- inspect surrounding repository code,
- run git status/diff/log and other read-only git commands,
- run pytest, Ruff, mypy, format checks and JavaScript syntax checks,
- build packages/wheels,
- create temporary files and databases,
- start and stop project-local development servers,
- bind localhost ports,
- access trusted package registries/documentation when needed,
- install project-local dependencies when genuinely required by the approved work,
- use installed technical skills.

Do not stop for permission for ordinary non-destructive development operations within scope.

## Boundaries

Do not:

- commit or push,
- stage files unless the Lead explicitly asks,
- run git reset --hard or git clean -fd,
- overwrite protected pre-existing work,
- use sudo,
- modify system packages/configuration,
- modify shell startup files,
- expose or alter credentials,
- modify unrelated files outside the workspace,
- mutate the real CareerOS database during testing,
- broaden API/schema/backend scope without Lead approval.

## Implementation principles

- Make the smallest correct change.
- Reuse existing architecture/helpers.
- Avoid duplicate business logic.
- Preserve deterministic and server-authoritative behavior.
- Avoid optimistic writes unless explicitly approved.
- Preserve grouped/flat semantics where relevant.
- Maintain privacy/local-first constraints.
- Keep SCHEMA_VERSION unchanged unless persistence genuinely changes.

## Completion

Run focused verification first and broader required verification afterward.

Report:

1. Exact files changed.
2. What changed.
3. Commands/checks and results.
4. Unresolved concerns.
5. Any deviation from assigned scope.

Do not commit or push.
