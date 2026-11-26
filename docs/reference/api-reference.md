# API reference

The optional FastAPI service is loopback-only by default. For normal operation,
start it with `uv run careeros serve`; this validates the selected schema-14
database and matching Materials root before binding. The contributor-oriented
`uv run uvicorn careeros.api:app` entry point uses environment-selected defaults
and does not perform that `serve` preflight. The UI is at `/ui`. `GET /health`
reports database presence/schema metadata; it is diagnostic, not a readiness
guarantee.

## Route map

| Subsystem | Read routes | Compute or write routes |
| --- | --- | --- |
| Jobs and sources | `GET /jobs`, `/jobs/{job_id}`, `/jobs/{job_id}/group`, `/sources/stats`, `/collection-runs` | `POST /ranked-jobs`, `POST /review-queue` |
| Today and report | `GET /today`, `GET /report` | `POST /report` |
| Applications | `GET /application-pipeline`, `/applications`, `/applications/{job_id}`, `/applications/{job_id}/history` | `PUT /applications/{job_id}` |
| Reminders | `GET /reminders`, `/reminders/{reminder_id}` | `POST /reminders`, `PATCH /reminders/{reminder_id}`, `PUT /reminders/{reminder_id}/complete`, `PUT /reminders/{reminder_id}/reopen`, `DELETE /reminders/{reminder_id}` |
| Review and saved searches | `GET /review-decisions`, `/review-decisions/{job_id}`, `/reviewed-opportunities`, `/saved-searches`, `/saved-searches/{name}` | `PUT`/`DELETE /review-decisions/{job_id}`, `POST /saved-searches`, `POST /saved-searches/{name}/execute`, `PATCH`/`DELETE /saved-searches/{name}` |
| Groups | `GET /groups`, `/groups/summary`, `/groups/{group_id}` | regrouping is a CLI/database operation |
| Preferences | `GET /preferences`, `/preferences/learned` | `PUT /preferences/explicit`, `DELETE /preferences/explicit`, `PUT /preferences/learned/{key}`, `DELETE /preferences/learned` |
| Career Profile | `GET /career-profile/sources`, `/career-profile/current`, `/career-profile/history`, `/career-profile/versions/{version_id}`, and candidate routes | analysis, candidate review, and `POST /career-profile/versions` |
| Materials | `GET /materials`, `/materials/{material_id}`, `/material-versions/{version_id}/content` | `POST /materials`, `POST /materials/{material_id}/versions`, retirement routes |
| Packages and preparation | package/version and preparation GET routes | package draft, answer, requirement, review, ready, and finalization routes |

`POST /ranked-jobs`, `POST /review-queue`, and `POST /report` are read-only
computations despite using an unsafe HTTP method; they still require the write
header described below. The exact parameters and response models are
authoritative in `src/careeros/api/main.py`; this table intentionally summarizes
candidate, material-retirement, package/version, and preparation subroutes and
omits repetitive schema fields.

## Local security contract

Every request must arrive from a loopback peer with exactly one valid
`Host` (`localhost`, `127.0.0.1`, or `::1`). `Forwarded`, `X-Forwarded-*`, and
`X-Real-IP` headers are rejected; Uvicorn disables proxy-header trust. An
`Origin`, when supplied, must be the same loopback HTTP origin, and cross-site
fetch/preflight requests are rejected. Unsafe methods (`POST`, `PUT`, `PATCH`,
`DELETE`) additionally require `X-CareerOS-Write: 1`.

Responses set `Cache-Control: no-store` plus CSP, frame, content-type, and
referrer protections. The service does not contact providers on behalf of API
callers or submit applications.

## Important status semantics

- `403`: local security policy rejects peer, host, forwarded headers, origin,
  fetch context, or a missing write header.
- `404`: requested job, application, reminder, material, or other concrete
  record does not exist.
- `409`: optimistic revision conflict or another state transition conflict;
  callers must reload rather than overwrite newer state.
- `422`: request validation failure, including forbidden/invalid fields,
  malformed timestamps, and invalid lifecycle data.
- `503`: the database writer/collection lock is currently held, or a durable
  materialized Review ranking is not ready; retry after inspecting diagnostics.

Use disposable databases and fictional data for API experiments. Do not expose
the loopback service through a reverse proxy without a separately reviewed
deployment boundary.
