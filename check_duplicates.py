"""Pre-run duplicate detection for Sentry self-hosted -> SaaS migration.

When multiple self-hosted instances are merged into ONE SaaS org, team/project
names and slugs can collide. SaaS requires unique slugs per org, so a collision
means the second create will fail (or silently merge). Run this BEFORE migrating
to see what will clash.

Usage:
  python check_duplicates.py export1.json [export2.json ...]

Reports:
  - slug collisions across the provided exports (these WILL fail on create)
  - name collisions across the provided exports (informational)
Writes duplicate_report.json and exits non-zero if any slug collision is found.
"""
import json
import sys
import argparse
import logging
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def load(path):
    with open(path, "r") as f:
        return json.load(f)


def collect(path, data):
    """Return teams and projects as lists of (slug, name, source_file)."""
    teams, projects = [], []
    for item in data:
        model = item.get("model")
        fields = item.get("fields", {})
        if model == "sentry.team":
            teams.append((fields.get("slug"), fields.get("name"), path))
        elif model == "sentry.project":
            projects.append((fields.get("slug"), fields.get("name"), path))
    return teams, projects


def find_collisions(entries, key_index):
    """Group entries by a key (0=slug, 1=name); return only keys with >1 source file."""
    grouped = defaultdict(list)
    for entry in entries:
        key = entry[key_index]
        if key is None:
            continue
        grouped[key].append(entry)
    collisions = {}
    for key, group in grouped.items():
        sources = {e[2] for e in group}
        if len(group) > 1 and (len(sources) > 1 or len(group) > 1):
            collisions[key] = group
    return collisions


def report_section(title, collisions, key_name):
    print(f"\n=== {title} ===")
    if not collisions:
        print("  none")
        return
    for key, group in sorted(collisions.items()):
        srcs = ", ".join(f"{name} ({src})" for _slug, name, src in group)
        print(f"  {key_name} '{key}' appears {len(group)}x: {srcs}")


def main():
    parser = argparse.ArgumentParser(description="Detect duplicate team/project names & slugs across exports")
    parser.add_argument("exports", nargs="+", help="One or more export.json files")
    args = parser.parse_args()

    all_teams, all_projects = [], []
    for path in args.exports:
        data = load(path)
        teams, projects = collect(path, data)
        all_teams.extend(teams)
        all_projects.extend(projects)
        logger.info(f"Loaded {path}: {len(teams)} teams, {len(projects)} projects")

    team_slug_dups = find_collisions(all_teams, 0)
    team_name_dups = find_collisions(all_teams, 1)
    project_slug_dups = find_collisions(all_projects, 0)
    project_name_dups = find_collisions(all_projects, 1)

    report_section("Team SLUG collisions (WILL fail on create)", team_slug_dups, "team slug")
    report_section("Project SLUG collisions (WILL fail on create)", project_slug_dups, "project slug")
    report_section("Team NAME collisions (informational)", team_name_dups, "team name")
    report_section("Project NAME collisions (informational)", project_name_dups, "project name")

    report = {
        "team_slug_collisions": {k: [list(e) for e in v] for k, v in team_slug_dups.items()},
        "project_slug_collisions": {k: [list(e) for e in v] for k, v in project_slug_dups.items()},
        "team_name_collisions": {k: [list(e) for e in v] for k, v in team_name_dups.items()},
        "project_name_collisions": {k: [list(e) for e in v] for k, v in project_name_dups.items()},
    }
    with open("duplicate_report.json", "w") as f:
        json.dump(report, f, indent=2)
    print("\nWrote duplicate_report.json")

    hard_collisions = len(team_slug_dups) + len(project_slug_dups)
    if hard_collisions:
        print(f"\nFOUND {hard_collisions} slug collision group(s) that will break a live run. Resolve before migrating.")
        sys.exit(2)
    print("\nNo slug collisions detected.")


if __name__ == "__main__":
    main()
