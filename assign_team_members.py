import json
import requests
import logging
from typing import Dict, List
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SentryTeamMemberManager:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0"):
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_export_data(self, export_file_path: str) -> List[Dict]:
        try:
            with open(export_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON file: {str(e)}")
            raise

    def load_member_mappings(self, mappings_file_path: str) -> Dict:
        try:
            with open(mappings_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid member mappings file: {str(e)}")
            raise

    def add_member_to_team(self, org_slug: str, member_id: str, team_slug: str) -> bool:
        """
        Add a member to a team
        """
        url = f"{self.base_url}/organizations/{org_slug}/members/{member_id}/teams/{team_slug}/"
        
        try:
            response = requests.post(url, headers=self.headers)
            if response.status_code in [201, 202, 204]:  # All successful states
                logger.info(f"Successfully added member {member_id} to team {team_slug}")
                return True
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add member {member_id} to team {team_slug}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def sync_team_members(self, export_file_path: str, mappings_file_path: str, org_slug: str) -> Dict:
        """
        Sync team memberships using the member mappings and export data
        """
        results = {
            'successful': [],
            'failed': [],
            'skipped': [],
            'team_mappings': defaultdict(list)
        }

        try:
            # Load member ID mappings
            member_mappings = self.load_member_mappings(mappings_file_path)
            successful_members = member_mappings['user_mappings']

            # Load export data
            data = self.load_export_data(export_file_path)
            
            # Create team pk to slug mapping
            team_slugs = {}
            for item in data:
                if item.get('model') == 'sentry.team':
                    team_slugs[item['pk']] = item.get('fields', {}).get('slug')

            # Process organization member team relationships
            for item in data:
                if item.get('model') == 'sentry.organizationmemberteam':
                    fields = item.get('fields', {})
                    user_pk = str(fields.get('organizationmember'))
                    team_pk = fields.get('team')
                    
                    # Skip if we don't have mappings for either user or team
                    if not user_pk or not team_pk:
                        continue
                    
                    if user_pk not in successful_members:
                        results['skipped'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'reason': 'User not successfully created in Sentry'
                        })
                        continue

                    team_slug = team_slugs.get(team_pk)
                    if not team_slug:
                        results['skipped'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'reason': 'Team slug not found'
                        })
                        continue

                    sentry_member_id = successful_members[user_pk]
                    
                    if self.add_member_to_team(org_slug, sentry_member_id, team_slug):
                        results['successful'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'sentry_member_id': sentry_member_id,
                            'team_slug': team_slug
                        })
                        results['team_mappings'][team_slug].append(sentry_member_id)
                    else:
                        results['failed'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'sentry_member_id': sentry_member_id,
                            'team_slug': team_slug
                        })

            # Convert defaultdict to regular dict for JSON serialization
            results['team_mappings'] = dict(results['team_mappings'])
            return results

        except Exception as e:
            logger.error(f"Team member sync failed: {str(e)}")
            raise

def main():
    import sys
    
    if len(sys.argv) != 5:
        print("Usage: python assign_team_members.py <auth_token> <organization_slug> <export.json> <member_mappings.json>")
        sys.exit(1)

    auth_token = sys.argv[1]
    org_slug = sys.argv[2]
    export_file = sys.argv[3]
    mappings_file = sys.argv[4]

    try:
        manager = SentryTeamMemberManager(auth_token)
        results = manager.sync_team_members(export_file, mappings_file, org_slug)
        
        logger.info("Team member sync completed:")
        logger.info(f"Successful assignments: {len(results['successful'])}")
        logger.info(f"Failed assignments: {len(results['failed'])}")
        logger.info(f"Skipped assignments: {len(results['skipped'])}")
        
        # Write results to file
        with open('team_member_assignments.json', 'w') as f:
            json.dump(results, f, indent=2)
            
    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main() 