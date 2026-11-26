# Job sources and provider boundaries

## Registry and configuration

With no private source configuration, the registry is exactly these seven
public sources, in collection order: Arbeitnow, Remote OK, We Work Remotely,
Jobicy, Remotive, Working Nomads, and Himalayas.

Optional configuration is selected by `--sources-config`, then
`CAREEROS_SOURCES_CONFIG`, then `${XDG_CONFIG_HOME:-~/.config}/careeros/sources.yaml`.
The selected file must be a user-owned regular `0600` file and is bounded to
64 KiB. It can enable Lever, Greenhouse, Workable, and SmartRecruiters, with
at most eight configured boards/accounts/companies per provider. Configuration
is credential-free and public-read-only; it does not discover accounts or
submit applications.

### Configure optional ATS sources

Create `sources.yaml` in the private configuration location above, outside the
checkout, using an owner-controlled directory (preferably `0700`) and a regular
file owned by you with mode `0600`. For an existing file, verify its ownership
and path before changing permissions; do not overwrite an existing configuration
with this example. In the isolated learning session, the same XDG path is inside
the fresh temporary directory. CLI `--sources-config` overrides the environment
selector; HTTP surfaces use `CAREEROS_SOURCES_CONFIG`, then the XDG default.
A missing default is valid; a missing explicitly selected file is an error.

This version-1 example uses fictional identifiers and leaves every optional
provider disabled. It grants no collection permission:

```yaml
version: 1
sources:
  lever:
    enabled: false
    boards:
      - id: fictional-example-board
        instance: global
        company: Fictional Example Company
  greenhouse:
    enabled: false
    boards:
      - token: fictional-example-board
  workable:
    enabled: false
    boards:
      - subdomain: fictional-example-account
  smartrecruiters:
    enabled: false
    companies:
      - identifier: fictional-example-company
```

The root keys must be exactly `version` (integer `1`) and `sources`. Provider
sections are optional; each supplied section requires a boolean `enabled`.
Disabled sections may retain their definitions. An enabled section requires at
least one entry, with at most eight per provider and no duplicate identifiers:

| Provider | Entry fields |
| --- | --- |
| Lever | `boards`: `id`, `instance` (`global` or `eu`), and non-empty display `company` |
| Greenhouse | `boards`: `token` |
| Workable | `boards`: account `subdomain` |
| SmartRecruiters | `companies`: `identifier` |

Identifiers are lowercase ASCII matching `[a-z0-9][a-z0-9-]{0,99}`, not full
URLs. Replace the fictional entries with known identifiers only after confirming
authority under the [provider conditions](#provider-rights), then set only that
provider's `enabled` to `true`. Use its individual collection command
(`collect lever`, `collect greenhouse`, `collect workable`, or
`collect smartrecruiters`) when deliberately ready to contact it. Leaving these
four disabled does not disable the seven built-in sources.

## Request bounds and policy

All shared HTTP helpers enforce a 16 MiB limit from both declared and actual
response size. Optional providers use a provisional six-hour CareerOS polling floor and
make sequential bounded requests without retries, backoff, or concurrency:

| Provider | Public request bound per attempted run |
| --- | --- |
| Lever | Up to five pages per board, 100 rows/page, and 40 requests per provider run |
| Greenhouse | Board metadata plus one `jobs?content=true` request per board (maximum 16 requests) |
| Workable | One `accounts/{subdomain}?details=true` request per selected account (maximum 8) |
| SmartRecruiters | One `destination=PUBLIC` list page plus one detail request per selected posting (maximum 108 requests) |

The polling policy distinguishes provider guidance from provisional application
defaults; neither establishes collection or reuse permission:

| Provider | Minimum interval | Basis |
| --- | --- | --- |
| Arbeitnow | 6 hours | Provisional CareerOS policy |
| Remote OK | 4 hours | Provisional CareerOS policy |
| We Work Remotely | 6 hours | Provisional CareerOS policy |
| Jobicy | 60 minutes | Documented hourly-or-slower synchronization |
| Remotive | 6 hours | Documented maximum of four requests per day |
| Working Nomads | 6 hours | Provisional CareerOS policy; authority unresolved |
| Himalayas | 24 hours | Documented daily feed refresh; no benefit from more frequent polling |

Jobicy requests are capped at its documented 200 results. Working Nomads uses
the undocumented internal Elasticsearch endpoint
`POST https://www.workingnomads.com/jobsapi/_search` behind its public listings.
It may change without notice; malformed responses are collection failures.

`collect due` considers a source due when no completed attempt exists, the last
completed attempt is older than its floor, or the newest attempt is incomplete.
A completed failure delays the next automatic attempt by one interval. A
collection starts an observable run marker first; jobs, grouping, and success
bookkeeping commit atomically afterward. `collect status` and `collect history`
are read-only. Manual source and `all` commands bypass scheduling but still
record attempts and hold the per-database lock.

## Provider rights

The MIT license covers Career WatchPost source code, not provider job
descriptions, payloads, trademarks, logos, or employer content. Local collection
does not itself establish permission to collect, publish a feed, redistribute
structured listings, or run a live-data portfolio demo. Preserve source labels
and original listing links. The following conditions retain the previously
qualified provider guidance; consult the linked primary terms for your intended
use, without treating this publication as a certification of compliance.

- [Arbeitnow terms](https://www.arbeitnow.com/terms/) require attribution and
  links back to the original source listings.
- [Remote OK feed guidance](https://remoteok.com/faq) requires source credit
  and links back to Remote OK listings.
- [Jobicy API/RSS guidance](https://jobicy.com/jobs-rss-feed) requires canonical
  URLs, source attribution, hourly-or-slower synchronization, and no
  presentation as an original posting.
- [Remotive API terms](https://remotive.com/remote-jobs/api) require a Remotive
  link and source mention and prohibit third-party job-site and lead-capture
  uses.
- [Himalayas API terms](https://himalayas.app/terms) require unchanged
  application links, visible source credit/linking, and removal after expiry.
  They prohibit onward job-board, aggregator, or structured-data redistribution.
  This publication does not establish that expiry/removal obligations are
  automatically enforced by the application.
- We Work Remotely has no established general redistribution grant in the
  qualified documentation. Public RSS availability does not establish one.
- [Working Nomads terms](https://www.workingnomads.com/terms-and-conditions)
  restrict resale and competitive-product use. Collection authority for its
  undocumented endpoint remains unresolved, including local use. Do not collect
  from Working Nomads until authority for the intended use is established.

The optional ATS integrations expose narrow public-read interfaces. Public-read
access does not confer blanket collection, reuse, or redistribution authority:

- [Lever developer FAQ](https://hire.lever.co/developer/support).
- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html).
- [Workable careers-page API guidance](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page).
- [SmartRecruiters Posting API](https://developers.smartrecruiters.com/docs/posting-api)
  and [API policy](https://developers.smartrecruiters.com/docs/the-smartrecruiters-platform).

The configured account or board owner remains responsible for permission and
current terms. Confirm permitted collection and use before enabling a source;
avoid collection where authorization is unresolved. `collect all`, `collect due`,
and the collection timer can attempt every registered source, including Working
Nomads; they are not a recommendation or a permission check. Use individual
source commands only where authority is established. Career WatchPost never
contacts employers or submits applications and does not authorize public
redistribution of collected data.
