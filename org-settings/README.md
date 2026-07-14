# Org settings migration

`migrate_org_settings.py` — migrates whitelisted **organization governance + privacy** settings from a live
self-hosted org to a SaaS org. Live-API-driven (uses [`../common/selfhosted_source.py`](../common/selfhosted_source.py)),
so it needs both a SaaS token and a self-hosted read token.

- **Dependencies:** `requests` (`pip install -r ../requirements.txt`).
- **SaaS scope:** `org:write`. **Self-hosted scope:** `org:read`.
- Post-migration it GETs the SaaS org back and verifies each field. Writes `org_settings_migration_results.json`.

## Run

```bash
python3 org-settings/migrate_org_settings.py <saas_token> <dest_org> \
    --source-token <selfhosted_read_token> \
    [--source-org migration-test-org] \
    [--source-url http://127.0.0.1:9000/api/0] \
    [--saas-url https://sentry.io/api/0] \
    [--dry-run]
```

Always run `--dry-run` first (logs the intended `PUT` without sending it).

## Scope (whitelist)

Migrated: `defaultRole`, `openMembership`, `allowJoinRequests`, `eventsMemberAdmin`, `alertsMemberWrite`,
`attachmentsRole`, `debugFilesRole`, `enhancedPrivacy`, `allowSharedIssues`, `scrapeJavaScript`, `isEarlyAdopter`.

Intentionally **skipped** (logged): `require2FA` — enforcing it can lock out members who haven't enrolled.
Data-scrubbing fields are handled by [`../data-scrubbers/`](../data-scrubbers/), not here.
