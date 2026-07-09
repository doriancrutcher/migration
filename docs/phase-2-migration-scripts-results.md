# Phase 2: Migration Scripts — Findings & Runbook

**Status: COMPLETE. Full live run into `dorian-v25-migration` verified — Projects, Teams (+ associations),
Members, Team membership, and Alert Rules all migrated (see §10). Membership required a Business trial on
the destination org to lift the free-plan invite gate.**

Goal: confirm the run order of Dustin's migration scripts and that they carry the P0 data
(Projects, Teams & Membership, Alert Rules) from **self-hosted v25 -> SaaS**, patching gaps found
along the way. Scripts repo: `migration-testing/migration` (fork of
[github.com/dgbailey/migration](https://github.com/dgbailey/migration), annotated + fixed).

## 0. Where everything lives

- **Deliverables + scripts (this project):** `NVIDIA Migration Project/migration-testing/`
  - `migration/` — the annotated + fixed scripts, `README.md`, `requirements.txt`
  - `export.json` — the self-hosted export
  - `seed_selfhosted.py` — idempotent source-data seeder
  - `.venv/` — Python env (`requests`)
  - `dryrun-out/` — dry-run output + mapping files (scratch)
- **Docker infra (left in home dir on purpose):** `~/sentry-migration-testing/self-hosted/`
  (the running Sentry stack; data lives in Docker volumes, not this folder)

Commands below assume you first `cd` into the `migration-testing` folder:

```bash
cd "$HOME/Documents/Claude/Projects/NVIDIA Migration Project/migration-testing"
```

## 1. Environment

- Self-hosted Sentry **25.6.2**, native **arm64** on Apple Silicon (see `phase-1-self-hosted-v25-setup.md`).
- Source org: `migration-test-org`.
- Scripts run with the venv at `migration-testing/.venv` (`requests` only).

## 2. Test data seeded into self-hosted (source)

Created via an idempotent ORM script (`migration-testing/seed_selfhosted.py`, run through
`docker compose run --rm -T web django shell`):

| Type | Count | Detail |
|------|-------|--------|
| Teams | 4 | backend, frontend, platform (+ default `sentry`) |
| Projects | 6 | checkout-service, payments-api (backend); web-dashboard, mobile-app (frontend); data-pipeline (platform); (+ default `internal`) |
| Members | 4 | dorian (owner), alice, bob, carol (active users) with team memberships |
| Metric alert rules | 5 | one per non-default project, each owned by the project's team |

## 3. Export (self-hosted side)

The relocation-style export produces the exact `model`/`pk`/`fields` JSON the scripts consume:

```bash
MIG="$HOME/Documents/Claude/Projects/NVIDIA Migration Project/migration-testing"
cd ~/sentry-migration-testing/self-hosted
docker compose run --rm -T -v "$MIG:/export" \
  web export organizations /export/export.json \
  --filter-org-slugs migration-test-org --no-prompt
```

Verified the export contains all required models: `sentry.project` (6), `sentry.team` (4),
`sentry.projectteam` (6), `sentry.organizationmember` (4, with `user_email`/`user_is_active`),
`sentry.organizationmemberteam` (6), `sentry.alertrule` (5), `sentry.snubaquery` (5), plus the
supporting `sentry.alertruleprojects`, `sentry.alertruletrigger`, `sentry.snubaqueryeventtype`.

## 4. Confirmed run order

There are hard dependencies (a team named `migration` must pre-exist in SaaS; projects before teams;
member mappings before team assignment; team mappings before alert rules):

```
0. [SaaS, manual] create a team with slug "migration"
1. create_sentry_projects.py   -> projects created under the "migration" team
2. create_sentry_teams.py      -> real teams + attach to projects; writes project_team_sync_results.json
3. add_sentry_members.py       -> org members; writes user_mappings_for_teams.json
4. assign_team_members.py      -> member<->team assignments
5. migrate_alert_rules.py      -> metric alert rules (reads project_team_sync_results.json)
```

## 5. Dry-run mode

Every script accepts `--dry-run`, which logs the exact method/URL/payload it would send without calling
the API. Always dry-run first to preview a migration. (Validated against the real export before the live
run below.)

## 6. Changes made to the scripts

1. **`--dry-run` mode** added to all five scripts (`create_sentry_projects.py`, `create_sentry_teams.py`,
   `add_sentry_members.py`, `assign_team_members.py`, `migrate_alert_rules.py`). Logs the intended
   method/URL/payload without calling the API.
2. **`check_duplicates.py`** (new): scans one or more exports and reports team/project **slug**
   collisions (which would break a live merge) and **name** collisions (informational). Exits non-zero
   on slug collisions. Verified against two simulated instances.
3. **`migrate_alert_rules.py` rewritten** to fix real bugs:
   - Removed the hardcoded `"projects": ["your-project-slug"]`; now maps real projects via
     `sentry.alertruleprojects` -> project slug.
   - `queryType` now comes from the snuba `type` field (was mistakenly the query string).
   - `eventTypes` derived from `sentry.snubaqueryeventtype`.
   - `timeWindow` converted from seconds to minutes.
   - Real trigger thresholds read from `sentry.alertruletrigger` (was a hardcoded default).
   - Owner mapped from the alert rule's `team` field -> new SaaS team id (`team:<id>`).
   - Issue alerts (`sentry.rule`) are detected and reported as skipped (not silently ignored).
4. Fixed `create_sentry_projects.py` to read project fields from `fields` and default platform safely.
5. Added `requirements.txt` and an annotated `README.md`.
6. **Default trigger action** injected in `migrate_alert_rules.py`: SaaS rejects a metric alert whose
   trigger has no action (`"Each trigger must have an associated action"`), but self-hosted allows it and
   the export carries no actions. The script now adds a default **email-to-owner-team** action so rules
   create successfully. Original notification actions are still not preserved (flag for review).
7. **`--send-invite` flag** added to `add_sentry_members.py`. Default is **off** (preserves Dustin's
   original `sendInvite: false` / `reinvite: false` bulk-provision behavior). When passed, both are set
   true so the API attempts to send invitation emails. Caveat: internal-integration tokens may accept
   `sendInvite: true` but still not deliver emails — in testing, resending the invite from the SaaS UI
   (as a real user) was the reliable way to trigger delivery. Live test used real Gmail addresses.

## 7. Items that cannot be carried over (flag for Chris)

- **Issue alerts** (`sentry.rule`) — the scripts only handle **metric** alerts. Issue alerts use a
  different endpoint/schema and are not migrated. (1 default issue alert existed in the test org.)
- **Alert notification actions** — trigger `actions` (email/Slack/PagerDuty targets) are exported but
  NOT recreated; migrated rules will have thresholds but no notification actions.
- **Member roles** — everyone is created as `member` (owners/managers/admins are flattened). This is a
  limitation of integration-token invites.
- **Project slugs / DSNs change** — SaaS derives new slugs from names, so DSNs differ (expected; teams
  will need the new DSNs).
- **Duplicate names across instances** — must be resolved before a merged live run (`check_duplicates.py`
  reports them; the scripts do not auto-rename).

## 8. SaaS destination setup + live run

1. In sentry.io, create the destination org `dorian-v25-migration`.
2. Create a team with **slug `migration`** (required by `create_sentry_projects.py`).
3. **Plan:** member invites need a plan with the invite feature — start a **Business trial** on the org
   (free plan blocks invites; see §10 finding 1).
4. Create an **Internal Integration** (Org Settings → Developer Settings → Custom Integrations) and copy
   its token. Scopes: `org:write`, `team:write`, `project:admin`, `member:write`, `alerts:write`.
   Revoke the token when done.
5. (Optional, if merging multiple instances) duplicate check:
   ```bash
   cd "$HOME/Documents/Claude/Projects/NVIDIA Migration Project/migration-testing"
   .venv/bin/python migration/check_duplicates.py export.json [other-export.json ...]
   ```
6. Run the pipeline (swap `<TOKEN>`; add `--dry-run` to any command to preview it first):
   ```bash
   cd "$HOME/Documents/Claude/Projects/NVIDIA Migration Project/migration-testing"
   ORG=dorian-v25-migration TOKEN=<TOKEN> EX=export.json SC=migration PY=.venv/bin/python
   $PY $SC/create_sentry_projects.py  $TOKEN $ORG $EX
   $PY $SC/create_sentry_teams.py     $TOKEN $ORG $EX
   $PY $SC/add_sentry_members.py      $TOKEN $ORG --export-file $EX
   $PY $SC/assign_team_members.py     $TOKEN $ORG $EX user_mappings_for_teams.json
   $PY $SC/migrate_alert_rules.py     $TOKEN $ORG $EX project_team_sync_results.json
   ```
7. Verify in the SaaS UI: projects (+ DSNs), teams & membership, alert rules.

## 9. Reset / cleanup helpers

- Re-seed source data (idempotent): re-run `seed_selfhosted.py`.
- Delete migrated SaaS projects: `create_sentry_projects.py <token> <org> <export> --delete`.
- Delete migrated SaaS members: `add_sentry_members.py <token> <org> --delete member_id_mappings.json`.

## 10. Live run results (org `dorian-v25-migration`, 2026-07-08)

Ran the pipeline live with an Internal Integration token. Verified via the API.

| Step | Script | Result |
|------|--------|--------|
| 1. Projects | `create_sentry_projects.py` | 6/6 created |
| 2. Teams + associations | `create_sentry_teams.py` | 4 teams created, 6/6 project attachments |
| 3. Members | `add_sentry_members.py` | 3/3 added (alice/bob/carol); dorian skipped = already the org owner. Required Business trial (see finding 1) |
| 4. Team assignments | `assign_team_members.py` | 5/5 assigned; 1 skipped (dorian, already owner) |
| 5. Alert rules | `migrate_alert_rules.py` | 5/5 created (after adding default trigger action); 1 issue alert skipped |

Verified state in SaaS:
- Teams -> projects: `backend`->[checkout-service, payments-api], `frontend`->[mobile-app, web-dashboard],
  `platform`->[data-pipeline]. (Every project is also attached to `migration` because step 1 creates them
  under that team — cosmetic; can be detached later.)
- Team membership: `backend`->[alice, carol], `frontend`->[bob, carol], `platform`->[bob]. Matches source.
- 5 metric alert rules present, each scoped to its project and owned by the correct team.

### Two new findings (in addition to §7)

1. **Member invites are plan-gated.** The free **Developer** plan has `organizations:invite-members` off,
   so ALL invites 403 regardless of token scopes (`allowMemberInvite`/`openMembership` being true is not
   sufficient). NOT a script bug. **Resolved by starting a 14-day Business trial on the org**, after which
   invites succeeded immediately. NVIDIA's real destination orgs (paid Business) won't hit this.
2. **Metric alert triggers require an action in SaaS** (self-hosted does not). Handled by injecting a
   default email-to-owner-team action (see §6.6).
3. **Existing members are re-reported as failures.** Inviting `dorian@sentry.io` returns
   `"already a member"` (400) because that account owns the org. Harmless, but worth noting when reading
   the member-step output (it counts as 1 failure).

## 11. Running it by hand — what each step does

The scripts are just automation over the Sentry API. Each step below shows what it reads from the export,
what it does in SaaS, and how to do the same thing manually (UI or raw API). This is the "explain it to a
colleague" version. Prereqs: destination org exists, a team slugged `migration` exists, and (for members) a
plan that allows invites (§8). `BASE=https://sentry.io/api/0`, `ORG=dorian-v25-migration`,
`-H "Authorization: Bearer <TOKEN>"` on every call.

**Step 1 — Projects** (`create_sentry_projects.py`)
- Reads each `sentry.project` (name, platform) from the export.
- Creates it under the `migration` team; SaaS derives a new slug from the name (so DSNs are new).
- UI: **Projects → Create Project** → choose platform, name it, assign team `migration`.
- API: `POST $BASE/teams/$ORG/migration/projects/` with `{"name": "...", "platform": "..."}`.

**Step 2 — Teams + associations** (`create_sentry_teams.py`)
- Reads `sentry.team` and `sentry.projectteam`.
- Creates each real team, then attaches teams to the projects from step 1. Records old-team-pk → new-team-id
  in `project_team_sync_results.json` (used by step 5).
- UI: **Settings → Teams → Create Team** for each; then on each project **Settings → add team**.
- API: `POST $BASE/organizations/$ORG/teams/` `{"name","slug"}`, then
  `POST $BASE/projects/$ORG/<project-slug>/teams/<team-slug>/`.

**Step 3 — Members** (`add_sentry_members.py`)
- Reads active `sentry.organizationmember` (uses `user_email`). Writes `user_mappings_for_teams.json`
  (old-member-pk → new-member-id) for step 4.
- Everyone is added at the `member` role; no invite email is sent.
- UI: **Settings → Members → Invite Member** → email, role Member.
- API: `POST $BASE/organizations/$ORG/members/` `{"email","orgRole":"member","sendInvite":false}`.

**Step 4 — Team membership** (`assign_team_members.py`)
- Reads `sentry.organizationmemberteam` and the mapping file from step 3, then puts each member on their teams.
- UI: **Settings → Members → (member) → Teams → add**, or **Settings → Teams → (team) → Members → add**.
- API: `POST $BASE/organizations/$ORG/members/<member-id>/teams/<team-slug>/`.

**Step 5 — Alert rules** (`migrate_alert_rules.py`)
- Reads `sentry.alertrule` + its `sentry.snubaquery` (metric/dataset/window), `sentry.alertruleprojects`
  (which project), `sentry.alertruletrigger` (thresholds), `sentry.snubaqueryeventtype`, and the team mapping
  from step 2 (for the owner). Only metric alerts; issue alerts (`sentry.rule`) are skipped.
- Each trigger must have an action (SaaS rule), so a default email-to-owner-team action is added.
- UI: **Alerts → Create Alert → Metric alert** → pick the project, metric (`count()`), time window, threshold,
  add a notification action, set the owner team.
- API: `POST $BASE/organizations/$ORG/alert-rules/` with `{name, dataset, query, aggregate, timeWindow,
  queryType, eventTypes, thresholdType, triggers:[{label,alertThreshold,actions:[...]}], projects:[slug],
  owner:"team:<id>"}`.

Order matters: 1→2 (teams attach to existing projects), 3→4 (assignment needs the member mapping),
2→5 (alert owner needs the team mapping). Always `--dry-run` first to preview.
