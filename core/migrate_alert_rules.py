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

    @staticmethod
    def resolve_source_org_pk(data: List[Dict], source_org: str = None):
        """Resolve which SOURCE org's rules to migrate. An export may contain many orgs. Alert rules
        are scoped by project membership (project->organization FK), so this returns the source org
        pk used to restrict which projects (and therefore which rules) are in scope. Returns None
        (no filter) only when the file holds a single org and no --source-org was given."""
        orgs = {i.get('pk'): (i.get('fields') or {}).get('slug')
                for i in data if isinstance(i, dict) and i.get('model') == 'sentry.organization'}
        if source_org:
            matches = [pk for pk, slug in orgs.items() if slug == source_org]
            if not matches:
                raise ValueError(f"--source-org '{source_org}' not found. Orgs in file: {sorted(filter(None, orgs.values()))}")
            if len(matches) > 1:
                raise ValueError(f"--source-org '{source_org}' is ambiguous (pks {matches} share this slug)")
            return matches[0]
        if len(orgs) > 1:
            raise ValueError(
                f"Export contains {len(orgs)} orgs {sorted(filter(None, orgs.values()))}; "
                f"pass --source-org SLUG to migrate one at a time.")
        return next(iter(orgs), None)

    # ---- lookup builders from the export ----
    def build_snuba_index(self, data: List[Dict]) -> Dict[int, Dict]:
        return {i["pk"]: i.get("fields", {}) for i in data
                if isinstance(i, dict) and i.get("model") == "sentry.snubaquery"}

    def build_event_types(self, data: List[Dict]) -> Dict[int, List[str]]:
        out: Dict[int, List[str]] = {}
        for i in data:
            if isinstance(i, dict) and i.get("model") == "sentry.snubaqueryeventtype":
                f = i.get("fields", {})
                sq = f.get("snuba_query")
                et = EVENT_TYPE_MAP.get(f.get("type"))
                if sq is not None and et:
                    out.setdefault(sq, []).append(et)
        return out

    def build_project_slugs(self, data: List[Dict], source_pk=None) -> Dict[int, str]:
        """Map project pk -> slug. When source_pk is given, only projects owned by that org are
        included, which is what scopes alert rules to a single source org."""
        out: Dict[int, str] = {}
        for i in data:
            if not isinstance(i, dict) or i.get("model") != "sentry.project":
                continue
            f = i.get("fields", {}) or {}
            if source_pk is not None and f.get("organization") != source_pk:
                continue
            out[i["pk"]] = f.get("slug")
        return out

    def build_rule_projects(self, data: List[Dict]) -> Dict[int, List[int]]:
        out: Dict[int, List[int]] = {}
        for i in data:
            if isinstance(i, dict) and i.get("model") == "sentry.alertruleprojects":
                f = i.get("fields", {})
                out.setdefault(f.get("alert_rule"), []).append(f.get("project"))
        return out

    def build_rule_triggers(self, data: List[Dict]) -> Dict[int, List[Dict]]:
        out: Dict[int, List[Dict]] = {}
        for i in data:
            if isinstance(i, dict) and i.get("model") == "sentry.alertruletrigger":
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

    def migrate_alert_rules(self, export_file: str, org_slug: str, team_mappings_file: str,
                            source_org: str = None):
        data = self.load_export_data(export_file)
        source_pk = self.resolve_source_org_pk(data, source_org)
        if source_pk is not None:
            logger.info(f"Filtering to source org '{source_org or '(only org in file)'}' (pk {source_pk})")
        team_map = self.load_team_mappings(team_mappings_file)

        snuba_index = self.build_snuba_index(data)
        event_types = self.build_event_types(data)
        # project_slugs is scoped to the source org; rules whose projects are all outside it are skipped.
        project_slugs = self.build_project_slugs(data, source_pk=source_pk)
        rule_projects = self.build_rule_projects(data)
        rule_triggers = self.build_rule_triggers(data)

        migrated_rules, failed_rules, skipped, skipped_other_org = [], [], [], []

        # Flag (but do not migrate) issue alerts
        issue_alerts = [i for i in data if isinstance(i, dict) and i.get("model") == "sentry.rule"]
        for ia in issue_alerts:
            skipped.append({
                "pk": ia.get("pk"),
                "label": ia.get("fields", {}).get("label"),
                "reason": "Issue alert (sentry.rule) not supported by this script",
            })

        for item in data:
            if not isinstance(item, dict) or item.get("model") != "sentry.alertrule":
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
            # When filtering by source org, drop rules whose projects all belong to another org.
            if source_pk is not None:
                in_scope = [p for p in proj_pks if p in project_slugs]
                if proj_pks and not in_scope:
                    skipped_other_org.append({"pk": pk, "name": name, "reason": "Rule belongs to another org"})
                    continue
                proj_pks = in_scope
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

        return migrated_rules, failed_rules, skipped, skipped_other_org


def main():
    parser = argparse.ArgumentParser(description='Migrate Sentry metric alert rules')
    parser.add_argument('auth_token', help='Sentry auth token')
    parser.add_argument('org_slug', help='Destination SaaS organization slug')
    parser.add_argument('export_file', help='Path to export.json file')
    parser.add_argument('team_mappings_file', help='project_team_sync_results.json from create_sentry_teams.py')
    parser.add_argument('--source-org', help='Source org slug to migrate (required when the export holds multiple orgs)')
    parser.add_argument('--run_on_real_data', type=lambda v: str(v).strip().lower() in ('true', '1', 'yes', 'y'),
                        default=False, metavar='true|false',
                        help='Set to true to actually perform changes. Default false = dry-run.')
    parser.add_argument('--dry-run', action='store_true',
                        help='(default) Dry-run is on by default; accepted for compatibility and is a no-op.')
    args = parser.parse_args()

    dry_run = not args.run_on_real_data
    if dry_run:
        logger.info("=== DRY RUN (default): no changes will be made to SaaS. Pass --run_on_real_data=true to apply. ===")
    else:
        logger.info("=== EXECUTE: changes WILL be made to SaaS ===")

    migrator = AlertRuleMigrator(args.auth_token, dry_run=dry_run)
    migrated, failed, skipped, skipped_other_org = migrator.migrate_alert_rules(
        args.export_file, args.org_slug, args.team_mappings_file, source_org=args.source_org)

    # Tag output with source org, dest org, and timestamp so per-org runs never overwrite.
    tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
    out = f"alert_rule_migration_results_{tag}.json"
    with open(out, 'w') as f:
        json.dump({"migrated": migrated, "failed": failed, "skipped_issue_alerts": skipped,
                   "skipped_other_org": skipped_other_org}, f, indent=2)

    logger.info(f"Completed. Migrated: {len(migrated)}, Failed: {len(failed)}, "
                f"Skipped issue alerts: {len(skipped)}, Skipped other-org rules: {len(skipped_other_org)}")
    logger.info(f"Wrote {out}")


if __name__ == "__main__":
    main()
