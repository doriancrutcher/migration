# Project settings migration

`migrate_project_settings.py` — migrates whitelisted **per-project general settings** from a live self-hosted
org to a SaaS org. Live-API-driven (uses [`../common/selfhosted_source.py`](../common/selfhosted_source.py)).

- **Dependencies:** `requests` (`pip install -r ../requirements.txt`).
- **SaaS scope:** `project:write`. **Self-hosted scope:** `project:read`.
- **Project matching:** by **name** (case-insensitive). Core migration reassigns slugs on create but preserves
  names, so name is the stable key. Assumes a greenfield destination.
- Verifies each field with a SaaS GET-back and writes `project_settings_migration_results.json`.

## Run

```bash
python3 project-settings/migrate_project_settings.py <saas_token> <dest_org> \
    --source-token <selfhosted_read_token> \
    [--source-org migration-test-org] \
    [--source-url http://127.0.0.1:9000/api/0] \
    [--saas-url https://sentry.io/api/0] \
    [--dry-run]
```

Always run `--dry-run` first (logs the intended `PUT`s without sending them). Output is formatted as aligned
key/value blocks per project plus a summary table.
