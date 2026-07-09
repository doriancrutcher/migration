import json
import re
import requests
import logging
from typing import Dict, List
from collections import defaultdict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "project"


#requires team to already exist, always uses 'migration' as the team.
class SentryProjectManager:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_data_from_json(self, json_file_path: str) -> List[Dict]:
        """
        Load data from a JSON file
        """
        try:
            with open(json_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON file: {str(e)}")
            raise
        except FileNotFoundError:
            logger.error(f"File not found: {json_file_path}")
            raise

    def delete_project(self, org_slug: str, project_slug: str) -> bool:
        """
        Delete a project from the organization
        """
        url = f"{self.base_url}/projects/{org_slug}/{project_slug}/"

        if self.dry_run:
            logger.info(f"[DRY-RUN] DELETE {url}")
            return True

        try:
            response = requests.delete(url, headers=self.headers)
            response.raise_for_status()
            logger.info(f"Successfully scheduled deletion for project: {project_slug}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to delete project {project_slug}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def create_project(self, org_slug: str, project_config: Dict) -> Dict:
        """
        Create a new Sentry project for the specified organization.
        Always uses 'migration' as the team.
        """
        url = f"{self.base_url}/teams/{org_slug}/migration/projects/"

        # Ensure project name is included in the body
        payload = {
            "name": project_config['name'],
            "platform": project_config['platform']
        }

        if self.dry_run:
            predicted_slug = project_config.get('slug') or slugify(project_config['name'])
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)} -> predicted slug '{predicted_slug}'")
            return {"slug": predicted_slug, "name": project_config['name'], "dry_run": True}

        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create project: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            raise

    def sync_projects(self, json_file_path: str, org_slug: str, delete_mode: bool = False) -> Dict[str, List[str]]:
        """
        Create or delete projects from JSON file based on mode
        """
        results = {
            'created': [],
            'failed': [],
            'deleted': [],
            'delete_failed': []
        }

        try:
            data = self.load_data_from_json(json_file_path)

            for item in data:
                if item.get('model') == 'sentry.project':
                    project_slug = item.get('fields', {}).get('slug')

                    if delete_mode:
                        if self.delete_project(org_slug, project_slug):
                            results['deleted'].append(project_slug)
                        else:
                            results['delete_failed'].append(project_slug)
                    else:
                        project_config = {
                            'name': item.get('fields', {}).get('name'),
                            'slug': project_slug,
                            'platform': item.get('fields', {}).get('platform') or 'python',

                        }

                        try:
                            created_project = self.create_project(org_slug, project_config)
                            logger.info(f"Created project: {created_project['slug']}")
                            results['created'].append(created_project['slug'])
                        except Exception as e:
                            logger.error(f"Failed to create project {project_slug}: {str(e)}")
                            results['failed'].append(project_slug)

            return results

        except Exception as e:
            logger.error(f"Project sync failed: {str(e)}")
            raise

def main():
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='Manage Sentry projects')
    parser.add_argument('auth_token', help='Sentry authentication token')
    parser.add_argument('org_slug', help='Organization slug')
    parser.add_argument('export_file', help='JSON export file path')
    parser.add_argument('--delete', action='store_true', help='Delete projects instead of creating them')
    parser.add_argument('--dry-run', action='store_true', help='Log intended API calls without sending them')

    args = parser.parse_args()

    try:
        manager = SentryProjectManager(args.auth_token, dry_run=args.dry_run)
        if args.dry_run:
            logger.info("=== DRY RUN: no changes will be made to SaaS ===")
        results = manager.sync_projects(args.export_file, args.org_slug, args.delete)

        logger.info("Project management completed:")
        if args.delete:
            logger.info(f"Deleted projects: {len(results['deleted'])}")
            logger.info(f"Failed deletions: {len(results['delete_failed'])}")
        else:
            logger.info(f"Created projects: {len(results['created'])}")
            logger.info(f"Failed creations: {len(results['failed'])}")

        # Write results to file
        with open('project_management_results.json', 'w') as f:
            json.dump(results, f, indent=2)

    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
