"""Migrate organization-level governance + privacy settings from self-hosted -> SaaS.

Source of truth is the live self-hosted org (its detailed GET returns every field already in
SaaS field names), so the migration is a whitelist copy: read the source org, pick the
whitelisted fields, PUT them to the destination org, then verify with a GET.

Data-scrubbing settings are intentionally NOT handled here -- they belong to the dedicated
`feat/data-scrubbers` feature. `require2FA` is intentionally skipped to avoid locking members
out of the destination org. Both groups are recorded in the results file (no silent drops).

Usage:
  python migrate_org_settings.py <saas_token> <dest_org> --source-token <t> \
      [--source-org migration-test-org] [--source-url http://127.0.0.1:9000/api/0] [--dry-run]

The SaaS token needs `org:write`. The self-hosted token needs `org:read`.
"""
import json
import logging
import argparse
import requests
from datetime import datetime

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from selfhosted_source import SelfHostedSource

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Governance + privacy settings carried by this feature.
ORG_SETTINGS_WHITELIST = [
    "defaultRole",
    "openMembership",
    "allowJoinRequests",
    "eventsMemberAdmin",
    "alertsMemberWrite",
    "attachmentsRole",
    "debugFilesRole",
    "enhancedPrivacy",
    "allowSharedIssues",
    "scrapeJavaScript",
    "isEarlyAdopter",
]

# Handled by feat/data-scrubbers, not here.
DEFERRED_TO_DATA_SCRUBBERS = [
    "dataScrubber",
    "dataScrubberDefaults",
    "sensitiveFields",
    "safeFields",
    "scrubIPAddresses",
    "storeCrashReports",
    "relayPiiConfig",
    "trustedRelays",
]

# Deliberately not migrated (security lockout risk / out of scope).
SKIPPED = ["require2FA"]


class OrgSettingsMigrator:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    def build_payload(self, source_org: dict) -> dict:
        return {k: source_org[k] for k in ORG_SETTINGS_WHITELIST if k in source_org}

    def update_org(self, dest_org: str, payload: dict) -> dict:
        url = f"{self.base_url}/organizations/{dest_org}/"
        if self.dry_run:
            logger.info(f"[DRY-RUN] PUT {url} payload={json.dumps(payload)}")
            return {"dry_run": True}
        try:
            resp = requests.put(url, headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update org settings: {e}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    def get_org(self, dest_org: str) -> dict:
        url = f"{self.base_url}/organizations/{dest_org}/"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def verify(self, dest_org: str, payload: dict) -> dict:
        """Return {field: {expected, actual}} for any whitelisted field that didn't take."""
        current = self.get_org(dest_org)
        mismatches = {}
        for k, expected in payload.items():
            actual = current.get(k)
            if actual != expected:
                mismatches[k] = {"expected": expected, "actual": actual}
        return mismatches


def main():
    parser = argparse.ArgumentParser(description="Migrate organization settings self-hosted -> SaaS")
    parser.add_argument("auth_token", help="SaaS auth token (needs org:write)")
    parser.add_argument("dest_org", help="Destination SaaS org slug")
    parser.add_argument("--source-token", required=True, help="Self-hosted read token (org:read)")
    parser.add_argument("--source-org", default="migration-test-org", help="Self-hosted org slug")
    parser.add_argument("--source-url", default="http://127.0.0.1:9000/api/0", help="Self-hosted API base URL")
    parser.add_argument("--saas-url", default="https://sentry.io/api/0", help="SaaS API base URL")
    parser.add_argument("--dry-run", action="store_true", help="Log the intended PUT without sending it")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN: no changes will be made to SaaS ===")

    source = SelfHostedSource(args.source_token, base_url=args.source_url)
    logger.info(f"Reading org settings from self-hosted org '{args.source_org}' at {args.source_url} ...")
    src_org = source.get_org(args.source_org)

    migrator = OrgSettingsMigrator(args.auth_token, base_url=args.saas_url, dry_run=args.dry_run)
    payload = migrator.build_payload(src_org)
    deferred_present = {k: src_org.get(k) for k in DEFERRED_TO_DATA_SCRUBBERS if k in src_org}
    skipped_present = {k: src_org.get(k) for k in SKIPPED if k in src_org}

    logger.info(f"Applying {len(payload)} whitelisted settings: {json.dumps(payload)}")
    logger.info(f"Deferred to feat/data-scrubbers (not applied): {list(deferred_present)}")
    logger.info(f"Skipped (not applied): {list(skipped_present)}")

    migrator.update_org(args.dest_org, payload)

    mismatches = {}
    if not args.dry_run:
        mismatches = migrator.verify(args.dest_org, payload)
        if mismatches:
            logger.warning(f"Verification mismatches (field did not take): {json.dumps(mismatches)}")
        else:
            logger.info("Verification passed: all whitelisted fields on SaaS match the source.")

    results = {
        "timestamp": datetime.now().isoformat(),
        "source_org": args.source_org,
        "dest_org": args.dest_org,
        "dry_run": args.dry_run,
        "applied": payload,
        "deferred_to_data_scrubbers": deferred_present,
        "skipped": skipped_present,
        "verification_mismatches": mismatches,
    }
    with open("org_settings_migration_results.json", "w") as f:
        json.dump(results, f, indent=2)
    logger.info("Wrote org_settings_migration_results.json")


if __name__ == "__main__":
    main()
