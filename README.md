# CareerOS

CareerOS is a privacy-first career intelligence platform.

Its first working module will be an AI-assisted job discovery and
recommendation system that collects job advertisements, normalizes them,
stores them locally, ranks them, and produces explainable recommendations.

## Design principles

- Local-first storage
- Privacy by design
- Modular and replaceable components
- Explainable AI-assisted ranking
- Cost-aware model usage
- Automated testing
- Clear documentation

## Planned modules

- Job Hunter
- Candidate Profile
- Job Ranking
- Feedback and Preference Learning
- Skill Intelligence
- CV Analysis
- Career Analytics
- Reporting and Notifications

## Architecture

CareerOS owns its data, business logic, and provider interfaces.

External systems such as Hermes and LLM providers will be treated as
replaceable integrations rather than permanent dependencies.

## Current status

Milestone 1: repository and development foundation.

Current CLI capabilities include multi-source job collection (`collect
<source>`, `collect all`), rate-aware scheduled collection (`collect due`) with
persistent run history, read-only collection observability (`collect status`,
`collect history`), plus `search`, `show`, `rank`, and `diagnose`. See
`AGENTS.md` for the full command reference.
