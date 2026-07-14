# Sentry Self-Hosted → SaaS Migration Scripts

Fork of [github.com/dgbailey/migration](https://github.com/dgbailey/migration), annotated and fixed
for the self-hosted v25 → Sentry SaaS migration effort.

- **Roadmap, milestones, and branch model:** [ROADMAP.md](ROADMAP.md)
- **Changelog (vs. upstream):** [CHANGELOG.md](CHANGELOG.md)
- **Scope decisions log (deferrals to revisit):** [DECISIONS.md](DECISIONS.md)
- **Setup + runbook docs:** [docs/](docs/) (`phase-1` self-hosted setup, `phase-2` migration runbook)
- **Checkpoint:** the core scope (Projects, Teams & Membership, Alert Rules) is complete and tagged
  `v1.0-core` (also preserved on the frozen `phase-2-core` branch).
- **Trunk:** `main` is the integration branch; remaining data types land as `feat/<data-type>` branches
  merged via PR (see ROADMAP). `master` is a pristine mirror of upstream, used only to pull updates.

## What these scripts are

Standalone Python scripts that read a **self-hosted Sentry export** (Django `dumpdata`-style JSON:
a flat list of `{"model": ..., "pk": ..., "fields": {...}}` objects) and recreate a subset of that
data in **Sentry SaaS** (`https://sentry.io/api/0`) via the REST API.

They do **not** talk to the self-hosted API. The only self-hosted step is producing the export file.
Everything else is: parse export JSON → POST to SaaS.

```
self-hosted  --(export)-->  export.json  --(these scripts + SaaS token)-->  sentry.io
```

Core scope covered (the delivered P0 minimum):

- Projects
- Teams & membership
- Alert rules (metric alerts only — see limitations)

## Files

| File | Purpose |
|------|---------|
| `create_sentry_projects.py` | Create (or delete) projects in SaaS from the export |
| `create_sentry_teams.py` | Create teams and attach them to already-created projects |
| `add_sentry_members.py` | Add org members from the export |
| `assign_team_members.py` | Assign members to teams |
| `migrate_alert_rules.py` | Recreate metric alert rules |
| `duplicates_report.py` | Pre-flight (multi-org): cross-org project/team collisions + team-membership diffs from exports |
| `selfhosted_source.py` | Read-only live client for the self-hosted API (source for data the export omits) |
| `migrate_org_settings.py` | Migrate org governance + privacy settings (live self-hosted -> SaaS) |
| `migrate_project_settings.py` | Migrate per-project general settings (live self-hosted -> SaaS; matches projects by name) |
| `migrate_data_scrubbers.py` | Migrate standard data-scrubbing settings at org + project level (excludes advanced custom-PII) |
| `requirements.txt` | Python deps (`requests`) |
| `ROADMAP.md` | Scope targets, milestones, branch model |
| `CHANGELOG.md` | Changes vs. upstream |
| `docs/` | Setup + runbook documentation |
| `create_sentry_projects` | STALE — ignore (older variant, broken against real exports) |

All five migration scripts accept `--dry-run`, which logs the exact method/URL/payload each would send
without calling the API. Always dry-run first.

## Run order (hard dependencies)

```
pre. duplicates_report.py         -> (multi-org merges only) resolve cross-org collisions FIRST
0. [SaaS, manual] create a team whose slug is exactly "migration"
1. create_sentry_projects.py      -> projects (created UNDER the "migration" team)
2. create_sentry_teams.py         -> real teams + attach to projects
                                     writes: project_team_sync_results.json (team_id_mappings)
3. add_sentry_members.py          -> org members
                                     writes: user_mappings_for_teams.json, member_id_mappings.json
4. assign_team_members.py         -> member<->team assignments
                                     reads:  user_mappings_for_teams.json
5. migrate_alert_rules.py         -> metric alert rules
                                     reads:  project_team_sync_results.json (team_id_mappings)
```

Why this order:

- `create_sentry_projects.py` POSTs to `/teams/{org}/migration/projects/`, so a team slugged
  `migration` must exist first. Every project is initially created under that one team.
- `create_sentry_teams.py` creates the real teams AND attaches them to projects, which must already exist.
- `assign_team_members.py` needs `user_mappings_for_teams.json` from `add_sentry_members.py`.
- `migrate_alert_rules.py` maps an alert rule's `owner` (a team) via the team mappings from
  `create_sentry_teams.py`.

## Per-script detail

### 1. create_sentry_projects.py
- CLI: `python create_sentry_projects.py <auth_token> <org_slug> <export.json> [--delete] [--dry-run]`
- Reads export items where `model == sentry.project`; uses `fields.name`, `fields.slug`, `fields.platform`.
- Writes to SaaS: `POST /teams/{org}/migration/projects/` with `{name, platform}`.
- Writes locally: `project_management_results.json`.
- Notes: slug is not sent on create → SaaS derives it from the name, so slugs (and DSNs) change.
  `--delete` removes by slug — useful for resetting the test org.

### 2. create_sentry_teams.py
- CLI: `python create_sentry_teams.py <auth_token> <org_slug> <export.json> [--dry-run]`
- Reads `sentry.team`, `sentry.project`, `sentry.projectteam` to build team↔project relationships.
- Writes to SaaS: `POST /organizations/{org}/teams/`, then `POST /projects/{org}/{project}/teams/{team}/`.
- Writes locally: `project_team_sync_results.json` (includes `team_id_mappings`: old_pk → new_id), the
  bridge consumed by the alert-rule script.

### 3. add_sentry_members.py
- CLI: `python add_sentry_members.py <auth_token> <org_slug> --export-file <export.json> [--test you@gmail.com] [--send-invite] [--dry-run]`
  - or delete: `python add_sentry_members.py <auth_token> <org_slug> --delete <member_id_mappings.json>`
- Reads `sentry.organizationmember` (active users only).
- Writes to SaaS: `POST /organizations/{org}/members/` with `orgRole: "member"`.
- Writes locally: `member_id_mappings.json` and `user_mappings_for_teams.json` (used by step 4).
- Flags:
  - `--send-invite` sets `sendInvite`/`reinvite` true (default off = provision without emailing).
    Note: internal-integration tokens may not actually deliver invite emails; resend from the SaaS UI
    if delivery is required.
  - `--test` rewrites emails to a `+alias` on your own domain for safe inbox testing.
- Limitation: role is hardcoded to `member` (integration tokens can only invite `member`).

### 4. assign_team_members.py
- CLI: `python assign_team_members.py <auth_token> <org_slug> <export.json> <user_mappings_for_teams.json> [--dry-run]`
- Reads `sentry.organizationmemberteam` (+ `sentry.team`) and the user mappings file.
- Writes to SaaS: `POST /organizations/{org}/members/{member_id}/teams/{team_slug}/`.
- Writes locally: `team_member_assignments.json`. Skips users not created in step 3.

### 5. migrate_alert_rules.py
- CLI: `python migrate_alert_rules.py <auth_token> <org_slug> <export.json> <project_team_sync_results.json> [--dry-run]`
- Reads `sentry.alertrule` + `sentry.snubaquery` + `sentry.alertruleprojects` +
  `sentry.alertruletrigger` + `sentry.snubaqueryeventtype`.
- Writes to SaaS: `POST /organizations/{org}/alert-rules/`.
- Writes locally: `alert_rule_migration_results_<timestamp>.json`.
- Translation performed: real project-slug mapping, `queryType` from the snuba type, `eventTypes` from
  `snubaqueryeventtype`, `timeWindow` seconds→minutes, real trigger thresholds, owner → `team:<id>`,
  and a default email-to-owner-team action injected (SaaS rejects a trigger with no action).
  Issue alerts (`sentry.rule`) are detected and reported as skipped.

### duplicates_report.py (pre-flight, multi-org consolidation)
- CLI: `python duplicates_report.py org1.json org2.json [org3.json ...] [--label PATH=Name] [--similarity 0.6] [--out duplicate_report.json] [--html [duplicate_report.html]]`
- `--html` also writes a **self-contained** `duplicate_report.html` (inline CSS, no server or dependencies,
  opens offline in any browser) - a readable/shareable view of the same report.
- Offline only (never calls SaaS); one export file == one org. Use it BEFORE migrating when several
  self-hosted orgs are being merged into one SaaS org. Reports:
  - **PROJECT NAME** collisions (HARD - SaaS derives the slug from the name, so same-name projects clash),
  - **TEAM SLUG** collisions (HARD - slug must be unique),
  - **TEAM NAME** collisions with a **membership diff** (same team name, different rosters per org),
  - **PROJECT SLUG** collisions and **similar org names** (informational).
- Writes `duplicate_report.json` and exits non-zero if any HARD collision is found.
- Source is exports only (no live instance) for now - see DECISIONS.md D7.

## Known limitations (carried, flagged for review)

- **Issue alerts** (`sentry.rule`) are not migrated — metric alerts only (different endpoint/schema).
- **Alert notification actions** are not preserved; migrated rules get a default action only.
- **Member roles** are flattened to `member` (integration-token invite limitation).
- **Project slugs / DSNs change** because slug isn't sent on create (SaaS derives it from the name).
- **Duplicate names across instances** must be resolved before a merged run (`duplicates_report.py`
  reports them; the scripts do not auto-rename).

## SaaS token / permissions

Create an Internal Integration (Org Settings → Developer Settings → Custom Integrations) and use its
token. Scopes: `org:write`, `team:write`, `project:admin`, `member:write`, `alerts:write`. Member
invites also require a plan with the invite feature enabled (free Developer plan blocks them).

## Dependencies

```bash
pip install -r requirements.txt   # requests
```
