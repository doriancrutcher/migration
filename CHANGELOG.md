# Changelog

All notable changes to the migration scripts, relative to the upstream baseline
[dgbailey/migration](https://github.com/dgbailey/migration) (commit `2bd3bf6`).

Format loosely follows [Keep a Changelog](https://keepachangelog.com/). This project uses the
upstream fork's own history, not semver releases; the core-scope checkpoint is tagged `v1.0-core`.

## [v1.0-core] - 2026-07-08

Core-scope migration hardened and verified end-to-end (Projects, Teams & Membership, Alert Rules)
into the SaaS test org `dorian-v25-migration`.

### Added

- `--dry-run` **on all five migration scripts** (`create_sentry_projects.py`, `create_sentry_teams.py`,
`add_sentry_members.py`, `assign_team_members.py`, `migrate_alert_rules.py`). Logs the exact
method / URL / payload each would send, and returns fake ids/slugs so downstream steps can be
previewed too, without touching SaaS.
- `--send-invite` **flag on** `add_sentry_members.py`**.** Controls `sendInvite`/`reinvite` (default off,
preserving the original bulk-provision-without-email behavior). When set, the API attempts to send
invitation emails.
- `check_duplicates.py` **(new).** Offline pre-flight that scans one or more exports and reports
team/project **slug** collisions (would break a merged live run) and **name** collisions
(informational). Writes `duplicate_report.json`; exits non-zero on slug collisions. Never calls SaaS.
- `requirements.txt` **(new).** Pins the only dependency (`requests`).
- `README.md` **(new).** Annotated repo index: what each script does, run order, dependencies, known
limitations, token/permission notes.
- `ROADMAP.md` **(new).** Scope targets, milestones, and branch model.
- `.gitignore` **(new/real).** Ignores `__pycache__/`, `.venv/`, and runtime artifacts
(`export*.json`, `*_mappings.json`, results JSON, `duplicate_report.json`, `dryrun-out/`).
- `docs/` **(new).** Self-hosted setup runbook (`phase-1`) and migration runbook (`phase-2`).



### Changed

- `migrate_alert_rules.py` **- near rewrite** (~167 insertions / ~139 deletions). The original was an
unfinished scaffold that would fail on the first real rule. Now:
  - Real project targeting via `sentry.alertruleprojects` -> `sentry.project` slug (removed the
  hardcoded `"projects": ["your-project-slug"]` placeholder).
  - `queryType` taken from the snuba `type` field (was mistakenly the query string); the actual query
  string is now sent as its own `query` field (previously omitted).
  - `eventTypes` derived from `sentry.snubaqueryeventtype`.
  - `timeWindow` converted from seconds (self-hosted) to minutes (SaaS).
  - Real trigger labels/thresholds read from `sentry.alertruletrigger` (were hardcoded defaults from a
  non-existent field).
  - Owner mapped from the rule's `team` field -> new SaaS team id, formatted `team:<id>`.
  - Default **email-to-owner-team action injected** into any trigger with no action, since SaaS rejects
  a trigger with empty `actions` while self-hosted allows it and the export carries none.
  - Issue alerts (`sentry.rule`) are detected and reported as `skipped_issue_alerts` instead of being
  silently ignored.
  - Per-rule O(n) export scans replaced with index dicts built once.
  - Results written as structured `{migrated, failed, skipped_issue_alerts}` with counts.
- `create_sentry_projects.py`**.** Added a `slugify()` helper to predict the SaaS-derived slug in
dry-run output; platform fallback now handles null/empty values (`fields.get('platform') or 'python'`),
not just a missing key.
- `create_sentry_teams.py`**.** Migrated CLI from positional `sys.argv` parsing to `argparse`
(named args + `--help`).
- `assign_team_members.py`**.** Migrated CLI from positional `sys.argv` parsing to `argparse`.



### Fixed

- `migrate_alert_rules.py`**:** guarded access to `e.response.text` in the error handler, which
previously raised `AttributeError` on non-HTTP exceptions and masked the real error.



### Removed

- `keep.txt`**.** Dustin's scratch scope list; folded into `ROADMAP.md`.



### Known limitations (carried, flagged for review)

- Issue alerts (`sentry.rule`) are not migrated (metric alerts only).
- Alert notification actions are not preserved (a default action is injected).
- Member roles are flattened to `member` at invite time (integration-token limitation).
- Project slugs / DSNs change because slug isn't sent on create.
- Duplicate names across merged instances must be resolved manually (`check_duplicates.py` reports them).



## [Unreleased]

Repo restructured around a `main` trunk with one `feat/<data-type>` branch + PR per remaining data
type (see `ROADMAP.md`).

### Added (feat/org-settings)

- `selfhosted_source.py` (new): read-only live client for the self-hosted Sentry API (auth header,
  RFC5988 cursor pagination, `get_org`). The second data source, for models the relocation export
  does not carry. Reused and extended by later features.
- `migrate_org_settings.py` (new): migrates organization governance + privacy settings from the live
  self-hosted org to SaaS via a whitelist copy (`PUT /organizations/{org}/`). Includes `--dry-run`,
  post-run verification (GET-back compare), and a results file. Data-scrubbing fields are deferred to
  `feat/data-scrubbers` and `require2FA` is intentionally skipped -- both are recorded in the results
  file rather than silently dropped.

### Added (feat/project-settings)

- `selfhosted_source.py`: added `get_projects(org_slug)` (paginated project list) and
  `get_project(org_slug, project_slug)` (full per-project settings) helpers.
- `migrate_project_settings.py` (new): migrates per-project general settings from the live self-hosted
  org to SaaS. **Greenfield** scope: pairs source -> destination projects by **name** (case-insensitive,
  since phase-2 reassigned slugs but preserved names) and PUTs to the destination slug; unmatched source
  projects are skipped and reported. Whitelist (`resolveAge`, `allowedDomains`, `scrapeJavaScript`,
  `verifySSL`, `subjectPrefix`, `subjectTemplate`, `defaultEnvironment`, `highlightTags`,
  `highlightContext`). Data-scrubbing fields deferred to `feat/data-scrubbers`; identity/advanced/risky
  fields skipped -- both recorded per project. Includes `--dry-run`, per-project GET-back verification,
  and a `project_settings_migration_results.json` results file. Needs a SaaS `project:write` token.
- `migrate_project_settings.py`: human-readable run output -- dropped the logger prefix, one aligned
  per-project block (source/dest, `key = value` settings, deferred summary, action, verify) and a final
  summary table. Output only; behavior and results file unchanged.
- `ROADMAP.md`: marked org-settings and project-settings done; added a future `feat/collision-preflight`
  hardening milestone for brownfield destinations (pre-flight collision report + per-type merge policy +
  provenance).

### Added (feat/data-scrubbers)

- `migrate_data_scrubbers.py` (new): migrates the **standard** data-scrubbing settings deferred by the
  two settings features, at **both** org and project level, from the live self-hosted instance to SaaS.
  Whitelist (`dataScrubber`, `dataScrubberDefaults`, `sensitiveFields`, `safeFields`, `scrubIPAddresses`,
  `storeCrashReports`). Org via `PUT /organizations/{org}/`; projects paired by name (reusing the
  project-settings matching) via `PUT /projects/{org}/{proj}/`. `--org-only` / `--projects-only` scope
  flags, `--dry-run`, per-target GET-back verification, and a `data_scrubbers_migration_results.json`
  results file. The advanced custom-PII fields `relayPiiConfig` and `trustedRelays` are intentionally
  excluded (recorded, not dropped) -- see `DECISIONS.md` (D5). Needs a SaaS `org:write` + `project:write`
  token.
- `DECISIONS.md` (new): running log of scope/design choices we may revisit (advanced scrubbers deferral,
  project match-by-name/greenfield, `require2FA` skip, member-role flattening, metric-alerts-only).

