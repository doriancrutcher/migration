# Migration Roadmap

Goal: migrate a full Sentry organization from **self-hosted v25 → Sentry SaaS**. The delivered
minimum (core scope) is complete; the remaining data types are grouped into later phases so work can
proceed without disturbing the checkpoint.

## Scope targets and status

| # | Data type | Phase | Branch | Status |
|---|-----------|-------|--------|--------|
| 1 | Projects | phase-2-core | `phase-2-core` | Done |
| 2 | Teams & membership | phase-2-core | `phase-2-core` | Done |
| 3 | Alert rules (metric) | phase-2-core | `phase-2-core` | Done |
| 4 | All organization settings | phase-3-settings | `phase-3-settings` | Planned |
| 5 | Projects and their settings | phase-3-settings | `phase-3-settings` | Planned |
| 6 | Teams and their settings | phase-3-settings | `phase-3-settings` | Planned |
| 7 | Enabled data scrubbers | phase-3-settings | `phase-3-settings` | Planned |
| 8 | User accounts and member options | phase-3-settings | `phase-3-settings` | Planned |
| 9 | Crons | phase-4-content | `phase-4-content` | Planned |
| 10 | Dashboards | phase-4-content | `phase-4-content` | Planned |
| 11 | Repositories | phase-4-content | `phase-4-content` | Planned |
| 12 | Recent and saved searches | phase-4-content | `phase-4-content` | Planned |

(Items 1-3 are the P0 minimum that was promised and delivered.)

## Phases

### phase-2-core (Done)
Projects, Teams & Membership, Alert Rules. Tagged `v1.0-core`. See `README.md` and `docs/`.

### phase-3-settings (Planned)
Configuration/governance data that layers onto the objects created in core:
- All organization settings
- Projects and their settings
- Teams and their settings
- Enabled data scrubbers (org- and project-level `sensitiveFields` / `scrubIPAddresses` / `dataScrubber`)
- User accounts and per-member options (roles, notification options)

### phase-4-content (Planned)
Higher-level content and integrations:
- Crons (monitors)
- Dashboards
- Repositories (integration-dependent)
- Recent and saved searches

## Candidate Sentry API references (starting points)

- Add member to org: https://docs.sentry.io/api/organizations/add-a-member-to-an-organization/
- Add members to teams (`organizationmemberteam`): member → team endpoints
- Org settings: `GET/PUT /organizations/{org}/`
- Project settings: `GET/PUT /projects/{org}/{project}/`
- Team settings: `GET/PUT /teams/{org}/{team}/`
- Dashboards: `/organizations/{org}/dashboards/`
- Monitors (crons): `/organizations/{org}/monitors/`
- Repositories: `/organizations/{org}/repos/`
- Saved searches: `/organizations/{org}/searches/`

(Endpoints to be confirmed per data type during each phase.)

## Working model (do not disrupt the checkpoint)

- `master` mirrors upstream `dgbailey/migration` (pristine; used to pull Dustin's updates).
- `phase-2-core` holds the checkpoint commit and the `v1.0-core` tag — treat as frozen.
- New work happens on `phase-3-settings` / `phase-4-content`, branched off the checkpoint.
- Each new data type is a **new script file** following the established pattern (export-driven,
  `--dry-run`, writes a mapping file). Existing core scripts are not modified on the new branches.
