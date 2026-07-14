# Data-scrubbers migration

`migrate_data_scrubbers.py` — migrates **standard data-scrubbing** settings at both org and project level from
a live self-hosted org to a SaaS org. Live-API-driven (uses [`../common/selfhosted_source.py`](../common/selfhosted_source.py)).

- **Dependencies:** `requests` (`pip install "requests>=2.31.0"`).
- **SaaS scope:** `org:write` + `project:write`. **Self-hosted scope:** `org:read`, `project:read`.
- Verifies each field with a SaaS GET-back and writes `data_scrubbers_migration_results.json`.

## Run

```bash
python3 data-scrubbers/migrate_data_scrubbers.py "$SAAS_TOKEN" "$DEST_ORG" \
    --source-token "$SH_TOKEN" \
    --source-org "$SRC_ORG" \
    --source-url "$SRC_URL" \
    [--saas-url https://sentry.io/api/0] \
    [--org-only | --projects-only] \
    [--dry-run]
```

Always run `--dry-run` first. Use `--org-only` or `--projects-only` to scope the run to a single level
(default migrates both). Projects are matched by name, consistent with the project-settings tool.

**`--source-url` matters:** it defaults to the local `http://127.0.0.1:9000/api/0`. For any non-local
self-hosted instance (dedicated host / VM), set it to that instance's API base, e.g.
`--source-url https://sentry.your-instance.example/api/0`. The `--source-token` is a read token minted
**on that self-hosted instance**, and the machine running this must be able to reach `--source-url`.

## Scope

Standard scrubbers: `dataScrubber`, `dataScrubberDefaults`, `scrubIPAddresses`, and `sensitiveFields` /
`safeFields`. **Advanced custom-PII / relay-rule scrubbing is deferred** to a later phase (see DECISIONS.md).
