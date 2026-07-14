# Sentry Self-Hosted → SaaS Migration Toolkit

**This README is the master runbook.** The migration is performed by running the tools below **one at a
time, in the order given** — there is deliberately **no single orchestrator script**. Each tool does one
data type, is `--dry-run`-first, and writes its own results file that you review **before** running the next
one. Do not skip ahead: later steps depend on files written by earlier ones, and the manual review between
steps is the safety checkpoint against destructive writes (see [DECISIONS.md](DECISIONS.md) D8).

Fork of [github.com/dgbailey/migration](https://github.com/dgbailey/migration), reorganized into per-tool
folders.

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

## Order of operations (run each step, review, then continue)

Run every command from this directory. **Dry-run first**, inspect the output, then re-run for real. After
each step, review the results file it writes before moving on. Each folder's own `README.md` has the exact
command, inputs/outputs, and flags.

| Step | Folder | Run | Review after |
|------|--------|-----|--------------|
| **0. Pre-flight** *(multi-org merges only)* | [`preflight/`](preflight/) | `duplicates_report.py` — cross-org project/team collisions + membership diffs | `duplicate_report.json` (resolve every **Danger** before continuing) |
| **1a. Projects** | [`core/`](core/) | `create_sentry_projects.py` | `project_management_results.json` |
| **1b. Teams** | [`core/`](core/) | `create_sentry_teams.py` | `project_team_sync_results.json` |
| **1c. Members** | [`core/`](core/) | `add_sentry_members.py` | `member_id_mappings.json`, `user_mappings_for_teams.json` |
| **1d. Team membership** | [`core/`](core/) | `assign_team_members.py` | `team_member_assignments.json` |
| **1e. Alerts** | [`core/`](core/) | `migrate_alert_rules.py` | `alert_rule_migration_results_<ts>.json` |
| **2. Org settings** | [`org-settings/`](org-settings/) | `migrate_org_settings.py` | `org_settings_migration_results.json` |
| **3. Project settings** | [`project-settings/`](project-settings/) | `migrate_project_settings.py` | `project_settings_migration_results.json` |
| **4. Data scrubbers** | [`data-scrubbers/`](data-scrubbers/) | `migrate_data_scrubbers.py` | `data_scrubbers_migration_results.json` |

Why the order is fixed (hard dependencies): a SaaS team slugged `migration` must exist before **1a**;
**1b** attaches teams to already-created projects; **1d** needs the user mappings from **1c**; **1e** maps
each alert's owner team via the mappings from **1b**. The settings steps (2–4) run after the objects they
configure exist. See [`core/README.md`](core/) for the full dependency notes.

Shared code lives in [`common/`](common/) (the read-only self-hosted API client used by the settings tools).

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
