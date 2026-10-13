# alerts-aci — alert migration on the Workflows / Detectors API

On **2026-09-24** Sentry removed the legacy alert endpoints that `core/migrate_alert_rules.py` writes to
(`POST /projects/{org}/{proj}/rules/` and `POST /organizations/{org}/alert-rules/` now return **404**;
getsentry/sentry #121879). This folder keeps the core's logic and sends its output to the new API instead:

| Self-hosted | SaaS (new API) |
|---|---|
| issue alert (`sentry.rule`) | 1 **workflow** attached to the project's issue-stream detector |
| metric alert (`sentry.alertrule`) | 1 **workflow** + 1 `metric_issue` **detector** (monitor) per project |

`core/migrate_alert_rules.py` is **not modified** and still makes every decision: owner/team/user remapping,
Slack rebind, action porting, transaction→span translation, strict skipping. `aci_alert_migrator.py`
subclasses it and translates each finished legacy payload into the new shape.

Every tool is **dry by default**. Pass `--run_on_real_data=true` to write.

## Tools

| Script | Writes? | What it does |
|---|---|---|
| `migrate_safe_alerts_aci.py` | yes (opt-in) | Migrates a **pinned** list of alerts (by export pk, never by name) |
| `verify_safe_alerts_aci.py` | no | Checks every alert a live run created exists, is linked to the right project/monitor, has no dead Slack action, and isn't duplicated |
| `check_targets_in_org_aci.py` | no | Before a run: is each target already in the org? (`PRESENT-SAME`, `PRESENT-DIFF`, `RENAMED-COPY`, `SIMILAR`, `ELSEWHERE`, `MISSING`) |
| `validate_aci_payloads.py` | no (offline) | Re-checks translated payloads against the server's validation rules (needs `jsonschema`) |
| `snapshot_org_alerts.py` | no | Saves every project, workflow and detector in an org to one JSON |
| `audit_all_alerts_offline.py` | no (offline) | Compares every alert in one or more exports against a snapshot, flags missing / muted / default / customized-default |
| `pagerduty_followup_list.py` | no (offline) | From a `--pagerduty-as-email` run: md + csv list of alerts that need PagerDuty re-added |
| `delete_verified_aci.py` | yes (opt-in) | **Test orgs only** (`--allow-org`): deletes exactly what a verified run created |
| `aci_alert_migrator.py` | — | Library used by the scripts above |

## Targets file

```json
{"generated_utc": "2026-10-01T00:00:00Z",
 "targets": [
   {"type": "issue",  "pk": 123, "project_slug": "my-project", "name": "My alert"},
   {"type": "metric", "pk": 45,  "project_slug": "my-project", "name": "Error rate"}]}
```

`type` + `pk` (the row pk in the export) are required. The script builds a reduced copy of the export
containing only these alert rows plus every supporting row, so the core can't match other alerts that
happen to share a name.

## Typical run

```
export SAAS_TOKEN=...   # org:read project:read project:write alerts:write member:read (+ team:admin)
export DEST_ORG=my-saas-org SRC_ORG=my-source-org EXPORT=export.json

python3 check_targets_in_org_aci.py "$SAAS_TOKEN" "$DEST_ORG" --targets targets.json

python3 migrate_safe_alerts_aci.py "$SAAS_TOKEN" "$DEST_ORG" --export "$EXPORT" --source-org "$SRC_ORG" \
  --targets targets.json --team-map team_map.json --user-map user_map.json \
  --slack-integration-id "$SLACK_INTEGRATION_ID"

python3 validate_aci_payloads.py dry-run-outputs/aci_safe_alert_migration_results_<ts>.json

# same migrate command + --run_on_real_data=true, then:
python3 verify_safe_alerts_aci.py "$SAAS_TOKEN" "$DEST_ORG" --results aci_safe_alert_migration_results_<ts>.json
```

`--team-map` / `--user-map` are the mapping files written by the core team/member steps (from a **live**
run — files containing `dryrun-*` ids are refused).

## Useful flags (`migrate_safe_alerts_aci.py`)

- `--offline` — no network at all; destination ids become placeholders. Review payloads without a token.
- `--pagerduty-as-email` — PagerDuty services don't carry over; replace each PD action with an email to the
  owner team and record the swap (`pagerduty_followup_list.py` turns it into a follow-up list).
- `--drop-member SOURCE_USER_ID` — drop email actions to one person (e.g. a pending invite), keep the rest.
- `--exclude issue:PK` / `--only-remaining LIVE_RESULTS.json` — leave one out / retry what was held back or failed.
- `--only-email` — hold back Slack alerts while the org lacks the Slack alert feature.
- `--create-twins` — when a same-named alert with a different config exists, create `<name> (v24)` instead of skipping.
- `--skip-quota-check` — skip the 1000-workflows-per-org preflight when the org's cap is raised.
- `--protect-org SLUG` — orgs where test-only flags (`--test-slack-as-email`) are refused.

## Behaviour to know about

- **Duplicates:** the new API has no server-side duplicate check. Issue alerts are deduped client-side by a
  config fingerprint (same name + same config → skipped; same name + different config → skipped and reported
  unless `--create-twins`). Metric alerts are deduped by (name, project).
- **Owner teams:** "You can only assign teams you are a member of" unless the token has `team:admin`.
- **Member emails** need the member's **user id**; pending invites have none and fail
  ("pending invite?") — use `--drop-member` or wait for the invite to be accepted.
- **Environments:** issue workflows need the environment to already exist in the org; metric detectors create it.
- **Metric alerts with no actions on a trigger** get the owner-team email on that trigger (the core does this
  because the old API required ≥1 action per trigger), so a warning level that only showed in the UI on
  self-hosted will now also email.
- **Transaction → span:** transaction-based metric alerts move to the spans dataset (`is_transaction:true`);
  counts are threshold-scaled when the window widens (`--no-scale-count-thresholds` to disable). Check the
  monitor's chart after creating it.
- **Disabled / snoozed** source alerts are refused (they would be created enabled).

Outputs (results JSON, logs, snapshots, audit CSVs) contain org data and are git-ignored in this folder.
