# Phase 3: Verification Evidence — Inputs & Outputs

How each Phase 3 feature was tested and verified. For every feature: the exact command run (input) and
the observed result (output), captured from live runs against the SaaS test org.

- **Source (self-hosted):** `migration-test-org` (6 real projects + a 7th, `android`, with no SaaS twin)
- **Destination (SaaS):** `dest-saas-org`
- **Method:** dry-run preview → live run with per-target GET-back verification → independent API diff →
  change-detection. Every write is followed by a read that compares each field to the source.

Common setup (tokens supplied at run time, never committed):

```bash
cd "<project-root>/migration-testing/migration"
SAAS=<saas_token>            # org:write and/or project:write
SH=<selfhosted_read_token>   # org:read, project:read
ORG=dest-saas-org
PY=.venv/bin/python
```

---

## 1. Organization settings (`migrate_org_settings.py`)

**Input**
```bash
$PY migrate_org_settings.py "$SAAS" "$ORG" --source-token "$SH" --dry-run   # preview
$PY migrate_org_settings.py "$SAAS" "$ORG" --source-token "$SH"             # live
```

**Output (live)** — all 11 whitelisted fields written and verified against the source:

```
Applying 11 whitelisted settings: {defaultRole, openMembership, allowJoinRequests,
  eventsMemberAdmin, alertsMemberWrite, attachmentsRole, debugFilesRole, enhancedPrivacy,
  allowSharedIssues, scrapeJavaScript, isEarlyAdopter}
Deferred to feat/data-scrubbers (not applied): [dataScrubber, dataScrubberDefaults, ...]
Skipped (not applied): [require2FA]
Verification passed: all whitelisted fields on SaaS match the source.
```

**Result:** 11/11 fields verified. `require2FA` skipped and scrubbers deferred — both recorded, not
dropped.

---

## 2. Project settings (`migrate_project_settings.py`)

**Input**
```bash
$PY migrate_project_settings.py "$SAAS" "$ORG" --source-token "$SH" --dry-run
$PY migrate_project_settings.py "$SAAS" "$ORG" --source-token "$SH"
```

**Output (live summary)** — matched by name; the un-migrated `android` project is correctly reported,
not force-matched:

```
================================================================
Summary [LIVE]: matched 6, unmatched 1
  Checkout Service  checkout-service -> checkout-service  (9 settings)  OK
  Data Pipeline     data-pipeline -> data-pipeline        (9 settings)  OK
  Internal          internal -> internal                  (9 settings)  OK
  Mobile App        mobile-app -> mobile-app              (9 settings)  OK
  Payments API      payments-api -> payments-api          (9 settings)  OK
  Web Dashboard     web-dashboard -> web-dashboard        (9 settings)  OK
  [UNMATCHED] android (source slug 'android') - no SaaS project by that name
```

**Change-detection proof** — set a bogus value in SaaS, re-run, confirm it resets to the source value:

```bash
# set resolveAge=999 on web-dashboard in SaaS, then re-run the migration
curl -s -X PUT "https://sentry.io/api/0/projects/$ORG/web-dashboard/" \
  -H "Authorization: Bearer $SAAS" -H "Content-Type: application/json" \
  -d '{"resolveAge": 999}' >/dev/null
$PY migrate_project_settings.py "$SAAS" "$ORG" --source-token "$SH"
# -> Web Dashboard ... verify: passed   (resolveAge reset from 999 back to source value 0)
```

**Result:** 6/6 matched, 0 wrong matches, per-project verification passed; the write path is confirmed
by the reset test.

---

## 3. Data scrubbers (`migrate_data_scrubbers.py`)

**Input**
```bash
$PY migrate_data_scrubbers.py "$SAAS" "$ORG" --source-token "$SH" --dry-run
$PY migrate_data_scrubbers.py "$SAAS" "$ORG" --source-token "$SH"
```

**Output (live summary)** — org level + all matched projects verified; advanced fields excluded:

```
Summary [LIVE]:
  org    dest-saas-org  (6 scrubbers)  OK
    Checkout Service  -> checkout-service  (6 scrubbers)  OK
    Data Pipeline     -> data-pipeline      (6 scrubbers)  OK
    Internal          -> internal           (6 scrubbers)  OK
    Mobile App        -> mobile-app         (6 scrubbers)  OK
    Payments API      -> payments-api       (6 scrubbers)  OK
    Web Dashboard     -> web-dashboard      (6 scrubbers)  OK
    [UNMATCHED] android (source slug 'android')
```

Each block also logs the excluded advanced fields, e.g.:
```
excluded : advanced fields not migrated (see DECISIONS.md D5): [relayPiiConfig, trustedRelays]
```

**Round-trip proof (end-to-end, via the UI).** Set three distinctive values on self-hosted
`mobile-app` → Security & Privacy, ran the tool, and confirmed they appeared on the SaaS project:

| Field set in self-hosted | Value | Confirmed in SaaS |
|---|---|---|
| Prevent Storing of IP Addresses | ON | ON |
| Additional Sensitive Fields | `credit_card` | `credit_card` |
| Safe Fields | `order_id` | `order_id` |

**Result:** org + 6/6 projects verified; advanced custom-PII left untouched; round-trip confirmed in
the UI.

---

## 4. Independent API diff (all features)

Beyond each script's own GET-back check, we re-read self-hosted vs SaaS separately to confirm the two
systems agree. Example (project general settings):

```bash
$PY - <<'PY'
import requests
F=["resolveAge","allowedDomains","scrapeJavaScript","verifySSL","subjectPrefix",
   "subjectTemplate","defaultEnvironment","highlightTags","highlightContext"]
sh=open("../.sh_read_token").read().strip(); saas=open("../.saas_token").read().strip()
shH={"Authorization":f"Bearer {sh}"}; saasH={"Authorization":f"Bearer {saas}"}
org="dest-saas-org"; src="migration-test-org"
for p in requests.get(f"http://127.0.0.1:9000/api/0/organizations/{src}/projects/",headers=shH).json():
    dl=requests.get(f"https://sentry.io/api/0/organizations/{org}/projects/",headers=saasH).json()
    d=next((x for x in dl if x["name"].lower()==p["name"].lower()),None)
    if not d: print(f"[{p['name']}] NO SAAS MATCH"); continue
    s=requests.get(f"http://127.0.0.1:9000/api/0/projects/{src}/{p['slug']}/",headers=shH).json()
    df=requests.get(f"https://sentry.io/api/0/projects/{org}/{d['slug']}/",headers=saasH).json()
    diffs={f:(s.get(f),df.get(f)) for f in F if s.get(f)!=df.get(f)}
    print(f"[{p['name']}] {'MATCH' if not diffs else 'DIFF '+str(diffs)}")
PY
```

**Output**
```
[Checkout Service] MATCH
[Data Pipeline] MATCH
[Internal] MATCH
[Mobile App] MATCH
[Payments API] MATCH
[Web Dashboard] MATCH
```

---

## 5. Summary

| Feature | Inputs | Outcome |
|---|---|---|
| Org settings | dry-run + live | 11/11 fields verified matching source |
| Project settings | dry-run + live + change-detection | 6/6 matched, 0 mismatches, reset test passed |
| Data scrubbers | dry-run + live + UI round-trip | org + 6/6 projects verified; advanced excluded |
| Match-by-name | live (with `android`) | unmatched project correctly reported, not force-matched |
| Idempotency | re-run each feature | second run all-pass, no errors |

All results files (`org_settings_migration_results.json`, `project_settings_migration_results.json`,
`data_scrubbers_migration_results.json`) are written per run and gitignored (may contain org data).
