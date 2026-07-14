# Data-scrubbers migration

`migrate_data_scrubbers.py` — migrates **standard data-scrubbing** settings at both org and project level from
a live self-hosted org to a SaaS org. Live-API-driven (uses [`../common/selfhosted_source.py`](../common/selfhosted_source.py)).

- **Dependencies:** `requests` (`pip install -r ../requirements.txt`).
- **SaaS scope:** `org:write` + `project:write`. **Self-hosted scope:** `org:read`, `project:read`.
- Verifies each field with a SaaS GET-back and writes `data_scrubbers_migration_results.json`.

## Run

```bash
python3 data-scrubbers/migrate_data_scrubbers.py <saas_token> <dest_org> \
    --source-token <selfhosted_read_token> \
    [--source-org migration-test-org] \
    [--source-url http://127.0.0.1:9000/api/0] \
    [--saas-url https://sentry.io/api/0] \
    [--org-only | --projects-only] \
    [--dry-run]
```

Always run `--dry-run` first. Use `--org-only` or `--projects-only` to scope the run to a single level
(default migrates both). Projects are matched by name, consistent with the project-settings tool.

## Scope

Standard scrubbers: `dataScrubber`, `dataScrubberDefaults`, `scrubIPAddresses`, and `sensitiveFields` /
`safeFields`. **Advanced custom-PII / relay-rule scrubbing is deferred** to a later phase (see DECISIONS.md).
