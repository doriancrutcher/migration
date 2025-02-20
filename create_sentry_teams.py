import json
import requests
import logging
from typing import Set, Dict, List
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#mapping requires teams to already exist
class SentryTeamProjectMapper:
    def __init__(self, auth_token: str, org_slug: str, base_url: str = "https://sentry.io/api/0"):
        self.base_url = base_url
        self.org_slug = org_slug
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_export_data(self, export_file_path: str) -> List[Dict]:
        """
        Load data from the export JSON file
        """
        try:
            with open(export_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON file: {str(e)}")
            raise
        except FileNotFoundError:
            logger.error(f"File not found: {export_file_path}")
            raise

    def extract_mappings(self, data: List[Dict]) -> Dict[str, Dict]:
        """
        Extract teams and projects with their relationships
        Returns:
        {
            'teams': {
                'team_pk': {'slug': 'team_slug', 'name': 'team_name'},
                ...
            },
            'projects': {
                'project_slug': {
                    'pk': 'project_pk',
                    'team_pks': ['team_pk1', 'team_pk2', ...]
                },
                ...
            }
        }
        """
        mappings = {
            'teams': {},
            'projects': {}
        }
        project_team_maps = defaultdict(set)  # Project PK to set of team PKs

        # First pass: collect all teams and projects, and build relationships
        for item in data:
            if item.get('model') == 'sentry.team':
                team_pk = item.get('pk')
                fields = item.get('fields', {})
                if team_pk:
                    mappings['teams'][team_pk] = {
                        'slug': fields.get('slug', '').lower().replace(' ', '-'),
                        'name': fields.get('name')
                    }
                    logger.debug(f"Found team: {fields.get('name')} (PK: {team_pk})")
            
            elif item.get('model') == 'sentry.project':
                fields = item.get('fields', {})
                project_slug = fields.get('slug')
                project_pk = item.get('pk')
                if project_slug and project_pk:
                    mappings['projects'][project_slug] = {
                        'pk': project_pk,
                        'team_pks': set()  # Will be filled with multiple team PKs
                    }
                    # If this project has a team field, add to the mapping
                    if 'team' in fields:
                        project_team_maps[project_pk].add(fields['team'])
                    logger.debug(f"Found project: {project_slug} (PK: {project_pk})")
            
            # Look for projectteam relationships
            elif item.get('model') == 'sentry.projectteam':
                fields = item.get('fields', {})
                project_pk = fields.get('project')
                team_pk = fields.get('team')
                if project_pk and team_pk:
                    project_team_maps[project_pk].add(team_pk)
                    logger.debug(f"Found project-team relationship: Project PK {project_pk} -> Team PK {team_pk}")

        # Second pass: update project team relationships
        for project_slug, project_data in mappings['projects'].items():
            project_pk = project_data['pk']
            if project_pk in project_team_maps:
                team_pks = project_team_maps[project_pk]
                # Filter out any team PKs that don't exist in our teams mapping
                valid_team_pks = [pk for pk in team_pks if pk in mappings['teams']]
                mappings['projects'][project_slug]['team_pks'] = valid_team_pks
                logger.debug(f"Mapped project {project_slug} to teams: {valid_team_pks}")

        return mappings

    def add_team_to_project(self, project_slug: str, team_slug: str) -> bool:
        """
        Add a team to a project using the Sentry API
        """
        url = f"{self.base_url}/projects/{self.org_slug}/{project_slug}/teams/{team_slug}/"
        
        try:
            response = requests.post(url, headers=self.headers)
            response.raise_for_status()
            logger.info(f"Successfully added team '{team_slug}' to project '{project_slug}'")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add team '{team_slug}' to project '{project_slug}': {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def create_team(self, org_slug: str, team_name: str, team_slug: str = None) -> Dict:
        """
        Create a new team in the organization
        Returns both API response and original team PK
        """
        url = f"{self.base_url}/organizations/{org_slug}/teams/"
        payload = {"name": team_name}
        if team_slug:
            payload["slug"] = team_slug
        
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create team {team_name}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            raise

    def sync_project_teams(self, export_file_path: str, org_slug: str) -> Dict[str, List[str]]:
        """
        Sync team-project relationships from export file
        """
        results = {
            'teams_created': [],
            'teams_failed': [],
            'project_mappings_successful': [],
            'project_mappings_failed': [],
            'team_id_mappings': [],  # New field for storing old PK -> new ID mappings
            'mappings': {}
        }
        
        try:
            # Load and process export data
            data = self.load_export_data(export_file_path)
            mappings = self.extract_mappings(data)
            
            # Convert any sets in mappings to lists for JSON serialization
            for project_data in mappings['projects'].values():
                if 'team_pks' in project_data:
                    project_data['team_pks'] = list(project_data['team_pks'])
            
            results['mappings'] = mappings
            
            # First, create all teams
            for team_pk, team_data in mappings['teams'].items():
                try:
                    team_name = team_data['name']
                    team_slug = team_data['slug']
                    created_team = self.create_team(org_slug, team_name, team_slug)
                    logger.info(f"Created team: {team_slug}")
                    results['teams_created'].append(team_slug)
                    
                    # Store the mapping between old PK and new team ID
                    results['team_id_mappings'].append({
                        'old_pk': team_pk,
                        'new_id': created_team['id'],
                        'slug': team_slug,
                        'name': team_name
                    })
                    
                except Exception as e:
                    logger.error(f"Failed to create team {team_data['name']}: {str(e)}")
                    results['teams_failed'].append(team_data['slug'])
            
            # Then process project-team relationships
            for project_slug, project_data in mappings['projects'].items():
                team_pks = project_data.get('team_pks', [])
                
                for team_pk in team_pks:
                    if team_pk in mappings['teams']:
                        team_slug = mappings['teams'][team_pk]['slug']
                        
                        if self.add_team_to_project(project_slug, team_slug):
                            results['project_mappings_successful'].append(f"{project_slug}:{team_slug}")
                        else:
                            results['project_mappings_failed'].append(f"{project_slug}:{team_slug}")
                    else:
                        logger.warning(f"Team PK {team_pk} not found for project {project_slug}")
                        results['project_mappings_failed'].append(f"{project_slug}:unknown_team_{team_pk}")
            
            return results
            
        except Exception as e:
            logger.error(f"Project-team sync failed: {str(e)}")
            raise

def main():
    import sys
    
    if len(sys.argv) != 4:
        print("Usage: python create_sentry_teams.py <auth_token> <organization_slug> <export.json>")
        sys.exit(1)

    auth_token = sys.argv[1]
    org_slug = sys.argv[2]
    export_file = sys.argv[3]

    try:
        mapper = SentryTeamProjectMapper(auth_token, org_slug)
        results = mapper.sync_project_teams(export_file, org_slug)
        
        logger.info("Project-team sync completed:")
        logger.info(f"Teams created: {len(results['teams_created'])}")
        logger.info(f"Teams failed: {len(results['teams_failed'])}")
        logger.info(f"Project mappings successful: {len(results['project_mappings_successful'])}")
        logger.info(f"Project mappings failed: {len(results['project_mappings_failed'])}")
        
        # Write results to file
        with open('project_team_sync_results.json', 'w') as f:
            json.dump(results, f, indent=2)
            
    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()