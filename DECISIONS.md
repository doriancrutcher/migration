# Migration Decisions Log

A running record of scope/design choices made per feature -- especially things we intentionally
deferred or excluded and may want to revisit. Newest first. Each entry: what was decided, why, and
what would change it.

## D8 - Ship distinct, separately-run tools; no single orchestrating wizard
- Feature: delivery model (affects `feat/wizard`, now dropped as the default path)
- Decision: the toolkit is delivered as **distinct tools the operator runs one at a time, in a
  documented order**, not a single guided `migrate.py` that chains all steps. Each tool does one data
  type, is `--dry-run`-first, and writes its own results file to review before the next tool runs.
- Why: **overwrite safety.** A one-button orchestrator makes it too easy to fire a step that mutates the
  destination before the operator has reviewed the previous step's output. Separate, explicit commands
  force a human checkpoint between potentially destructive writes.
- Revisit if: the customer later wants a convenience runner -- it may be added, but only as an opt-in
  wrapper over the same tools, never as the default, and still dry-run-first per step.

## D7 - Duplicates report is export-based (offline) for now; live multi-org reader deferred
- Feature: `feat/duplicates-report`
- Decision: the duplicates/collision report reads **JSON export files** (one per self-hosted org) and
  compares them offline. It does **not** talk to a live self-hosted instance. Scope is names/slugs plus
  team-membership diffs and similar org names; **no usage/volume stats**.
- Why: exports are the stable, already-understood input, need no live token or multi-org API access, and
  are reproducible for tests. Volume/usage signals need the stats API and self-hosted test data has ~0
  events, so they would be uninformative right now.
- Revisit if: we want to run the report directly against a running instance (a live multi-org reader via
  `selfhosted_source.py`) or need usage-based priority ("high-volume = keep", "unused duplicate = drop").
  Those are follow-ups, not part of this tool's v1.

## D5 - Data scrubbers: standard fields only, advanced custom-PII deferred
- Feature: `feat/data-scrubbers`
- Decision: migrate the **standard** data-scrubbing settings at both org and project level
  (`dataScrubber`, `dataScrubberDefaults`, `sensitiveFields`, `safeFields`, `scrubIPAddresses`,
  `storeCrashReports`). **Do NOT** migrate the advanced custom-PII fields `relayPiiConfig`
  (custom PII/scrubbing rules) or `trustedRelays`.
- Why: the advanced fields are complex, relay-dependent, and higher-risk to copy blindly; the standard
  set fully covers the "Enabled data scrubbers" checklist item.
- Revisit if: the customer relies on custom PII rules (`relayPiiConfig`) or runs trusted Relays and
  needs them carried over. Would be a follow-up (e.g. `feat/data-scrubbers-advanced`).

## D4 - Project matching is by name (greenfield assumption)
- Feature: `feat/project-settings` (and reused by `feat/data-scrubbers`)
- Decision: pair self-hosted -> SaaS projects by **name** (case-insensitive); PUT to the destination's
  own slug. Unmatched projects are skipped and reported, never guessed.
- Why: phase-2 reassigned SaaS slugs but preserved names; names are the stable key. Assumes names are
  unique and unchanged, and the destination org is effectively empty (greenfield).
- Revisit if: brownfield destinations (existing/in-use SaaS org), duplicate/renamed project names, or
  multi-org consolidation. Tracked as the `feat/collision-preflight` milestone (per-type collision
  report + policy + provenance) in ROADMAP.

## D3 - Organization settings: require2FA skipped
- Feature: `feat/org-settings`
- Decision: do not migrate `require2FA`.
- Why: enabling it on the destination could lock out members who don't yet have 2FA set up.
- Revisit if: the customer explicitly wants 2FA enforcement carried over (with a member-readiness check
  first). Recorded in the results file as skipped, not silently dropped.

## D2 - Member roles flattened to "member" at invite time
- Feature: core (phase-2)
- Decision: all migrated members are invited as `member`.
- Why: internal-integration tokens can only invite at the `member` role.
- Revisit if: real roles must be preserved -> `feat/member-roles` (PUT the true `orgRole` after invite,
  needs a `member:admin` token).

## D1 - Alerts: metric alerts only
- Feature: core (phase-2)
- Decision: migrate metric alert rules; issue alerts (`sentry.rule`) are detected and reported as
  skipped, not migrated. Notification actions are not preserved (a default action is injected).
- Why: issue alerts use a different endpoint/schema; out of the promised core scope.
- Revisit if: issue alerts / real notification actions are required -> a dedicated alerts follow-up.
