import json
import logging
import argparse
import requests
from typing import Dict, List
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Handles METRIC alert rules (sentry.alertrule). Issue alerts (sentry.rule) are
# reported but NOT migrated by this script -- their action/condition schema is
# different and needs the /projects/{org}/{proj}/rules/ endpoint.

# SnubaQueryEventType.EventType -> API eventTypes string
EVENT_TYPE_MAP = {0: "error", 1: "default", 2: "transaction"}


class AlertRuleMigrator:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.auth_token = auth_token
        self.base_url = base_url
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_export_data(self, export_file: str) -> List[Dict]:
        with open(export_file, 'r') as f:
            return json.load(f)

    # ---- lookup builders from the export ----
    def build_snuba_index(self, data: List[Dict]) -> Dict[int, Dict]:
        return {i["pk"]: i.get("fields", {}) for i in data if i.get("model") == "sentry.snubaquery"}

    def build_event_types(self, data: List[Dict]) -> Dict[int, List[str]]:
        out: Dict[int, List[str]] = {}
        for i in data:
            if i.get("model") == "sentry.snubaqueryeventtype":
                f = i.get("fields", {})
                sq = f.get("snuba_query")
                et = EVENT_TYPE_MAP.get(f.get("type"))
                if sq is not None and et:
                    out.setdefault(sq, []).append(et)
        return out

    def build_project_slugs(self, data: List[Dict]) -> Dict[int, str]:
        return {
            i["pk"]: i.get("fields", {}).get("slug")
            for i in data if i.get("model") == "sentry.project"
        }

    def build_rule_projects(self, data: List[Dict]) -> Dict[int, List[int]]:
        out: Dict[int, List[int]] = {}
        for i in data:
            if i.get("model") == "sentry.alertruleprojects":
                f = i.get("fields", {})
                out.setdefault(f.get("alert_rule"), []).append(f.get("project"))
        return out

    def build_rule_triggers(self, data: List[Dict]) -> Dict[int, List[Dict]]:
        out: Dict[int, List[Dict]] = {}
        for i in data:
            if i.get("model") == "sentry.alertruletrigger":
                f = i.get("fields", {})
                out.setdefault(f.get("alert_rule"), []).append({
                    "label": f.get("label", "critical"),
                    "alertThreshold": f.get("alert_threshold", 100),
                    "actions": [],  # NOTE: trigger actions/notifications are not carried over
                })
        return out

    def create_alert_rule(self, org_slug: str, payload: Dict) -> Dict:
        url = f"{self.base_url}/organizations/{org_slug}/alert-rules/"
        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)}")
            return {"id": "dry-run", "name": payload.get("name"), "dry_run": True}
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create alert rule: {str(e)}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    def load_team_mappings(self, mappings_file: str) -> Dict[str, str]:
        """old team pk -> new SaaS team id, from project_team_sync_results.json."""
        with open(mappings_file, "r") as f:
            data = json.load(f)
        team_mappings = {}
        for mapping in data.get("team_id_mappings", []):
            old_pk = str(mapping.get("old_pk"))
            new_id = mapping.get("new_id")
            if old_pk and new_id:
                team_mappings[old_pk] = new_id
        logger.info(f"Loaded {len(team_mappings)} team mappings")
        return team_mappings

    def migrate_alert_rules(self, export_file: str, org_slug: str, team_mappings_file: str):
        data = self.load_export_data(export_file)
        team_map = self.load_team_mappings(team_mappings_file)

        snuba_index = self.build_snuba_index(data)
        event_types = self.build_event_types(data)
        project_slugs = self.build_project_slugs(data)
        rule_projects = self.build_rule_projects(data)
        rule_triggers = self.build_rule_triggers(data)

        migrated_rules, failed_rules, skipped = [], [], []

        # Flag (but do not migrate) issue alerts
        issue_alerts = [i for i in data if i.get("model") == "sentry.rule"]
        for ia in issue_alerts:
            skipped.append({
                "pk": ia.get("pk"),
                "label": ia.get("fields", {}).get("label"),
                "reason": "Issue alert (sentry.rule) not supported by this script",
            })

        for item in data:
            if item.get("model") != "sentry.alertrule":
                continue
            pk = item.get("pk")
            fields = item.get("fields", {})
            name = fields.get("name")

            snuba_id = fields.get("snuba_query")
            snuba = snuba_index.get(snuba_id, {})
            if not snuba:
                failed_rules.append((pk, "No snuba_query found"))
                continue

            # projects: map source project pks -> slugs
            proj_pks = rule_projects.get(pk, [])
            projects = [project_slugs.get(p) for p in proj_pks if project_slugs.get(p)]
            if not projects:
                failed_rules.append((pk, "No project mapping found (alertruleprojects empty)"))
                continue

            # owner: map source team pk -> new SaaS team id
            team_pk = fields.get("team")
            team_new_id = team_map.get(str(team_pk)) if team_pk is not None else None
            if team_pk is not None and team_new_id is None:
                logger.warning(f"Alert rule {pk}: no team mapping for owner team pk {team_pk}; creating without owner")

            # triggers: real thresholds from alertruletrigger (fallback to a critical trigger)
            triggers = rule_triggers.get(pk) or [{"label": "critical", "alertThreshold": 100, "actions": []}]

            # SaaS requires every trigger to have >=1 action (self-hosted does not).
            # Original notification actions are NOT in the export, so we inject a default
            # email-to-owner-team action to satisfy the API. Flag for review.
            if team_new_id is not None:
                default_action = {"type": "email", "targetType": "team", "targetIdentifier": str(team_new_id)}
            else:
                default_action = {"type": "email", "targetType": "user", "targetIdentifier": None}
            for t in triggers:
                if not t.get("actions"):
                    t["actions"] = [default_action]

            payload = {
                "name": name,
                "dataset": snuba.get("dataset", "events"),
                "query": snuba.get("query", ""),
                "aggregate": snuba.get("aggregate", "count()"),
                "timeWindow": snuba.get("time_window", 3600) // 60 if isinstance(snuba.get("time_window"), int) else 60,
                "queryType": snuba.get("type", 0),
                "eventTypes": event_types.get(snuba_id, ["error"]),
                "thresholdType": fields.get("threshold_type", 0),
                "resolveThreshold": fields.get("resolve_threshold"),
                "comparisonDelta": fields.get("comparison_delta"),
                "triggers": triggers,
                "projects": projects,
            }
            if team_new_id is not None:
                payload["owner"] = f"team:{team_new_id}"

            try:
                new_rule = self.create_alert_rule(org_slug, payload)
                migrated_rules.append(new_rule)
                logger.info(f"Migrated alert rule '{name}' -> projects {projects}")
            except Exception as e:
                failed_rules.append((pk, str(e)))
                logger.error(f"Failed to migrate alert rule {pk}: {e}")

        return migrated_rules, failed_rules, skipped


def main():
    parser = argparse.ArgumentParser(description='Migrate Sentry metric alert rules')
    parser.add_argument('auth_token', help='Sentry auth token')
    parser.add_argument('org_slug', help='Organization slug')
    parser.add_argument('export_file', help='Path to export.json file')
    parser.add_argument('team_mappings_file', help='project_team_sync_results.json from create_sentry_teams.py')
    parser.add_argument('--dry-run', action='store_true', help='Log intended API calls without sending them')
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN: no changes will be made to SaaS ===")

    migrator = AlertRuleMigrator(args.auth_token, dry_run=args.dry_run)
    migrated, failed, skipped = migrator.migrate_alert_rules(args.export_file, args.org_slug, args.team_mappings_file)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"alert_rule_migration_results_{timestamp}.json", 'w') as f:
        json.dump({"migrated": migrated, "failed": failed, "skipped_issue_alerts": skipped}, f, indent=2)

    logger.info(f"Completed. Migrated: {len(migrated)}, Failed: {len(failed)}, Skipped issue alerts: {len(skipped)}")


if __name__ == "__main__":
    main()
