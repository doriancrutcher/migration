# experiments/

Spikes that explore capabilities beyond the supported toolkit. Code here is **experimental**: it
works and has been run live, but it isn't part of the standard migration flow and may change or be
folded into the core scripts later.

## `slack_action_carryover.py` — carry a Slack alert action over to SaaS

By default, `core/migrate_alert_rules.py` replaces every issue-alert notification action with a
default **email** action, because a Slack action stores IDs that are specific to the source
instance (see `../DECISIONS.md` D9). This spike shows that the Slack action **can** be preserved
when the same Slack workspace is already installed in the destination SaaS org.

### How it works

| Piece | Source |
|---|---|
| Alert name, conditions, filters | JSON export (self-hosted) |
| Slack `channel` / `channel_id` / old `workspace` id | JSON export (self-hosted) |
| Destination Slack integration id | **Live SaaS API read** (`GET /organizations/{org}/integrations/?provider_key=slack`) |
| Creating the rule | Live SaaS API write (`POST /projects/{org}/{project}/rules/`) |

It rewrites **only** the instance-specific `workspace` field (old export id → the destination
integration id), keeps `channel`/`channel_id`, and polls SaaS's asynchronous channel-validation
task until the rule is created or rejected.

### Prerequisites

- The alert's **project already exists** in the destination SaaS org.
- The **same Slack workspace is installed** in the destination SaaS org (Settings → Integrations →
  Slack), and the Sentry app is present in the target channel.
- A SaaS token with `org:read` (to list integrations) + `alerts:write`/`project:write`.

### Usage

```bash
export SAAS_TOKEN=...          # org:read + alerts:write
export DEST_ORG=...            # destination SaaS org slug

# dry-run first (pass --workspace <id> to preview fully offline)
python3 experiments/slack_action_carryover.py "$SAAS_TOKEN" "$DEST_ORG" export.json \
  --only "My Slack Alert" --dry-run

# for real (auto-detects the destination Slack integration id)
python3 experiments/slack_action_carryover.py "$SAAS_TOKEN" "$DEST_ORG" export.json \
  --only "My Slack Alert"
```

Success prints `SLACK CARRIED OVER -> channel=... workspace=<dest id>`. Then verify in the SaaS UI
(open the rule → Send Test Notification).

### Notes / limits

- Same-workspace assumption: `channel_id` is Slack's own id, valid only if the *same* Slack
  workspace is connected in SaaS. A different workspace would need channel re-resolution.
- Only Slack is handled here. Other integrations (PagerDuty, Opsgenie, MS Teams, specific users)
  store their own instance-specific ids and would each need the same rewrite treatment.
- This is a spike, not a supported path. Productionizing means adding a `--preserve-integrations`
  flag to `core/migrate_alert_rules.py` that does this lookup+rewrite and falls back to email when
  no matching destination integration exists.
