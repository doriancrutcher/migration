import json
import requests
import logging
from typing import Dict, List
from collections import defaultdict
import argparse
from datetime import datetime
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#does not send invite emails. integration tokens are restricted to invting member roles only
class SentryMemberManager:
    def __init__(self, auth_token: str, test_email: str = None, base_url: str = "https://sentry.io/api/0"):
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }
        self.test_email = test_email

    def generate_test_email(self, original_email: str, internal_id: str) -> str:
        """
        Generate a unique test email using the Gmail + modifier
        """
        if not self.test_email:
            return original_email
            
        timestamp = datetime.now().strftime("%Y%m%d%H%M")
        base, domain = self.test_email.split('@')
        return f"{base}+test{internal_id}_{timestamp}@{domain}"

    def load_export_data(self, export_file_path: str) -> List[Dict]:
        """
        Load and parse the JSON export file
        """
        try:
            with open(export_file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load export file {export_file_path}: {str(e)}")
            raise

    def sync_members(self, export_file_path: str, org_slug: str) -> Dict[str, Dict]:
        """
        Add active users as members and track ID mappings
        """
        results = {
            'id_mappings': {
                'success': {},  # {internal_id: {'sentry_id': id, 'email': email, 'original_email': original}}
                'failed': {}    # {internal_id: {'error': error_msg, 'email': email, 'original_email': original}}
            },
            'stats': {
                'added': 0,
                'failed': 0,
                'skipped': 0
            }
        }
        
        # Also create a separate export file for team assignments
        team_assignment_mappings = {
            'user_mappings': {}  # {original_pk: sentry_member_id}
        }
        
        try:
            data = self.load_export_data(export_file_path)
            
            for item in data:
                if item.get('model') == 'sentry.organizationmember':
                    internal_id = str(item.get('pk'))
                    fields = item.get('fields', {})
                    
                    # Only process active users
                    if not fields.get('user_is_active', False):
                        results['stats']['skipped'] += 1
                        continue
                    
                    original_email = fields.get('user_email')
                    email = self.generate_test_email(original_email, internal_id)
                    role = fields.get('role', 'member')
                    
                    # Set up team roles if user is admin
                    team_roles = None
                    if role == 'admin':
                        team_roles = ["admin"]
                    
                    try:
                        created_member = self.add_member(
                            org_slug=org_slug,
                            email=email,
                            org_role=role,
                            team_roles=team_roles
                        )
                        
                        sentry_member_id = created_member.get('id')
                        
                        # Store successful mapping
                        results['id_mappings']['success'][internal_id] = {
                            'sentry_id': sentry_member_id,
                            'email': email,
                            'original_email': original_email
                        }
                        
                        # Add to team assignment mappings
                        team_assignment_mappings['user_mappings'][internal_id] = sentry_member_id
                        
                        results['stats']['added'] += 1
                        logger.info(f"Added member: {email} (original: {original_email}) with role {role}")
                        
                    except Exception as e:
                        # Store failed mapping
                        error_msg = str(e)
                        if hasattr(e, 'response') and hasattr(e.response, 'text'):
                            error_msg = e.response.text
                        
                        results['id_mappings']['failed'][internal_id] = {
                            'error': error_msg,
                            'email': email,
                            'original_email': original_email
                        }
                        results['stats']['failed'] += 1
                        logger.error(f"Failed to add member {email} (original: {original_email}): {error_msg}")
            
            # Write the team assignment mappings to a separate file
            with open('user_mappings_for_teams.json', 'w') as f:
                json.dump(team_assignment_mappings, f, indent=2)
            
            return results
            
        except Exception as e:
            logger.error(f"Member sync failed: {str(e)}")
            raise

    def delete_member(self, org_slug: str, member_id: str) -> bool:
        """
        Delete a member from the organization
        """
        url = f"{self.base_url}/organizations/{org_slug}/members/{member_id}/"
        
        try:
            response = requests.delete(url, headers=self.headers)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to delete member {member_id}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def delete_members(self, mappings_file: str, org_slug: str) -> Dict[str, List[str]]:
        """
        Delete members using the mappings file
        """
        results = {
            'deleted': [],
            'failed': []
        }
        
        try:
            # Load the mappings file
            with open(mappings_file, 'r') as f:
                mappings = json.load(f)
            
            # Process successful members
            for internal_id, member_data in mappings['id_mappings']['success'].items():
                sentry_id = member_data['sentry_id']
                email = member_data['email']
                
                if self.delete_member(org_slug, sentry_id):
                    results['deleted'].append({
                        'internal_id': internal_id,
                        'sentry_id': sentry_id,
                        'email': email
                    })
                    logger.info(f"Deleted member: {email} (Sentry ID: {sentry_id})")
                else:
                    results['failed'].append({
                        'internal_id': internal_id,
                        'sentry_id': sentry_id,
                        'email': email
                    })
                    logger.error(f"Failed to delete member: {email} (Sentry ID: {sentry_id})")
            
            return results
            
        except Exception as e:
            logger.error(f"Member deletion failed: {str(e)}")
            raise

    def add_member(self, org_slug: str, email: str, org_role: str, team_roles: List[Dict] = None) -> Dict:
        """
        Add a member to the organization
        """
        url = f"{self.base_url}/organizations/{org_slug}/members/"
        
        payload = {
            "email": email,
            "orgRole": "member",# restricted to member role via integration token
            "sendInvite": False,  # Don't send invite emails,
            "reinvite": False # Don't reinvite
        }
        
        # if team_roles:
        #     payload["teamRoles"] = team_roles
        #     del payload["orgRole"]
            
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add member {email}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            raise

def main():
    parser = argparse.ArgumentParser(description='Add or delete members from Sentry organization')
    parser.add_argument('auth_token', help='Sentry authentication token')
    parser.add_argument('org_slug', help='Organization slug')
    parser.add_argument('--delete', help='Delete members using mappings file', metavar='MAPPINGS_FILE')
    parser.add_argument('--export-file', help='Export JSON file path for adding members')
    parser.add_argument('--test', help='Test mode with Gmail alias (e.g., your.email@gmail.com)', metavar='EMAIL')
    
    args = parser.parse_args()

    try:
        manager = SentryMemberManager(args.auth_token, test_email=args.test)
        
        if args.delete:
            # Delete mode
            results = manager.delete_members(args.delete, args.org_slug)
            
            logger.info("Member deletion completed:")
            logger.info(f"Deleted members: {len(results['deleted'])}")
            logger.info(f"Failed deletions: {len(results['failed'])}")
            
            # Write deletion results to file
            with open('member_deletion_results.json', 'w') as f:
                json.dump(results, f, indent=2)
        
        elif args.export_file:
            # Add mode
            results = manager.sync_members(args.export_file, args.org_slug)
            
            logger.info("Member sync completed:")
            logger.info(f"Added members: {results['stats']['added']}")
            logger.info(f"Failed additions: {results['stats']['failed']}")
            logger.info(f"Skipped (inactive): {results['stats']['skipped']}")
            
            # Write results to file
            with open('member_id_mappings.json', 'w') as f:
                json.dump(results, f, indent=2)
        
        else:
            parser.error("Either --delete or --export-file must be specified")
            
    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main() 