# Org settings migration

`migrate_org_settings.py` — migrates whitelisted **organization governance + privacy** settings from a live
self-hosted org to a SaaS org. Live-API-driven (uses [`../common/selfhosted_source.py`](../common/selfhosted_source.py)),
so it needs both a SaaS token and a self-hosted read token.

- **Dependencies:** `requests` (`pip install -r ../requirements.txt`).
- **SaaS scope:** `org:write`. **Self-hosted scope:** `org:read`.
- Post-migration it GETs the SaaS org back and verifies each field. Writes `org_settings_migration_results.json`.

## Run

```bash
python3 org-settings/migrate_org_settings.py "$SAAS_TOKEN" "$ORG" \
    --source-token "$SH_TOKEN" \
    --source-org "$SRC_ORG" \
    --source-url "$SRC_URL" \
    [--saas-url https://sentry.io/api/0] \
    [--dry-run]
```

Always run `--dry-run` first (logs the intended `PUT` without sending it).

**`--source-url` matters:** it defaults to the local `http://127.0.0.1:9000/api/0`. For any non-local
self-hosted instance (dedicated host / VM), set it to that instance's API base, e.g.
`--source-url https://sentry.your-instance.example/api/0`. The `--source-token` is a read token minted
**on that self-hosted instance**, and the machine running this must be able to reach `--source-url`.

## Scope (whitelist)

Migrated: `defaultRole`, `openMembership`, `allowJoinRequests`, `eventsMemberAdmin`, `alertsMemberWrite`,
`attachmentsRole`, `debugFilesRole`, `enhancedPrivacy`, `allowSharedIssues`, `scrapeJavaScript`, `isEarlyAdopter`.

Intentionally **skipped** (logged): `require2FA` — enforcing it can lock out members who haven't enrolled.
Data-scrubbing fields are handled by [`../data-scrubbers/`](../data-scrubbers/), not here.
