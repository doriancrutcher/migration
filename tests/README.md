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
