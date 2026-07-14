# Sentry Self-Hosted → SaaS Migration Toolkit

Fork of [github.com/dgbailey/migration](https://github.com/dgbailey/migration), reorganized into a set of
**distinct tools run one at a time, in a defined order**. Each tool does one data type, is `--dry-run`-first,
and writes its own results file to review before you run the next one. There is deliberately **no single
orchestrator** — see [DECISIONS.md](DECISIONS.md) (D8): separate, explicit commands force a human review
checkpoint between potentially destructive writes.

- **Roadmap, milestones, branch model:** [ROADMAP.md](ROADMAP.md)
- **Changelog (vs. upstream):** [CHANGELOG.md](CHANGELOG.md)
- **Scope decisions log (deferrals to revisit):** [DECISIONS.md](DECISIONS.md)

## How the data flows

Two sources feed the migration:

- **Export-driven** (core + pre-flight): a self-hosted relocation export (Django `dumpdata`-style JSON —
  a flat list of `{"model", "pk", "fields"}` objects) is parsed and recreated in SaaS.
- **Live-API-driven** (settings tools): things the export doesn't carry are read from the live self-hosted
  REST API (read-only) via [`common/selfhosted_source.py`](common/selfhosted_source.py) and written to SaaS.

```
self-hosted --(export)-->  export.json  --.
                                           >--(tools + SaaS token)--> sentry.io
self-hosted --(read API)-> live settings -'
```

## The tools (run in this order)

| Step | Folder | Tool | What it does |
|------|--------|------|--------------|
| 0 | [`preflight/`](preflight/) | `duplicates_report.py` | Multi-org merges only: report cross-org project/team collisions + team-membership diffs from exports |
| 1 | [`core/`](core/) | 5 scripts | Projects, Teams & membership, Alert rules (the delivered P0 minimum) |
| 2 | [`org-settings/`](org-settings/) | `migrate_org_settings.py` | Org governance + privacy settings |
| 3 | [`project-settings/`](project-settings/) | `migrate_project_settings.py` | Per-project general settings |
| 4 | [`data-scrubbers/`](data-scrubbers/) | `migrate_data_scrubbers.py` | Standard data-scrubbing settings (org + project) |

Shared code lives in [`common/`](common/) (the read-only self-hosted API client used by the settings tools).
Each folder has its own `README.md` with the exact command, inputs/outputs, and flags.

Run commands from this directory, e.g. `python3 preflight/duplicates_report.py ...`, `python3 core/create_sentry_projects.py ...`.

## SaaS token / permissions

Create an Internal Integration (Org Settings → Developer Settings → Custom Integrations) and use its token.
Scopes across the suite: `org:write`, `team:write`, `project:admin`, `member:write`, `alerts:write`. Member
invites also require a plan with the invite feature enabled (the free Developer plan blocks them). The
settings tools additionally need a **self-hosted read token** (`org:read`, `project:read`) — passed at run
time via `--source-token`. No credentials are ever committed or shipped.

## Dependencies

- `preflight/` — **none** (Python 3 standard library only).
- `core/` and the settings tools — `requests`:

```bash
pip install -r requirements.txt
```

## Known limitations (carried, flagged for review)

- **Issue alerts** (`sentry.rule`) are not migrated — metric alerts only (different endpoint/schema).
- **Alert notification actions** are not preserved; migrated rules get a default action only.
- **Member roles** are flattened to `member` (integration-token invite limitation).
- **Project slugs / DSNs change** because slug isn't sent on create (SaaS derives it from the name).
- **Duplicate names across instances** must be resolved before a merged run (`preflight/duplicates_report.py`
  reports them; the tools do not auto-rename).
