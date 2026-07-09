"""Migrate per-project general settings from self-hosted -> SaaS (greenfield).

Source of truth is the live self-hosted project (its detailed GET returns every field already
in SaaS field names), so per project the migration is a whitelist copy: read the source
project, pick the whitelisted fields, PUT them to the matching destination project, then
verify with a GET.

Project matching (greenfield assumption): during phase-2 the SaaS side reassigned project
slugs, but project names were preserved. So we pair source -> destination by NAME
(case-insensitive) and PUT using the destination's own slug. A source project with no name
match on SaaS is skipped and reported (we never guess). This assumes project names are unique
and unchanged; brownfield collision handling (existing orgs, rename/merge policy, provenance)
is a separate future feature and intentionally not done here.

Data-scrubbing settings are intentionally NOT handled here -- they belong to the dedicated
`feat/data-scrubbers` feature. Identity/advanced/risky fields are skipped. Both groups are
recorded in the results file (no silent drops).

Usage:
  python migrate_project_settings.py <saas_token> <dest_org> --source-token <t> \
      [--source-org migration-test-org] [--source-url http://127.0.0.1:9000/api/0] [--dry-run]

The SaaS token needs `project:write`. The self-hosted token needs `project:read`.
"""
import json
import logging
import argparse
import requests
from datetime import datetime

from selfhosted_source import SelfHostedSource

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _fmt(value, limit: int = 100) -> str:
    """Compact, readable rendering of a setting value (truncated if very long)."""
    s = json.dumps(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."

# Core general project settings carried by this feature.
PROJECT_SETTINGS_WHITELIST = [
    "resolveAge",
    "allowedDomains",
    "scrapeJavaScript",
    "verifySSL",
    "subjectPrefix",
    "subjectTemplate",
    "defaultEnvironment",
    "highlightTags",
    "highlightContext",
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
]

# Deliberately not migrated: identity (matched separately), set at creation, or risky/out of scope.
SKIPPED = [
    "slug",
    "name",
    "platform",
    "securityToken",
    "groupingConfig",
    "groupingEnhancements",
    "fingerprintingRules",
    "dynamicSamplingBiases",
    "isBookmarked",
    "builtinSymbolSources",
]


class ProjectSettingsMigrator:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    def list_projects(self, dest_org: str) -> list:
        """GET all destination projects (follows cursor pagination)."""
        results = []
        url = f"{self.base_url}/organizations/{dest_org}/projects/"
        params = None
        while url:
            resp = requests.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            results.extend(resp.json())
            url, params = self._next_link(resp.headers.get("Link")), None
        return results

    @staticmethod
    def _next_link(link_header: str):
        if not link_header:
            return None
        for part in link_header.split(","):
            if 'rel="next"' in part and 'results="true"' in part:
                start, end = part.find("<"), part.find(">")
                if start != -1 and end != -1:
                    return part[start + 1:end]
        return None

    def build_payload(self, source_project: dict) -> dict:
        return {k: source_project[k] for k in PROJECT_SETTINGS_WHITELIST if k in source_project}

    def update_project(self, dest_org: str, dest_slug: str, payload: dict) -> dict:
        url = f"{self.base_url}/projects/{dest_org}/{dest_slug}/"
        if self.dry_run:
            logger.info(f"  action      : [DRY-RUN] would PUT {url} (not sent)")
            return {"dry_run": True}
        try:
            resp = requests.put(url, headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update project '{dest_slug}': {e}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    def get_project(self, dest_org: str, dest_slug: str) -> dict:
        url = f"{self.base_url}/projects/{dest_org}/{dest_slug}/"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def verify(self, dest_org: str, dest_slug: str, payload: dict) -> dict:
        """Return {field: {expected, actual}} for any whitelisted field that didn't take."""
        current = self.get_project(dest_org, dest_slug)
        mismatches = {}
        for k, expected in payload.items():
            actual = current.get(k)
            if actual != expected:
                mismatches[k] = {"expected": expected, "actual": actual}
        return mismatches


def main():
    parser = argparse.ArgumentParser(description="Migrate per-project settings self-hosted -> SaaS")
    parser.add_argument("auth_token", help="SaaS auth token (needs project:write)")
    parser.add_argument("dest_org", help="Destination SaaS org slug")
    parser.add_argument("--source-token", required=True, help="Self-hosted read token (project:read)")
    parser.add_argument("--source-org", default="migration-test-org", help="Self-hosted org slug")
    parser.add_argument("--source-url", default="http://127.0.0.1:9000/api/0", help="Self-hosted API base URL")
    parser.add_argument("--saas-url", default="https://sentry.io/api/0", help="SaaS API base URL")
    parser.add_argument("--dry-run", action="store_true", help="Log the intended PUTs without sending them")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN: no changes will be made to SaaS ===")

    source = SelfHostedSource(args.source_token, base_url=args.source_url)
    migrator = ProjectSettingsMigrator(args.auth_token, base_url=args.saas_url, dry_run=args.dry_run)

    logger.info(f"Listing source projects from self-hosted org '{args.source_org}' at {args.source_url} ...")
    src_projects = source.get_projects(args.source_org)
    logger.info(f"Found {len(src_projects)} source project(s).")

    logger.info(f"Listing destination projects from SaaS org '{args.dest_org}' ...")
    dest_projects = migrator.list_projects(args.dest_org)
    # Match by name, case-insensitive.
    dest_by_name = {p["name"].strip().lower(): p for p in dest_projects}
    logger.info(f"Found {len(dest_projects)} destination project(s).")

    per_project = []
    unmatched = []

    for src in src_projects:
        name = src.get("name", "")
        key = name.strip().lower()
        dest = dest_by_name.get(key)
        if not dest:
            logger.warning(f"No SaaS project named '{name}' (source slug '{src.get('slug')}') -- skipping.")
            unmatched.append({"source_name": name, "source_slug": src.get("slug")})
            continue

        dest_slug = dest["slug"]
        # The list payload is lightweight; fetch full source settings.
        src_full = source.get_project(args.source_org, src["slug"])
        payload = migrator.build_payload(src_full)
        deferred_present = {k: src_full.get(k) for k in DEFERRED_TO_DATA_SCRUBBERS if k in src_full}
        skipped_present = [k for k in SKIPPED if k in src_full]

        logger.info("")
        logger.info("-" * 64)
        logger.info(f"Project: {name}")
        logger.info(f"  source slug : {src['slug']}")
        logger.info(f"  dest slug   : {dest_slug}")
        if payload:
            key_w = max(len(k) for k in payload)
            logger.info(f"  settings applied ({len(payload)}):")
            for k, v in payload.items():
                logger.info(f"      {k.ljust(key_w)} = {_fmt(v)}")
        else:
            logger.info("  settings applied (0): none present on source")
        if deferred_present:
            logger.info(
                f"  deferred    : {len(deferred_present)} data-scrubber field(s) -> feat/data-scrubbers"
                f" ({', '.join(deferred_present)})"
            )

        migrator.update_project(args.dest_org, dest_slug, payload)

        mismatches = {}
        if args.dry_run:
            logger.info("  verify      : skipped (dry-run)")
        else:
            mismatches = migrator.verify(args.dest_org, dest_slug, payload)
            if mismatches:
                logger.warning(f"  verify      : MISMATCH {_fmt(mismatches, 200)}")
            else:
                logger.info("  verify      : passed")

        per_project.append({
            "source_name": name,
            "source_slug": src["slug"],
            "dest_slug": dest_slug,
            "applied": payload,
            "deferred_to_data_scrubbers": deferred_present,
            "skipped": skipped_present,
            "verification_mismatches": mismatches,
        })

    results = {
        "timestamp": datetime.now().isoformat(),
        "source_org": args.source_org,
        "dest_org": args.dest_org,
        "dry_run": args.dry_run,
        "matched_count": len(per_project),
        "unmatched_count": len(unmatched),
        "projects": per_project,
        "unmatched": unmatched,
    }
    with open("project_settings_migration_results.json", "w") as f:
        json.dump(results, f, indent=2)

    logger.info("")
    logger.info("=" * 64)
    mode = "DRY RUN (nothing written)" if args.dry_run else "LIVE"
    logger.info(f"Summary [{mode}]: matched {len(per_project)}, unmatched {len(unmatched)}")
    if per_project:
        name_w = max(len(p["source_name"]) for p in per_project)
        for p in per_project:
            if args.dry_run:
                status = "would apply"
            else:
                status = "OK" if not p["verification_mismatches"] else "MISMATCH"
            logger.info(
                f"  {p['source_name'].ljust(name_w)}  {p['source_slug']} -> {p['dest_slug']}"
                f"  ({len(p['applied'])} settings)  {status}"
            )
    for u in unmatched:
        logger.info(f"  [UNMATCHED] {u['source_name']} (source slug '{u['source_slug']}') - no SaaS project by that name")
    logger.info("")
    logger.info("Wrote project_settings_migration_results.json")


if __name__ == "__main__":
    main()
