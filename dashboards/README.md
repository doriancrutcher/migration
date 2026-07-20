# Dashboards migration

`migrate_dashboards.py` — recreates **custom dashboards** (widgets, queries, layout) from a live
self-hosted org into a SaaS org. Dashboards are **not** in the relocation export, so this is
live-API-driven (uses [`../common/selfhosted_source.py`](../common/selfhosted_source.py)), same as
the settings tools.

- **Dependencies:** `requests` (`pip install "requests>=2.31.0"`).
- **SaaS scope:** `org:read` + `org:write`. **Self-hosted scope:** `org:read` + `project:read`.
- **What migrates:** every *custom* dashboard (prebuilt ones like `default-overview` are skipped and
  reported). Per dashboard: title, widgets (title, displayType, interval, widgetType, layout, limit)
  and each widget's queries (name, fields, aggregates, columns, fieldAliases, conditions, orderby),
  dashboard-level `projects`, and `filters`.
- **Project matching:** by **name** (case-insensitive), same greenfield assumption as
  `project-settings`. Builds a source→dest id map and slug map; used to remap the dashboard-level
  `projects` list and to rewrite `project:<slug>` / `project.id:<id>` tokens inside widget query
  conditions. Unmappable references are **recorded, never silently dropped**.
- **Dataset/widgetType translation:** current SaaS rejects the legacy `discover` dataset. Each
  `discover` widget is classified from its query and translated: transaction-oriented widgets →
  `spans` (with `event.type:transaction` rewritten to `is_transaction:true` and
  `transaction.duration` → `span.duration`), everything else → `error-events`. `issue` and other
  already-current types pass through. Every translation is logged and recorded in the results file.
- **Idempotency:** a dashboard whose title already exists in the destination org is skipped.
- Verifies each created dashboard with a SaaS GET-back (widget count + titles) and writes
  `dashboard_migration_results.json`.

## Run

```bash
python3 dashboards/migrate_dashboards.py "$SAAS_TOKEN" "$DEST_ORG" \
    --source-token "$SH_TOKEN" \
    --source-org "$SRC_ORG" \
    --source-url "$SRC_URL" \
    [--saas-url https://sentry.io/api/0] \
    [--only "Dashboard Title"] \
    [--dry-run]
```

Always run `--dry-run` first (logs the intended `POST`s without sending them). `--only` limits the
run to dashboards with the given exact title (repeatable).

**`--source-url` matters:** it defaults to the local `http://127.0.0.1:9000/api/0`. For any non-local
self-hosted instance (dedicated host / VM), set it to that instance's API base, e.g.
`--source-url https://sentry.your-instance.example/api/0`. The `--source-token` is a read token minted
**on that self-hosted instance**, and the machine running this must be able to reach `--source-url`.

## Known limitations

- Dashboard **ownership** resets to the token's user (created_by is server-assigned on POST).
- The `discover` → `spans`/`error-events` translation covers the common cases exercised by the seed
  data. Exotic transaction fields beyond `transaction.duration` may need additional field mapping;
  any residual `400` is captured per-dashboard in the results file rather than aborting the run.
- Release-based dashboard `filters` are passed through; a release that doesn't exist in SaaS is left
  as-is (SaaS simply shows no data for it).
