"""Migrate data-scrubbing / privacy settings from self-hosted -> SaaS (org + project level).

These are the fields deferred by feat/org-settings and feat/project-settings. Source of truth is the
live self-hosted instance (detailed GETs already return SaaS field names), so each level is a whitelist
copy: read source -> pick whitelisted fields -> PUT -> verify with a GET.

Scope: STANDARD scrubbers only. The advanced custom-PII fields (`relayPiiConfig`, `trustedRelays`) are
intentionally NOT migrated -- see DECISIONS.md (D5). Nothing is silently dropped; excluded fields that
are present on the source are recorded in the results file.

Project matching (greenfield): self-hosted -> SaaS projects are paired by NAME (case-insensitive),
same as feat/project-settings; unmatched projects are skipped and reported.

Usage:
  python migrate_data_scrubbers.py <saas_token> <dest_org> --source-token <t> \
      [--source-org migration-test-org] [--source-url http://127.0.0.1:9000/api/0] \
      [--org-only | --projects-only] [--dry-run]

The SaaS token needs `org:write` (org level) and `project:write` (project level).
The self-hosted token needs `org:read` and `project:read`.
"""
import json
import logging
import argparse
import requests
from datetime import datetime

from selfhosted_source import SelfHostedSource

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Standard data-scrubbing settings, applied at BOTH org and project level.
SCRUBBER_WHITELIST = [
    "dataScrubber",
    "dataScrubberDefaults",
    "sensitiveFields",
    "safeFields",
    "scrubIPAddresses",
    "storeCrashReports",
]

# Advanced custom-PII fields deliberately NOT migrated (see DECISIONS.md D5).
EXCLUDED_ADVANCED = ["relayPiiConfig", "trustedRelays"]


def _fmt(value, limit: int = 100) -> str:
    s = json.dumps(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _next_link(link_header: str):
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part and 'results="true"' in part:
            start, end = part.find("<"), part.find(">")
            if start != -1 and end != -1:
                return part[start + 1:end]
    return None


class ScrubberMigrator:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    def build_payload(self, source: dict) -> dict:
        return {k: source[k] for k in SCRUBBER_WHITELIST if k in source}

    def list_projects(self, dest_org: str) -> list:
        results = []
        url = f"{self.base_url}/organizations/{dest_org}/projects/"
        params = None
        while url:
            resp = requests.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            results.extend(resp.json())
            url, params = _next_link(resp.headers.get("Link")), None
        return results

    def _put(self, url: str, payload: dict, label: str) -> dict:
        if self.dry_run:
            logger.info(f"  action      : [DRY-RUN] would PUT {url} (not sent)")
            return {"dry_run": True}
        try:
            resp = requests.put(url, headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update scrubbers for {label}: {e}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    def update_org(self, dest_org: str, payload: dict) -> dict:
        return self._put(f"{self.base_url}/organizations/{dest_org}/", payload, f"org '{dest_org}'")

    def update_project(self, dest_org: str, dest_slug: str, payload: dict) -> dict:
        return self._put(f"{self.base_url}/projects/{dest_org}/{dest_slug}/", payload, f"project '{dest_slug}'")

    def _get(self, url: str) -> dict:
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def verify_org(self, dest_org: str, payload: dict) -> dict:
        return self._diff(self._get(f"{self.base_url}/organizations/{dest_org}/"), payload)

    def verify_project(self, dest_org: str, dest_slug: str, payload: dict) -> dict:
        return self._diff(self._get(f"{self.base_url}/projects/{dest_org}/{dest_slug}/"), payload)

    @staticmethod
    def _diff(current: dict, payload: dict) -> dict:
        mismatches = {}
        for k, expected in payload.items():
            actual = current.get(k)
            if actual != expected:
                mismatches[k] = {"expected": expected, "actual": actual}
        return mismatches


def _print_block(title: str, subtitle: str, payload: dict, excluded_present: dict):
    logger.info("")
    logger.info("-" * 64)
    logger.info(title)
    if subtitle:
        logger.info(subtitle)
    if payload:
        key_w = max(len(k) for k in payload)
        logger.info(f"  scrubbers applied ({len(payload)}):")
        for k, v in payload.items():
            logger.info(f"      {k.ljust(key_w)} = {_fmt(v)}")
    else:
        logger.info("  scrubbers applied (0): none present on source")
    if excluded_present:
        logger.info(f"  excluded    : advanced fields not migrated (see DECISIONS.md D5): {list(excluded_present)}")


def _verify_line(dry_run: bool, mismatches: dict):
    if dry_run:
        logger.info("  verify      : skipped (dry-run)")
    elif mismatches:
        logger.warning(f"  verify      : MISMATCH {_fmt(mismatches, 200)}")
    else:
        logger.info("  verify      : passed")


def main():
    parser = argparse.ArgumentParser(description="Migrate data-scrubbing settings self-hosted -> SaaS")
    parser.add_argument("auth_token", help="SaaS auth token (needs org:write + project:write)")
    parser.add_argument("dest_org", help="Destination SaaS org slug")
    parser.add_argument("--source-token", required=True, help="Self-hosted read token (org:read, project:read)")
    parser.add_argument("--source-org", default="migration-test-org", help="Self-hosted org slug")
    parser.add_argument("--source-url", default="http://127.0.0.1:9000/api/0", help="Self-hosted API base URL")
    parser.add_argument("--saas-url", default="https://sentry.io/api/0", help="SaaS API base URL")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--org-only", action="store_true", help="Migrate only org-level scrubbers")
    group.add_argument("--projects-only", action="store_true", help="Migrate only project-level scrubbers")
    parser.add_argument("--dry-run", action="store_true", help="Log intended PUTs without sending them")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN: no changes will be made to SaaS ===")

    source = SelfHostedSource(args.source_token, base_url=args.source_url)
    migrator = ScrubberMigrator(args.auth_token, base_url=args.saas_url, dry_run=args.dry_run)

    results = {
        "timestamp": datetime.now().isoformat(),
        "source_org": args.source_org,
        "dest_org": args.dest_org,
        "dry_run": args.dry_run,
        "excluded_advanced": EXCLUDED_ADVANCED,
    }

    do_org = not args.projects_only
    do_projects = not args.org_only

    # ---- org-level ----
    if do_org:
        src_org = source.get_org(args.source_org)
        org_payload = migrator.build_payload(src_org)
        org_excluded = {k: src_org.get(k) for k in EXCLUDED_ADVANCED if k in src_org}
        _print_block("ORG scrubbers", f"  org         : {args.source_org} -> {args.dest_org}",
                     org_payload, org_excluded)
        migrator.update_org(args.dest_org, org_payload)
        org_mismatches = {} if args.dry_run else migrator.verify_org(args.dest_org, org_payload)
        _verify_line(args.dry_run, org_mismatches)
        results["org"] = {
            "applied": org_payload,
            "excluded_present": org_excluded,
            "verification_mismatches": org_mismatches,
        }

    # ---- project-level ----
    per_project = []
    unmatched = []
    if do_projects:
        src_projects = source.get_projects(args.source_org)
        dest_projects = migrator.list_projects(args.dest_org)
        dest_by_name = {p["name"].strip().lower(): p for p in dest_projects}

        for src in src_projects:
            name = src.get("name", "")
            dest = dest_by_name.get(name.strip().lower())
            if not dest:
                logger.warning(f"\nNo SaaS project named '{name}' (source slug '{src.get('slug')}') -- skipping.")
                unmatched.append({"source_name": name, "source_slug": src.get("slug")})
                continue

            dest_slug = dest["slug"]
            src_full = source.get_project(args.source_org, src["slug"])
            payload = migrator.build_payload(src_full)
            excluded = {k: src_full.get(k) for k in EXCLUDED_ADVANCED if k in src_full}
            _print_block(f"PROJECT scrubbers: {name}",
                         f"  {src['slug']} -> {dest_slug}", payload, excluded)
            migrator.update_project(args.dest_org, dest_slug, payload)
            mismatches = {} if args.dry_run else migrator.verify_project(args.dest_org, dest_slug, payload)
            _verify_line(args.dry_run, mismatches)
            per_project.append({
                "source_name": name,
                "source_slug": src["slug"],
                "dest_slug": dest_slug,
                "applied": payload,
                "excluded_present": excluded,
                "verification_mismatches": mismatches,
            })
        results["projects"] = per_project
        results["unmatched"] = unmatched

    with open("data_scrubbers_migration_results.json", "w") as f:
        json.dump(results, f, indent=2)

    # ---- summary ----
    logger.info("")
    logger.info("=" * 64)
    mode = "DRY RUN (nothing written)" if args.dry_run else "LIVE"
    logger.info(f"Summary [{mode}]:")
    if do_org:
        status = "would apply" if args.dry_run else ("OK" if not results["org"]["verification_mismatches"] else "MISMATCH")
        logger.info(f"  org    {args.dest_org}  ({len(results['org']['applied'])} scrubbers)  {status}")
    if do_projects:
        logger.info(f"  projects: matched {len(per_project)}, unmatched {len(unmatched)}")
        if per_project:
            name_w = max(len(p["source_name"]) for p in per_project)
            for p in per_project:
                status = "would apply" if args.dry_run else ("OK" if not p["verification_mismatches"] else "MISMATCH")
                logger.info(f"    {p['source_name'].ljust(name_w)}  -> {p['dest_slug']}  ({len(p['applied'])} scrubbers)  {status}")
        for u in unmatched:
            logger.info(f"    [UNMATCHED] {u['source_name']} (source slug '{u['source_slug']}')")
    logger.info("")
    logger.info("Wrote data_scrubbers_migration_results.json")


if __name__ == "__main__":
    main()
