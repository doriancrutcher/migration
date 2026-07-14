# Duplicates Report Tool — Verification Evidence

**Tool:** `migration/duplicates_report.py`
**Purpose:** the first tool in the migration suite. Before consolidating several self-hosted orgs into
one SaaS org, it reads one JSON export per org and reports cross-org collisions that would break a merged
migration — so they can be resolved (rename / merge / drop) up front.

**Source:** JSON exports only (offline). No live instance. See `DECISIONS.md` D7.

---

## What it detects

| Signal | Severity | Why |
|---|---|---|
| Project **name** collision | **HARD** | Projects are created by name; SaaS derives the slug from the name, so same-name projects across orgs clash. |
| Team **slug** collision | **HARD** | Teams are created with an explicit slug, which must be unique in the merged org. |
| Team **name** collision + **membership diff** | info | Same team name in two orgs with a *different roster* — a real merge hazard (who ends up on the team?). |
| Project **slug** collision | info | Slug isn't sent on project create; listed for awareness. |
| Similar org names | info | Surfaces org families (Dor-Org1 / 2 / 3). |

Exit code is non-zero (2) when any HARD collision exists.

---

## Test data (planted collisions)

Three orgs were seeded into the self-hosted instance (`migration-testing/seed_multi_org.py`) and each
exported separately with `export organizations --filter-org-slugs <slug>`.

| Org | Teams (roster) | Projects |
|---|---|---|
| **Dor-Org1** | `fe`/Frontend = dorian, derek, daniel · `be`/Backend = dorian | Payments API, Checkout Service, Web Dashboard |
| **Dor-Org2** | `data`/Data = sam | **Payments API**, **Checkout Service** (dupes of Org1), Analytics |
| **Dor-Org3** | `fe`/Frontend = **mandy, mikey, mitch** (same team name/slug as Org1, different people) | Internal Tools |

Planted expectations:
- **Project-name HARD**: `Payments API` and `Checkout Service` appear in both Org1 and Org2.
- **Team-slug HARD** with **different rosters**: `fe` in Org1 (dorian/derek/daniel) vs Org3 (mandy/mikey/mitch).
- **Similar names**: dor-org1 / dor-org2 / dor-org3.

---

## Command

```bash
cd migration-testing/dupe-test-exports
python3 ../migration/duplicates_report.py dor-org1.json dor-org2.json dor-org3.json --out duplicate_report.json
```

> Tip: add `--html` to also produce a self-contained `duplicate_report.html` (opens offline in any
> browser) - a readable, shareable view of the same report with severity-colored sections and the
> per-team membership diff.

## Output

```
Loaded dor-org1.json: org 'dor-org1' (2 teams, 3 projects)
Loaded dor-org2.json: org 'dor-org2' (1 teams, 3 projects)
Loaded dor-org3.json: org 'dor-org3' (1 teams, 1 projects)

=== PROJECT NAME collisions (HARD - derived slug will clash) ===
  'checkout service' in 2 orgs: dor-org1 (slug 'checkout-service', name 'Checkout Service'), dor-org2 (slug 'checkout-service', name 'Checkout Service')   [HARD]
  'payments api' in 2 orgs: dor-org1 (slug 'payments-api', name 'Payments API'), dor-org2 (slug 'payments-api', name 'Payments API')   [HARD]

=== TEAM SLUG collisions (HARD - slug must be unique) ===
  'fe' in dor-org1, dor-org3  [HARD; DIFFERENT rosters]
      common: (none)
      dor-org1: members [daniel@example.com, derek@example.com, dorian@example.com] | unique [daniel@example.com, derek@example.com, dorian@example.com]
      dor-org3: members [mandy@example.com, mikey@example.com, mitch@example.com] | unique [mandy@example.com, mikey@example.com, mitch@example.com]

=== TEAM NAME collisions (informational - watch roster diffs) ===
  'Frontend' in dor-org1, dor-org3  [info; DIFFERENT rosters]
      common: (none)
      dor-org1: members [daniel@example.com, derek@example.com, dorian@example.com] | unique [daniel@example.com, derek@example.com, dorian@example.com]
      dor-org3: members [mandy@example.com, mikey@example.com, mitch@example.com] | unique [mandy@example.com, mikey@example.com, mitch@example.com]

=== PROJECT SLUG collisions (informational - slug not sent on create) ===
  'checkout-service' in 2 orgs: dor-org1 (slug 'checkout-service', name 'Checkout Service'), dor-org2 (slug 'checkout-service', name 'Checkout Service')   [info]
  'payments-api' in 2 orgs: dor-org1 (slug 'payments-api', name 'Payments API'), dor-org2 (slug 'payments-api', name 'Payments API')   [info]

=== SIMILAR ORG NAMES (informational) ===
  'dor-org1' ~ 'dor-org2'  (ratio 0.875)
  'dor-org1' ~ 'dor-org3'  (ratio 0.875)
  'dor-org2' ~ 'dor-org3'  (ratio 0.875)

================================================================
Summary: 3 orgs | HARD collisions: 3 (project-name 2, team-slug 1) | info: team-name 1, project-slug 2, similar-names 3
Wrote duplicate_report.json

FOUND 3 HARD collision group(s) that will break a merged migration. Resolve (rename/merge/drop) before migrating.
```

Exit code: **2** (hard collisions found — as expected).

---

## Result

Every planted collision was surfaced correctly:

| Planted | Detected |
|---|---|
| Payments API in Org1 + Org2 | ✅ project-name HARD |
| Checkout Service in Org1 + Org2 | ✅ project-name HARD |
| Team `fe` in Org1 vs Org3, different people | ✅ team-slug HARD, `common: (none)`, each org's roster listed as unique |
| Frontend name reused across orgs | ✅ team-name info + roster diff |
| dor-org1/2/3 look alike | ✅ similar org names (0.875) |
| Non-zero exit on hard collisions | ✅ exit 2 |

The membership diff clearly shows the "same team name, different roster" case: `fe` has **no common
members** between Org1 and Org3, and each org's three members are listed as unique to that org.
