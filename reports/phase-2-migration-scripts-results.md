# Phase 2: Migration Scripts — Findings & Runbook

**Status: Complete.** A full live run into `dest-saas-org` was verified end to end — Projects, Teams (with associations), Members, Team membership, and Alert Rules all migrated successfully (see §10). Member migration required a Business trial on the destination org to lift a free-plan invite gate.

## Goal

Confirm the correct run order for Dustin's migration scripts, verify they carry over the P0 data set (Projects, Teams & Membership, Alert Rules) from **self-hosted v25 to SaaS**, and patch any gaps found along the way.

Scripts repo: `migration-testing/migration` — a fork of [github.com/dgbailey/migration](https://github.com/dgbailey/migration), annotated and fixed as part of this work.

## 0. Where Everything Lives

**Deliverables + scripts (this project):** `Sentry Migration Project/migration-testing/`

- `migration/` — the annotated and fixed scripts, plus `README.md` and `requirements.txt`
- `export.json` — the self-hosted export
- `seed_selfhosted.py` — idempotent source-data seeder
- `.venv/` — Python environment (`requests` only)
- `dryrun-out/` — dry-run output and mapping files (scratch)

**Docker infra (intentionally left in home dir):** `~/sentry-migration-testing/self-hosted/` — the running Sentry stack. Its data lives in Docker volumes, not this project folder.

Commands below assume you first `cd` into the `migration-testing` folder:

```bash
cd "<project-root>/migration-testing"
```

## 1. Environment

- Self-hosted Sentry **25.6.2**, native **arm64** on Apple Silicon (see `phase-1-self-hosted-v25-setup.md`)
- Source org: `migration-test-org`
- Scripts run using the venv at `migration-testing/.venv` (dependency: `requests`)

## 2. Test Data Seeded into Self-Hosted (Source)

Created via an idempotent ORM script (`migration-testing/seed_selfhosted.py`), run through `docker compose run --rm -T web django shell`:

| Type | Count | Detail |
|---|---|---|
| Teams | 4 | backend, frontend, platform (+ default `sentry`) |
| Projects | 6 | checkout-service, payments-api (backend); web-dashboard, mobile-app (frontend); data-pipeline (platform); (+ default `internal`) |
| Members | 4 | dorian (owner), alice, bob, carol (active users) with team memberships |
| Metric alert rules | 5 | one per non-default project, each owned by the project's team |

## 3. Export (Self-Hosted Side)

The relocation-style export produces the exact `model`/`pk`/`fields` JSON the scripts consume:

```bash
MIG="<project-root>/migration-testing"
cd ~/sentry-migration-testing/self-hosted
docker compose run --rm -T -v "$MIG:/export" \
  web export organizations /export/export.json \
  --filter-org-slugs migration-test-org --no-prompt
```

We verified the export contains all required models: `sentry.project` (6), `sentry.team` (4), `sentry.projectteam` (6), `sentry.organizationmember` (4, with `user_email`/`user_is_active`), `sentry.organizationmemberteam` (6), `sentry.alertrule` (5), `sentry.snubaquery` (5), plus the supporting `sentry.alertruleprojects`, `sentry.alertruletrigger`, and `sentry.snubaqueryeventtype`.

## 4. Confirmed Run Order

There are hard dependencies between steps: a team named `migration` must pre-exist in SaaS; projects must exist before teams; member mappings must exist before team assignment; and team mappings must exist before alert rules.

```
0. [SaaS, manual]        create a team with slug "migration"
1. create_sentry_projects.py    -> projects created under the "migration" team
2. create_sentry_teams.py       -> real teams created + attached to projects; writes project_team_sync_results.json
3. add_sentry_members.py        -> org members added; writes user_mappings_for_teams.json
4. assign_team_members.py       -> member <-> team assignments
5. migrate_alert_rules.py       -> metric alert rules (reads project_team_sync_results.json)
```

## 5. Dry-Run Mode

Every script accepts `--dry-run`, which logs the exact method/URL/payload it would send without calling the API. Always dry-run first to preview a migration — this was validated against the real export before the live run described in §10.

## 6. Changes Made to the Scripts

1. **`--dry-run` mode** added to all five scripts (`create_sentry_projects.py`, `create_sentry_teams.py`, `add_sentry_members.py`, `assign_team_members.py`, `migrate_alert_rules.py`). Logs the intended method/URL/payload without calling the API.
2. **`check_duplicates.py`** (new script). Scans one or more exports and reports team/project **slug** collisions (which would break a live merge) and **name** collisions (informational). Exits non-zero on slug collisions. Verified against two simulated instances.
3. **`migrate_alert_rules.py` rewritten** to fix several real bugs:
   - Removed the hardcoded `"projects": ["your-project-slug"]` — now maps real projects via `sentry.alertruleprojects` to project slug.
   - `queryType` now comes from the snuba `type` field (previously mistakenly pulled the query string).
   - `eventTypes` derived from `sentry.snubaqueryeventtype`.
   - `timeWindow` converted from seconds to minutes.
   - Real trigger thresholds now read from `sentry.alertruletrigger` (previously a hardcoded default).
   - Owner mapped from the alert rule's `team` field to the new SaaS team id (`team:<id>`).
   - Issue alerts (`sentry.rule`) are now detected and reported as skipped, rather than silently ignored.
4. Fixed `create_sentry_projects.py` to read project fields from `fields` and default platform safely.
5. Added `requirements.txt` and an annotated `README.md`.
6. **Default trigger action** injected in `migrate_alert_rules.py`. SaaS rejects a metric alert whose trigger has no action (`"Each trigger must have an associated action"`), but self-hosted allows this and the export carries no actions. The script now adds a default **email-to-owner-team** action so rules create successfully. Note: original notification actions are still not preserved — flagged for review in §7.
7. **`--send-invite` flag** added to `add_sentry_members.py`. Default is **off**, preserving Dustin's original `sendInvite: false` / `reinvite: false` bulk-provision behavior. When passed, both are set to true so the API attempts to send invitation emails. Caveat: internal-integration tokens may accept `sendInvite: true` but still not deliver emails — in testing, resending the invite from the SaaS UI (as a real user) was the reliable way to trigger delivery. The live test used real Gmail addresses.

## 7. Items That Cannot Be Carried Over (Flag for the supervisor)

- **Issue alerts** (`sentry.rule`) — the scripts only handle **metric** alerts. Issue alerts use a different endpoint/schema and are not migrated. (One default issue alert existed in the test org.)
- **Alert notification actions** — trigger `actions` (email/Slack/PagerDuty targets) are exported but **not** recreated; migrated rules will have thresholds but no notification actions.
- **Member roles** — everyone is created as `member` (owners/managers/admins are flattened). This is a limitation of integration-token invites.
- **Project slugs / DSNs change** — SaaS derives new slugs from names, so DSNs will differ from the source instance. This is expected — teams will need the new DSNs.
- **Duplicate names across instances** — must be resolved before a merged live run. `check_duplicates.py` reports these, but the scripts do not auto-rename.

## 8. SaaS Destination Setup + Live Run

1. In sentry.io, create the destination org `dest-saas-org`.
2. Create a team with **slug `migration`** (required by `create_sentry_projects.py`).
3. **Plan requirement:** member invites need a plan with the invite feature enabled. Start a **Business trial** on the org — the free plan blocks invites (see §10, finding 1).
4. Create an **Internal Integration** (Org Settings → Developer Settings → Custom Integrations) and copy its token. Required scopes: `org:write`, `team:write`, `project:admin`, `member:write`, `alerts:write`. Revoke the token once done.
5. *(Optional, if merging multiple instances)* Run the duplicate check:
   ```bash
   cd "<project-root>/migration-testing"
   .venv/bin/python migration/check_duplicates.py export.json [other-export.json ...]
   ```
6. Run the pipeline (swap in `<TOKEN>`; add `--dry-run` to any command to preview it first):
   ```bash
   cd "<project-root>/migration-testing"
   ORG=dest-saas-org TOKEN=<TOKEN> EX=export.json SC=migration PY=.venv/bin/python
   $PY $SC/create_sentry_projects.py  $TOKEN $ORG $EX
   $PY $SC/create_sentry_teams.py     $TOKEN $ORG $EX
   $PY $SC/add_sentry_members.py      $TOKEN $ORG --export-file $EX
   $PY $SC/assign_team_members.py     $TOKEN $ORG $EX user_mappings_for_teams.json
   $PY $SC/migrate_alert_rules.py     $TOKEN $ORG $EX project_team_sync_results.json
   ```
7. Verify in the SaaS UI: projects (with DSNs), teams & membership, and alert rules.

## 9. Reset / Cleanup Helpers

- **Re-seed source data** (idempotent): re-run `seed_selfhosted.py`.
- **Delete migrated SaaS projects:** `create_sentry_projects.py <token> <org> <export> --delete`.
- **Delete migrated SaaS members:** `add_sentry_members.py <token> <org> --delete member_id_mappings.json`.

## 10. Live Run Results (Org `dest-saas-org`, 2026-07-08)

Ran the pipeline live using an Internal Integration token. Results verified via the API.

| Step | Script | Result |
|---|---|---|
| 1. Projects | `create_sentry_projects.py` | 6/6 created |
| 2. Teams + associations | `create_sentry_teams.py` | 4 teams created, 6/6 project attachments |
| 3. Members | `add_sentry_members.py` | 3/3 added (alice/bob/carol); dorian skipped — already the org owner. Required Business trial (see finding 1) |
| 4. Team assignments | `assign_team_members.py` | 5/5 assigned; 1 skipped (dorian, already owner) |
| 5. Alert rules | `migrate_alert_rules.py` | 5/5 created (after adding default trigger action); 1 issue alert skipped |

**Verified state in SaaS:**

- Teams → projects: `backend` → [checkout-service, payments-api], `frontend` → [mobile-app, web-dashboard], `platform` → [data-pipeline]. (Every project is also attached to `migration`, since step 1 creates them under that team — this is cosmetic and can be detached later.)
- Team membership: `backend` → [alice, carol], `frontend` → [bob, carol], `platform` → [bob]. Matches source.
- 5 metric alert rules present, each scoped to its project and owned by the correct team.

### Two New Findings (in Addition to §7)

1. **Member invites are plan-gated.** The free **Developer** plan has `organizations:invite-members` turned off, so all invites return a 403 regardless of token scopes (`allowMemberInvite`/`openMembership` being true is not sufficient). This is not a script bug — it was resolved by starting a 14-day Business trial on the org, after which invites succeeded immediately. the customer's real destination orgs (paid Business plan) won't hit this.
2. **Metric alert triggers require an action in SaaS** (self-hosted does not enforce this). Handled by injecting a default email-to-owner-team action (see §6.6).
3. **Existing members are re-reported as failures.** Inviting `dorian@sentry.io` returns `"already a member"` (400) because that account owns the org. Harmless, but worth noting when reading the member-step output — it counts as 1 failure in the totals.

## 11. Running It By Hand — What Each Step Does

The scripts are just automation over the Sentry API. Below, each step shows what it reads from the export, what it does in SaaS, and how to do the same thing manually via the UI or a raw API call. Think of this as the "explain it to a colleague" version.

**Prerequisites:** destination org exists, a team slugged `migration` exists, and (for the members step) a plan that allows invites (§8). Reference values used below: `BASE=https://sentry.io/api/0`, `ORG=dest-saas-org`, with `-H "Authorization: Bearer <TOKEN>"` on every call.

**Step 1 — Projects** (`create_sentry_projects.py`)
- Reads each `sentry.project` (name, platform) from the export.
- Creates it under the `migration` team; SaaS derives a new slug from the name (so DSNs are new).
- UI equivalent: **Projects → Create Project** → choose platform, name it, assign team `migration`.
- API equivalent: `POST $BASE/teams/$ORG/migration/projects/` with `{"name": "...", "platform": "..."}`.

**Step 2 — Teams + Associations** (`create_sentry_teams.py`)
- Reads `sentry.team` and `sentry.projectteam`.
- Creates each real team, then attaches teams to the projects from step 1. Records old-team-pk → new-team-id in `project_team_sync_results.json` (used by step 5).
- UI equivalent: **Settings → Teams → Create Team** for each team, then on each project, **Settings → add team**.
- API equivalent: `POST $BASE/organizations/$ORG/teams/` with `{"name","slug"}`, then `POST $BASE/projects/$ORG/<project-slug>/teams/<team-slug>/`.

**Step 3 — Members** (`add_sentry_members.py`)
- Reads active `sentry.organizationmember` records (using `user_email`). Writes `user_mappings_for_teams.json` (old-member-pk → new-member-id) for step 4.
- Everyone is added at the `member` role; no invite email is sent by default.
- UI equivalent: **Settings → Members → Invite Member** → email, role Member.
- API equivalent: `POST $BASE/organizations/$ORG/members/` with `{"email","orgRole":"member","sendInvite":false}`.

**Step 4 — Team Membership** (`assign_team_members.py`)
- Reads `sentry.organizationmemberteam` and the mapping file from step 3, then adds each member to their teams.
- UI equivalent: **Settings → Members → (member) → Teams → add**, or **Settings → Teams → (team) → Members → add**.
- API equivalent: `POST $BASE/organizations/$ORG/members/<member-id>/teams/<team-slug>/`.

**Step 5 — Alert Rules** (`migrate_alert_rules.py`)
- Reads `sentry.alertrule` plus its `sentry.snubaquery` (metric/dataset/window), `sentry.alertruleprojects` (which project), `sentry.alertruletrigger` (thresholds), `sentry.snubaqueryeventtype`, and the team mapping from step 2 (for the owner). Only metric alerts are handled — issue alerts (`sentry.rule`) are skipped.
- Since each trigger must have an action under SaaS rules, a default email-to-owner-team action is added.
- UI equivalent: **Alerts → Create Alert → Metric alert** → pick the project, metric (`count()`), time window, threshold, add a notification action, set the owner team.
- API equivalent: `POST $BASE/organizations/$ORG/alert-rules/` with `{name, dataset, query, aggregate, timeWindow, queryType, eventTypes, thresholdType, triggers:[{label,alertThreshold,actions:[...]}], projects:[slug], owner:"team:<id>"}`.

**Ordering logic:** step 1 → 2 (teams attach to existing projects), step 3 → 4 (assignment needs the member mapping), step 2 → 5 (alert owner needs the team mapping). Always run `--dry-run` first to preview.
