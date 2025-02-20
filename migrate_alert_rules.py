import json
import logging
import requests
from typing import Dict, List
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#currently only does metric alert rules
class AlertRuleMigrator:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0"):
        self.auth_token = auth_token
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_export_data(self, export_file: str) -> Dict:
        with open(export_file, 'r') as f:
            data = json.load(f)
        return data

    def get_snuba_query_details(self, data: Dict, query_id: int) -> Dict:
        """Extract Snuba query details from export data"""
        for item in data:
            if item.get("model") == "sentry.snubaquery" and item.get("pk") == query_id:
                return item.get("fields", {})
        return {}

    def translate_query_type(self, dataset: str, event_types: List[str] = None) -> int:
        """Translate dataset and event types to queryType"""
        if dataset == "events" and event_types and ("error" in event_types or "default" in event_types):
            return 0
        elif dataset == "transactions":
            return 1
        elif dataset == "metrics":
            return 2
        return 0  # default to error type

    def create_alert_rule(self, org_slug: str, rule_data: Dict, snuba_data: Dict) -> Dict:
        """Create alert rule in new Sentry instance"""
        url = f"{self.base_url}/organizations/{org_slug}/alert-rules/"

        # Translate the old data format to the new API format
        payload = {
            "name": rule_data.get("name"),
            "owner": rule_data.get("owner"),
            "dataset": snuba_data.get("dataset", "events"),
            "queryType": snuba_data.get("query", ""),
            "aggregate": snuba_data.get("aggregate", "count()"),
            "timeWindow": snuba_data.get("time_window", 60),
            "thresholdType": rule_data.get("threshold_type", 0),
            "resolveThreshold": rule_data.get("resolve_threshold"),
            "triggers": self._build_triggers(rule_data),
            "projects": ["your-project-slug"],  # You'll need to map project IDs
            "comparisonDelta": rule_data.get("comparison_delta")
        }

        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create alert rule: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            raise

    def _build_triggers(self, rule_data: Dict) -> List[Dict]:
        """Build triggers list from rule data"""
        triggers = []
        
        # Add critical trigger (required)
        critical_trigger = {
            "label": "critical",
            "alertThreshold": rule_data.get("threshold", 100),
            "actions": []  # You'll need to map actions from your data
        }
        triggers.append(critical_trigger)

        # Add warning trigger if present
        if rule_data.get("warning_threshold"):
            warning_trigger = {
                "label": "warning",
                "alertThreshold": rule_data.get("warning_threshold"),
                "actions": []
            }
            triggers.append(warning_trigger)

        return triggers

    def migrate_alert_rules(self, export_file: str, org_slug: str, team_mappings_file: str):
        """Main migration function"""
        data = self.load_export_data(export_file)
        team_mappings = self.load_team_mappings(team_mappings_file)
        migrated_rules = []
        failed_rules = []

        # Create lookup dict for old_pk to new_id
        pk_to_new_id = {
            mapping['old_pk']: mapping['new_id'] 
            for mapping in team_mappings.get('team_id_mappings', [])
        }

        for item in data:
            if item.get("model") == "sentry.alertrule":
                rule_data = item.get("fields", {})
                snuba_query_id = rule_data.get("snuba_query")
                
                # Map the owner field using old_pk to new_id mapping
                owner_pk = rule_data.get("owner")
                if owner_pk:
                    if str(owner_pk) in pk_to_new_id:
                        rule_data["owner"] = pk_to_new_id[str(owner_pk)]
                    else:
                        logger.warning(f"Could not find team mapping for owner PK {owner_pk}")
                        failed_rules.append((item.get('pk'), f"No team mapping found for owner PK {owner_pk}"))
                        continue
                
                if not snuba_query_id:
                    logger.warning(f"No Snuba query found for alert rule {item.get('pk')}")
                    continue

                snuba_data = self.get_snuba_query_details(data, snuba_query_id)
                
                try:
                    new_rule = self.create_alert_rule(org_slug, rule_data, snuba_data)
                    migrated_rules.append(new_rule)
                except Exception as e:
                    failed_rules.append((item.get('pk'), str(e)))
                    logger.error(f"Failed to migrate alert rule {item.get('pk')}: {e}")
                    continue

        return migrated_rules, failed_rules

    def load_team_mappings(self, mappings_file: str) -> Dict:
        """Load team mappings from the team creation results"""
        try:
            with open(mappings_file, 'r') as f:
                data = json.load(f)
                logger.info(f"Loaded team mappings file structure: {json.dumps(data.keys(), indent=2)}")
                
                # The team mappings are stored in 'team_id_mappings' in the output
                team_mappings = {}
                for mapping in data.get("team_id_mappings", []):
                    old_pk = str(mapping.get("old_pk"))
                    new_id = mapping.get("new_id")
                    if old_pk and new_id:
                        team_mappings[old_pk] = new_id
                        logger.info(f"Loaded mapping: old PK {old_pk} -> new ID {new_id}")
                
                if not team_mappings:
                    logger.warning("No team mappings found in the file!")
                else:
                    logger.info(f"Loaded {len(team_mappings)} team mappings")
                
                return team_mappings
                
        except Exception as e:
            logger.error(f"Failed to load team mappings: {str(e)}")
            raise

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Migrate Sentry Alert Rules')
    parser.add_argument('auth_token', help='Sentry auth token')
    parser.add_argument('org_slug', help='Organization slug')
    parser.add_argument('export_file', help='Path to export.json file')
    parser.add_argument('team_mappings_file', help='Path to team_mappings.json file')
    
    args = parser.parse_args()

    migrator = AlertRuleMigrator(args.auth_token)
    results = migrator.migrate_alert_rules(args.export_file, args.org_slug, args.team_mappings_file)

    # Save results to file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(f"alert_rule_migration_results_{timestamp}.json", 'w') as f:
        json.dump(results, f, indent=2)

    logger.info(f"Migration completed. Migrated: {len(results[0])} rules. Failed: {len(results[1])} rules.")

if __name__ == "__main__":
    main() 