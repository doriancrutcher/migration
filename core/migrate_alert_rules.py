import json
import logging
import argparse
import requests
from typing import Dict, List
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Handles both alert types:
#   - METRIC alerts (sentry.alertrule)  -> /organizations/{org}/alert-rules/
#   - ISSUE  alerts (sentry.rule)       -> /projects/{org}/{proj}/rules/
# For issue alerts the original conditions/filters (which use stable, cross-instance
# rule-class ids) are carried over, but the notification actions are NOT in a portable
# form, so -- like metric alerts -- we inject a default "email the owner team" action
# (falling back to IssueOwners/ActiveMembers when a rule has no owner team).

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

    def build_environments(self, data: List[Dict]) -> Dict[int, str]:
        """environment pk -> name (SaaS rules take an environment name or null)."""
        return {
            i["pk"]: i.get("fields", {}).get("name")
            for i in data if i.get("model") == "sentry.environment"
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

    def create_issue_alert_rule(self, org_slug: str, project_slug: str, payload: Dict) -> Dict:
        url = f"{self.base_url}/projects/{org_slug}/{project_slug}/rules/"
        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)}")
            return {"id": "dry-run", "name": payload.get("name"), "project": project_slug, "dry_run": True}
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create issue alert rule: {str(e)}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    @staticmethod
    def _default_issue_action(team_new_id) -> Dict:
        """Default notification for a migrated issue alert: email the owner team, or
        fall back to the issue's suggested owners when no team maps."""
        if team_new_id is not None:
            return {
                "id": "sentry.mail.actions.NotifyEmailAction",
                "targetType": "Team",
                "targetIdentifier": str(team_new_id),
                "fallthroughType": "ActiveMembers",
            }
        return {
            "id": "sentry.mail.actions.NotifyEmailAction",
            "targetType": "IssueOwners",
            "targetIdentifier": None,
            "fallthroughType": "ActiveMembers",
        }

    def migrate_issue_alerts(self, data: List[Dict], org_slug: str,
                             project_slugs: Dict[int, str], team_map: Dict[str, str],
                             env_index: Dict[int, str]):
        """Recreate sentry.rule issue alerts via the project rules endpoint."""
        migrated, failed = [], []
        for item in data:
            if item.get("model") != "sentry.rule":
                continue
            pk = item.get("pk")
            fields = item.get("fields", {})
            name = fields.get("label")

            project_slug = project_slugs.get(fields.get("project"))
            if not project_slug:
                failed.append((pk, "No project mapping found for issue alert"))
                logger.error(f"Issue alert {pk}: no project slug for project pk {fields.get('project')}")
                continue

            raw = fields.get("data")
            try:
                blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                failed.append((pk, "Unparseable rule data blob"))
                logger.error(f"Issue alert {pk}: could not parse data blob")
                continue

            team_pk = fields.get("owner_team")
            team_new_id = team_map.get(str(team_pk)) if team_pk is not None else None
            if team_pk is not None and team_new_id is None:
                logger.warning(f"Issue alert {pk}: no team mapping for owner team pk {team_pk}; "
                               f"defaulting action to IssueOwners")

            env_name = env_index.get(fields.get("environment_id")) if fields.get("environment_id") else None

            payload = {
                "name": name,
                "actionMatch": blob.get("action_match", "any"),
                "filterMatch": blob.get("filter_match", "all"),
                "frequency": blob.get("frequency", 30),
                "environment": env_name,
                "conditions": blob.get("conditions", []),
                "filters": blob.get("filters", []),
                "actions": [self._default_issue_action(team_new_id)],
            }
            if team_new_id is not None:
                payload["owner"] = f"team:{team_new_id}"

            try:
                new_rule = self.create_issue_alert_rule(org_slug, project_slug, payload)
                migrated.append(new_rule)
                logger.info(f"Migrated issue alert '{name}' -> project {project_slug}")
            except Exception as e:
                failed.append((pk, str(e)))
                logger.error(f"Failed to migrate issue alert {pk}: {e}")
        return migrated, failed

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

    def migrate_alert_rules(self, export_file: str, org_slug: str, team_mappings_file: str,
                            migrate_issue: bool = True):
        data = self.load_export_data(export_file)
        team_map = self.load_team_mappings(team_mappings_file)

        snuba_index = self.build_snuba_index(data)
        event_types = self.build_event_types(data)
        project_slugs = self.build_project_slugs(data)
        rule_projects = self.build_rule_projects(data)
        rule_triggers = self.build_rule_triggers(data)
        env_index = self.build_environments(data)

        migrated_rules, failed_rules = [], []

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

        issue_migrated, issue_failed = [], []
        if migrate_issue:
            issue_migrated, issue_failed = self.migrate_issue_alerts(
                data, org_slug, project_slugs, team_map, env_index
            )

        return {
            "metric": {"migrated": migrated_rules, "failed": failed_rules},
            "issue": {"migrated": issue_migrated, "failed": issue_failed},
        }


def main():
    parser = argparse.ArgumentParser(description='Migrate Sentry metric and issue alert rules')
    parser.add_argument('auth_token', help='Sentry auth token')
    parser.add_argument('org_slug', help='Organization slug')
    parser.add_argument('export_file', help='Path to export.json file')
    parser.add_argument('team_mappings_file', help='project_team_sync_results.json from create_sentry_teams.py')
    parser.add_argument('--dry-run', action='store_true', help='Log intended API calls without sending them')
    parser.add_argument('--skip-issue-alerts', action='store_true',
                        help='Migrate metric alerts only (skip sentry.rule issue alerts)')
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN: no changes will be made to SaaS ===")

    migrator = AlertRuleMigrator(args.auth_token, dry_run=args.dry_run)
    results = migrator.migrate_alert_rules(
        args.export_file, args.org_slug, args.team_mappings_file,
        migrate_issue=not args.skip_issue_alerts,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"alert_rule_migration_results_{timestamp}.json", 'w') as f:
        json.dump(results, f, indent=2)

    m, i = results["metric"], results["issue"]
    logger.info(
        f"Completed. Metric alerts migrated: {len(m['migrated'])}, failed: {len(m['failed'])} | "
        f"Issue alerts migrated: {len(i['migrated'])}, failed: {len(i['failed'])}"
    )


if __name__ == "__main__":
    main()
