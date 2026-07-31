# Tests

Hermetic unit tests for the migration tools. They stub out `requests` before importing
the tool under test, so they run with **plain `python3`** — no network, no third-party
dependencies, and nothing is ever sent to Sentry.

## Run

```bash
# from the repo root (migration/)
python3 -m unittest discover -s tests
```

## Coverage

- `test_issue_alerts.py` — issue-alert (`sentry.rule`) migration in
  `core/migrate_alert_rules.py`:
  - default notification action = email the mapped **owner team**; **IssueOwners**
    fallback when a rule has no owner team (or the team doesn't map);
  - `conditions` / `filters` / `actionMatch` / `filterMatch` / `frequency` carried over
    (with sane defaults), environment id → name mapping;
  - correct `/projects/{org}/{project}/rules/` endpoint;
  - error handling (missing project, unparseable `data`) is recorded as failed and does
    **not** POST;
  - `--dry-run` sends nothing; the top-level result has separate `metric` / `issue`
    sections and `--skip-issue-alerts` skips issue alerts.
- `test_dashboards.py` — dashboard migration in `dashboards/migrate_dashboards.py`:
  - source→dest project id/slug map by **name**; unmatched sources omitted;
  - `project:` / `project.id:` token rewrite in widget conditions; unmapped refs recorded, not dropped;
  - widget payload shaping (only whitelisted widget/query fields forwarded);
  - `discover` → `error-events` / `spans` translation, incl. `event.type:transaction` →
    `is_transaction:true` and `transaction.duration` → `span.duration`; `issue` passes through;
  - dashboard-level `projects` remap (all-projects `-1` passthrough; unmapped recorded), `filters` passthrough;
  - prebuilt (non-numeric id) detection, `--dry-run` sends nothing, POST error surfaced as `RuntimeError`,
    and `verify()` flags widget count/title mismatches.
