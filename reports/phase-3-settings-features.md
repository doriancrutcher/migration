# Phase 3: Settings Features — Beyond Core Scope

**Status: In progress (3 features shipped + 1 built, gated).** With the P0 core proven in Phase 2, we
began delivering the remaining relocation-checklist data types one at a time, each as its own feature
branch merged into `main` via PR. Three are done and verified live into `dest-saas-org`:
**organization settings**, **project settings**, and **data scrubbers**. A fourth,
**member roles**, is code-complete and dry-run-proven but **gated on a supervisor decision** about the
credential it requires (see §3).

> **Read §3 first.** Several of these features can make org-wide or security-sensitive changes. §3 is a
> risk register that flags which features need supervisor sign-off before running against a real org.

## Goal

Extend the migration tooling past the P0 core (Projects, Teams & Membership, Alert Rules) toward the
full relocation checklist — organization settings, user accounts/options, project settings, team
settings, alerts, crons, dashboards, data scrubbers, repositories, and saved searches — without
disturbing the frozen core checkpoint.

Scripts repo: `migration-testing/migration` (fork of
[github.com/dgbailey/migration](https://github.com/dgbailey/migration)).

## 1. What Changed Since Phase 2

**A `main` trunk + one feature branch per data type.** The core scope is frozen at tag `v1.0-core`
(also on the `phase-2-core` branch). `main` is the integration trunk started from that checkpoint;
`master` is a pristine mirror of upstream, used only to pull Dustin's updates. Each new data type is a
`feat/<type>` branch → one PR into `main`. Because features only *add* files, conflicts are near zero.

**A second data source.** Phase 2 was export-driven (parse `export.json` → POST to SaaS). But the
relocation export doesn't carry every model — org options, project options, dashboards, monitors,
repos, and saved searches are absent. So we added a live, read-only client for the running self-hosted
REST API:

- `selfhosted_source.py` — GET-only client (auth, RFC5988 cursor pagination, typed helpers
  `get_org`, `get_projects`, `get_project`, `get_members`). Requires a self-hosted read token
  (`org:read`, `project:read`, `member:read`).

```
Phase 2:  self-hosted --(export)--> export.json --(scripts + SaaS token)--> sentry.io
Phase 3:  self-hosted --(live GET via selfhosted_source.py)--> (whitelist) --(PUT + SaaS token)--> sentry.io
```

Each Phase 3 feature follows the same shape: **read source live → copy a whitelist → PUT to SaaS →
verify with a GET-back → write a `*_results.json`**, and every script supports `--dry-run`.

## 2. Features Delivered

### 2.1 Organization settings — `migrate_org_settings.py` (PR #1)

Migrates org governance + privacy settings via `PUT /organizations/{org}/`.

- **Whitelist (11):** `defaultRole`, `openMembership`, `allowJoinRequests`, `eventsMemberAdmin`,
  `alertsMemberWrite`, `attachmentsRole`, `debugFilesRole`, `enhancedPrivacy`, `allowSharedIssues`,
  `scrapeJavaScript`, `isEarlyAdopter`.
- **Skipped:** `require2FA` (would risk locking out members without 2FA).
- **Deferred:** data-scrubbing fields → handled by the data-scrubbers feature (2.3).
- SaaS token scope: `org:write`.

### 2.2 Project settings — `migrate_project_settings.py` (PR #2)

Migrates per-project general settings via `PUT /projects/{org}/{proj}/`.

- **Whitelist (9):** `resolveAge`, `allowedDomains`, `scrapeJavaScript`, `verifySSL`, `subjectPrefix`,
  `subjectTemplate`, `defaultEnvironment`, `highlightTags`, `highlightContext`.
- **Project matching (greenfield):** Phase 2 reassigned SaaS project slugs but preserved names, so the
  script pairs source → destination projects by **name** (case-insensitive) and PUTs to the
  destination's own slug. Projects with no name match are skipped and reported — never guessed.
- **Deferred:** data-scrubbing fields → 2.3. **Skipped:** identity/advanced/risky fields.
- SaaS token scope: `project:write`.

### 2.3 Data scrubbers — `migrate_data_scrubbers.py` (PR #3)

Migrates the standard data-scrubbing settings deferred by 2.1 and 2.2, at **both** org and project
level (`--org-only` / `--projects-only` to scope).

- **Whitelist (6, both levels):** `dataScrubber`, `dataScrubberDefaults`, `sensitiveFields`,
  `safeFields`, `scrubIPAddresses`, `storeCrashReports`.
- **Excluded (recorded, not dropped):** advanced custom-PII `relayPiiConfig` and `trustedRelays` —
  complex, relay-dependent, higher-risk (see DECISIONS.md D5).
- Projects matched by name, reusing 2.2's logic.
- SaaS token scopes: `org:write` + `project:write`.

### 2.4 Member roles — `migrate_member_roles.py` (PR #4, built — GATED on §3 review)

Restores each member's real org role after the core invite step (core invites everyone as `member`
because integration tokens are capped there). Reads live self-hosted members, matches to SaaS members
by **email**, and PUTs the mapped `orgRole` via `PUT /organizations/{org}/members/{id}/`, then verifies.

- **Role map:** `member`/`admin`/`manager` pass through 1:1; a source **`owner` → `manager`** to avoid
  minting extra org owners.
- **Owner protection:** the tool never modifies an account that is currently an **owner** on SaaS
  (including the account running the migration).
- **Dry-run proven:** correctly plans `member → admin` and `member → manager` and reports the source
  owner as protected.
- **BLOCKED live** by a permissions constraint — see §3. SaaS token scope needed: **`member:admin`**,
  and the credential must be authorized to grant those roles.

## 3. Supervisor Review Flags — Potentially Damaging Changes

Every feature is whitelist-only, dry-run-first, and verified after — but a migration *write* is still a
change to a live org. The features below can broaden access, weaken privacy, or escalate privilege if
run with the wrong source data or credentials. **These should get supervisor sign-off before running
against a real customer org.**

| Feature | What it can change | Worst-case if misapplied | Risk | Supervisor sign-off |
|---|---|---|---|---|
| **Member roles (2.4)** | Grants `admin`/`manager` org roles; requires an **Org-Admin + Member-Admin** credential | **Privilege escalation**; the migration credential itself is very powerful and, if leaked, could take over the org | **CRITICAL** | **Required — blocking** |
| **Data scrubbers (2.3)** | Enables/**disables** PII scrubbing (`dataScrubber`, `scrubIPAddresses`, `sensitiveFields`, …) org- and project-wide | If the source has scrubbing **off**, migration turns it off on SaaS → **PII (IPs, secrets) stored/exposed** | **HIGH** | **Required** |
| **Org settings (2.1)** | Org-wide access/privacy (`openMembership`, `allowJoinRequests`, `defaultRole`, `enhancedPrivacy`, `allowSharedIssues`, `scrapeJavaScript`) | **Broadens who can join / what they can see** for the whole org; can lower the default member role | **HIGH** | **Required** |
| **Collision / overwrite (future §7)** | On brownfield orgs, could `overwrite`/`merge` objects the customer already uses | **Data loss / clobbering** existing customer config | **HIGH** | **Required** (policy sign-off per data type) |
| **Project settings (2.2)** | Per-project surface (`allowedDomains`, `verifySSL`, `scrapeJavaScript`) | Loosens per-project ingest/security surface | **Medium** | **Recommended** (spot-check security fields) |
| Core: Projects / Teams / Alerts | Creates new objects (additive) | Duplicate/partial objects; no destructive writes | Low | Standard |

**The one to escalate now — member roles (D6).** Setting an elevated org role is rejected for a normal
internal-integration token:

```
400 {"orgRole":["You do not have permission to set that org-level role"]}
```

To make it work, the migration credential must be granted **Organization: Admin + Member: Admin** (or
be an owner's user auth token). Handing a migration tool that much power is itself a security concern.
**Open question for the supervisor:** what is the least-privilege, approved credential model for
role elevation? Until that's answered, `feat/member-roles` stays gated — code and dry-run only.

**Guardrails already in place across all features:** whitelist-only writes (nothing outside an explicit
field list), mandatory `--dry-run` preview, per-target GET-back verification, results files for audit,
owner protection (2.4), and intentional exclusions logged in DECISIONS.md (never silently dropped).

## 4. Scope Decisions (see `migration-testing/migration/DECISIONS.md`)

Every intentional exclusion/deferral is logged so it can be revisited, never silently dropped:

- **D6** — Member roles: `owner → manager`, destination owners protected; role elevation needs a
  high-privilege credential (**flagged for supervisor review**, see §3).
- **D5** — Data scrubbers: standard fields only; advanced custom-PII (`relayPiiConfig`, `trustedRelays`)
  deferred.
- **D4** — Project matching by name (greenfield assumption); brownfield collisions deferred to a
  hardening milestone (§7).
- **D3** — `require2FA` not migrated (lockout risk).
- **D2** — Member roles flattened to `member` at invite (core limitation) → addressed by `feat/member-roles`.
- **D1** — Metric alerts only; issue alerts and notification actions not migrated (core limitation).

## 5. Testing Method

Each feature is validated the same way (commands in §8):

1. **Dry-run** — logs the exact PUT method/URL/payload and the match table; touches nothing.
2. **Live run** — applies the whitelist, then does a **GET-back verification** per target (compares
   each written field to the source; reports any mismatch).
3. **Independent API diff** — a separate read of self-hosted vs SaaS for the whitelisted fields, to
   confirm the two systems agree (not just re-reading our own results file).
4. **Change-detection** — flip a value in SaaS, re-run, confirm it's reset to the source value (proves
   the script actually writes).
5. **Idempotency** — run twice; the second run is still all-pass with no errors.
6. **Exclusion check** (data scrubbers) — confirm `relayPiiConfig` / `trustedRelays` are left untouched.

## 6. Live Results (Org `dest-saas-org`)

Source: self-hosted `migration-test-org` (6 projects). All runs dry-run-previewed, then applied live,
then verified.

| Feature | Result |
|---|---|
| Org settings | 11/11 whitelisted fields verified matching source |
| Project settings | 6/6 projects matched by name, 0 unmatched; per-project verification passed |
| Data scrubbers | Org + 6/6 projects verified passed; advanced fields correctly excluded |
| Member roles | Dry-run correct (member→admin, member→manager, owner protected); **live blocked** on credential permission (§3) |

**Match-by-name proof.** A 7th self-hosted project (`android`) with no SaaS counterpart was correctly
reported as `[UNMATCHED]` and skipped, rather than being force-matched — confirming the greenfield
matching behavior.

**Scrubber round-trip proof.** Setting `scrubIPAddresses=on`, `sensitiveFields=[credit_card]`,
`safeFields=[order_id]` on self-hosted `mobile-app`, then running the tool, produced those exact values
on the SaaS project (verified in the UI under Settings → Security & Privacy).

## 7. Hardening Before Real Customers: Collision Pre-flight

Every Phase 3 feature assumes a **greenfield** destination — a fresh SaaS org we control. Real
customers may migrate into an **existing, in-use** org, where names/slugs collide with objects they
already rely on. Planned as `feat/collision-preflight`:

- A `--dry-run` pre-flight report per data type ("these already exist in the destination").
- A configurable per-type policy: `skip` / `rename` / `merge` / `overwrite` / `fail`.
- Provenance tracking so re-runs only touch migration-created objects.
- A safe default of report-only / skip for org-level and security settings.

**Open question for the supervisor:** are the customer's migrations always into a fresh org, or sometimes brownfield?
That answer decides how much of this milestone we build before the tool is customer-ready. Note the
overwrite/merge policies here are themselves a **supervisor-review item** (§3) — they can clobber
existing customer config.

## 8. How to Run the Phase 3 Scripts

Prereqs: the self-hosted stack running, a self-hosted **read** token (created on the instance via
`django shell`), and a SaaS token with the scopes noted per feature. Tokens are supplied at run time
and never committed. Commands use the venv at `migration-testing/.venv`.

```bash
cd "<project-root>/migration-testing/migration"
SAAS=<saas_token>     # scopes depend on feature (see per-feature notes)
SH=<selfhosted_read_token>
ORG=dest-saas-org
PY=.venv/bin/python

# Always dry-run first, then drop --dry-run for the live run.
$PY migrate_org_settings.py     "$SAAS" "$ORG" --source-token "$SH" --dry-run
$PY migrate_project_settings.py "$SAAS" "$ORG" --source-token "$SH" --dry-run
$PY migrate_data_scrubbers.py   "$SAAS" "$ORG" --source-token "$SH" --dry-run   # or --org-only / --projects-only
$PY migrate_member_roles.py     "$SAAS" "$ORG" --source-token "$SH" --dry-run   # live requires member:admin (§3)
```

Each writes a results file (`org_settings_migration_results.json`,
`project_settings_migration_results.json`, `data_scrubbers_migration_results.json`,
`member_roles_migration_results.json`) — all gitignored as they may contain org data.

## 9. Feature Checklist Status

| Relocation item | Status |
|---|---|
| All organization settings | Done (2.1) |
| Projects and their settings | Done (core + 2.2) |
| Enabled data scrubbers | Done (2.3) |
| User accounts & options (roles/notifications) | Members in core; roles built (2.4) but **gated on §3 review**; notifications pending |
| Teams and their settings | Partial — teams in core; only name/slug/status carried (verify-only) |
| Alerts | Partial — metric alerts in core; issue alerts pending |
| Crons | Planned — `feat/monitors` |
| Dashboards | Planned — `feat/dashboards` |
| Repositories | Planned — `feat/repos` (integration-gated) |
| Recent & saved searches | Planned — `feat/saved-searches` |

## 10. Next Steps

1. **Escalate `feat/member-roles` (§3)** — get supervisor sign-off on the least-privilege credential
   for role elevation before any live run against a real org.
2. **Decide brownfield scope** — the supervisor to weigh in on fresh-org-only vs. existing-org migrations (§7),
   including the overwrite/merge policy review.
3. **Content features** — monitors, dashboards, repos, saved searches (all live-API sourced).
4. **Guided `migrate.py` wizard** — one script that prompts for source/dest + tokens + dry-run and
   orchestrates all feature modules in dependency order (built last).
5. **Repeat for v24** — confirm the same pipeline against a v24 self-hosted source.
