# Core migration (Projects, Teams & membership, Alert rules)

The delivered P0 minimum. Five export-driven scripts that recreate a subset of a self-hosted org in SaaS.
They read a self-hosted **export** and POST to `https://sentry.io/api/0`; they do not call the self-hosted API.

- **Dependencies:** `requests` (`pip install -r ../requirements.txt`).
- All five accept `--dry-run` (logs the exact method/URL/payload without calling the API). **Always dry-run first.**

## Run order (hard dependencies)

```
0. [SaaS, manual] create a team whose slug is exactly "migration"
1. create_sentry_projects.py   -> projects (created UNDER the "migration" team)
2. create_sentry_teams.py      -> real teams + attach to projects
                                  writes: project_team_sync_results.json (team_id_mappings)
3. add_sentry_members.py       -> org members
                                  writes: user_mappings_for_teams.json, member_id_mappings.json
4. assign_team_members.py      -> member<->team assignments
                                  reads:  user_mappings_for_teams.json
5. migrate_alert_rules.py      -> metric alert rules
                                  reads:  project_team_sync_results.json (team_id_mappings)
```

Why this order: projects are POSTed to `/teams/{org}/migration/projects/` (so the `migration` team must
exist first); teams attach to already-created projects; member→team assignment needs the user mappings from
step 3; alert rules map an `owner` team via the mappings from step 2.

## Per-script detail (run from the repo root)

### 1. create_sentry_projects.py
- `python3 core/create_sentry_projects.py <auth_token> <org_slug> <export.json> [--delete] [--dry-run]`
- Reads `sentry.project`; POSTs `{name, platform}` to `/teams/{org}/migration/projects/`. Slug isn't sent →
  SaaS derives it from the name (slugs/DSNs change). `--delete` removes by slug (handy for resetting a test org).
- Writes `project_management_results.json`.

### 2. create_sentry_teams.py
- `python3 core/create_sentry_teams.py <auth_token> <org_slug> <export.json> [--dry-run]`
- Reads `sentry.team`, `sentry.project`, `sentry.projectteam`; POSTs teams then attaches them to projects.
- Writes `project_team_sync_results.json` (includes `team_id_mappings`, consumed by the alert-rule script).

### 3. add_sentry_members.py
- `python3 core/add_sentry_members.py <auth_token> <org_slug> --export-file <export.json> [--test you@gmail.com] [--send-invite] [--dry-run]`
- Delete mode: `python3 core/add_sentry_members.py <auth_token> <org_slug> --delete <member_id_mappings.json>`
- Reads `sentry.organizationmember` (active users); POSTs to `/organizations/{org}/members/` with `orgRole: "member"`.
- Writes `member_id_mappings.json` and `user_mappings_for_teams.json` (used by step 4).
- `--send-invite` sets `sendInvite`/`reinvite` (default off = provision without emailing; integration tokens
  may not deliver emails — resend from the UI if needed). `--test` rewrites emails to a `+alias` for safe testing.
- Limitation: role is hardcoded to `member`.

### 4. assign_team_members.py
- `python3 core/assign_team_members.py <auth_token> <org_slug> <export.json> <user_mappings_for_teams.json> [--dry-run]`
- Reads `sentry.organizationmemberteam` (+ `sentry.team`) and the user mappings; POSTs
  `/organizations/{org}/members/{member_id}/teams/{team_slug}/`. Writes `team_member_assignments.json`.

### 5. migrate_alert_rules.py
- `python3 core/migrate_alert_rules.py <auth_token> <org_slug> <export.json> <project_team_sync_results.json> [--dry-run] [--only NAME]`
- Reads `sentry.alertrule` (+ `snubaquery`, `alertruleprojects`, `alertruletrigger`, `snubaqueryeventtype`);
  POSTs to `/organizations/{org}/alert-rules/`. Writes `alert_rule_migration_results_<timestamp>.json`.
- Translates: project-slug mapping, `queryType` from the snuba type, `eventTypes` from `snubaqueryeventtype`,
  `timeWindow` seconds→minutes, real trigger thresholds, owner → `team:<id>`, and a default email action
  (SaaS rejects a trigger with no action). Issue alerts (`sentry.rule`) are detected and reported as skipped.
