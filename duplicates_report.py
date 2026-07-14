"""Step 1 of the migration suite: cross-org duplicates / collision report (offline, export-based).

When several self-hosted orgs are consolidated into ONE SaaS org, project names, team slugs, and team
names can collide -- and a team that exists in two orgs may have a *different roster* in each. This tool
reads one JSON export per org and reports those overlaps BEFORE any migration runs, so they can be
resolved (rename / merge / drop) up front.

Source: JSON exports only (no live instance) -- see DECISIONS.md D7. One export file == one org.

What counts as a hard blocker for a merged create (vs. informational):
  - PROJECT NAME collision  -> HARD. Projects are created by name and SaaS derives the slug from it, so
    two projects with the same name across orgs would produce the same slug and clash.
  - TEAM SLUG collision      -> HARD. Teams are created with an explicit slug, which must be unique.
  - TEAM NAME collision      -> informational, but flagged with a MEMBERSHIP DIFF (same team name, but a
    different set of people in each org -- a real merge hazard).
  - PROJECT SLUG collision   -> informational (slug is not sent on project create).
  - SIMILAR ORG NAMES        -> informational (helps spot Dor-Org1 / Dor-Org2 / Dor-Org3 families).

Usage:
  python duplicates_report.py org1.json org2.json [org3.json ...] [--label PATH=DisplayName ...]
      [--similarity 0.6] [--out duplicate_report.json]

Writes duplicate_report.json and exits non-zero if any HARD collision is found.
"""
import sys
import json
import argparse
import logging
from difflib import SequenceMatcher
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def load(path: str):
    with open(path, "r") as f:
        return json.load(f)


def build_org(path: str, data: list, label: str = None) -> dict:
    """Reduce one export file to a compact per-org model.

    Returns: {source_file, slug, name, teams: {slug: {slug,name,members:set}}, projects: [{slug,name}]}.
    Team members are resolved within the file: organizationmemberteam(member_pk, team_pk) ->
    organizationmember(user_email).
    """
    org_name = org_slug = None
    teams = {}                      # team_pk -> {slug, name, members:set}
    members = {}                    # member_pk -> email
    project_list = []               # [{slug, name}]
    memberteam = []                 # (member_pk, team_pk)

    for item in data:
        model = item.get("model")
        pk = item.get("pk")
        f = item.get("fields", {})
        if model == "sentry.organization":
            org_name = f.get("name")
            org_slug = f.get("slug")
        elif model == "sentry.team":
            teams[pk] = {"slug": f.get("slug"), "name": f.get("name"), "members": set()}
        elif model == "sentry.organizationmember":
            email = f.get("user_email") or f.get("email")
            if email:
                members[pk] = email
        elif model == "sentry.organizationmemberteam":
            memberteam.append((f.get("organizationmember"), f.get("team")))
        elif model == "sentry.project":
            project_list.append({"slug": f.get("slug"), "name": f.get("name")})

    for member_pk, team_pk in memberteam:
        if team_pk in teams and member_pk in members:
            teams[team_pk]["members"].add(members[member_pk])

    # Identity precedence: explicit --label, then org slug, then org name, then filename.
    display = label or org_slug or org_name or path.rsplit("/", 1)[-1]
    return {
        "source_file": path,
        "slug": org_slug or display,
        "name": org_name or display,
        "display": display,
        "teams": teams,
        "projects": project_list,
    }


def _group(orgs, extractor):
    """Group by a normalized key across orgs. extractor(org) -> list of (key, payload).
    Returns key -> list of (org_display, payload) with >1 org represented."""
    grouped = defaultdict(list)
    for org in orgs:
        for key, payload in extractor(org):
            if key:
                grouped[key].append((org["display"], payload))
    return {k: v for k, v in grouped.items() if len({d for d, _ in v}) > 1}


def project_collisions(orgs):
    name_dups = _group(orgs, lambda o: [(_norm(p["name"]), p) for p in o["projects"]])
    slug_dups = _group(orgs, lambda o: [(_norm(p["slug"]), p) for p in o["projects"]])
    return name_dups, slug_dups


def team_collisions_with_membership(orgs, key_field):
    """Group teams across orgs by key_field ('name' or 'slug'); attach a membership diff per group."""
    grouped = defaultdict(dict)  # key -> {org_display: set(members)}
    display_name = {}            # key -> a representative original label
    for org in orgs:
        for t in org["teams"].values():
            key = _norm(t.get(key_field))
            if not key:
                continue
            grouped[key].setdefault(org["display"], set()).update(t["members"])
            display_name.setdefault(key, t.get(key_field))

    collisions = {}
    for key, per_org in grouped.items():
        if len(per_org) < 2:
            continue
        all_sets = list(per_org.values())
        common = set.intersection(*all_sets) if all_sets else set()
        membership = {}
        for org_display, mset in per_org.items():
            others = set().union(*[s for d, s in per_org.items() if d != org_display]) if len(per_org) > 1 else set()
            membership[org_display] = {
                "members": sorted(mset),
                "unique_to_this_org": sorted(mset - others),
            }
        collisions[key] = {
            "label": display_name[key],
            "orgs": sorted(per_org.keys()),
            "common_members": sorted(common),
            "membership": membership,
            "identical_rosters": all(s == all_sets[0] for s in all_sets),
        }
    return collisions


def similar_org_names(orgs, threshold: float):
    pairs = []
    for i in range(len(orgs)):
        for j in range(i + 1, len(orgs)):
            a, b = orgs[i], orgs[j]
            ratio = SequenceMatcher(None, _norm(a["name"]), _norm(b["name"])).ratio()
            if ratio >= threshold:
                pairs.append({"a": a["display"], "b": b["display"], "ratio": round(ratio, 3)})
    return sorted(pairs, key=lambda p: p["ratio"], reverse=True)


def _print_project_section(title, dups, note):
    logger.info(f"\n=== {title} ===")
    if not dups:
        logger.info("  none")
        return
    for key, group in sorted(dups.items()):
        where = ", ".join(f"{org} (slug '{p['slug']}', name '{p['name']}')" for org, p in group)
        logger.info(f"  '{key}' in {len(group)} orgs: {where}   [{note}]")


def _print_team_section(title, collisions, note):
    logger.info(f"\n=== {title} ===")
    if not collisions:
        logger.info("  none")
        return
    for key, info in sorted(collisions.items()):
        roster = "identical rosters" if info["identical_rosters"] else "DIFFERENT rosters"
        logger.info(f"  '{info['label']}' in {', '.join(info['orgs'])}  [{note}; {roster}]")
        if info["common_members"]:
            logger.info(f"      common: {', '.join(info['common_members'])}")
        else:
            logger.info("      common: (none)")
        for org_display, m in info["membership"].items():
            uniq = ", ".join(m["unique_to_this_org"]) or "(none)"
            logger.info(f"      {org_display}: members [{', '.join(m['members']) or '(none)'}] | unique [{uniq}]")


def main():
    parser = argparse.ArgumentParser(description="Cross-org duplicates/collision report from JSON exports")
    parser.add_argument("exports", nargs="+", help="One export.json per org")
    parser.add_argument("--label", action="append", default=[], metavar="PATH=NAME",
                        help="Override an org's display name for a given export path (repeatable)")
    parser.add_argument("--similarity", type=float, default=0.6,
                        help="Org-name similarity threshold 0..1 for the 'similar names' section (default 0.6)")
    parser.add_argument("--out", default="duplicate_report.json", help="Output JSON path")
    args = parser.parse_args()

    labels = {}
    for pair in args.label:
        if "=" in pair:
            path, name = pair.split("=", 1)
            labels[path] = name

    orgs = []
    for path in args.exports:
        org = build_org(path, load(path), label=labels.get(path))
        orgs.append(org)
        logger.info(f"Loaded {path}: org '{org['display']}' "
                    f"({len(org['teams'])} teams, {len(org['projects'])} projects)")

    proj_name_dups, proj_slug_dups = project_collisions(orgs)
    team_slug_dups = team_collisions_with_membership(orgs, "slug")
    team_name_dups = team_collisions_with_membership(orgs, "name")
    similar = similar_org_names(orgs, args.similarity)

    _print_project_section("PROJECT NAME collisions (HARD - derived slug will clash)", proj_name_dups, "HARD")
    _print_team_section("TEAM SLUG collisions (HARD - slug must be unique)", team_slug_dups, "HARD")
    _print_team_section("TEAM NAME collisions (informational - watch roster diffs)", team_name_dups, "info")
    _print_project_section("PROJECT SLUG collisions (informational - slug not sent on create)", proj_slug_dups, "info")

    logger.info("\n=== SIMILAR ORG NAMES (informational) ===")
    if similar:
        for p in similar:
            logger.info(f"  '{p['a']}' ~ '{p['b']}'  (ratio {p['ratio']})")
    else:
        logger.info("  none above threshold")

    def _proj_json(dups):
        return {k: [{"org": o, "slug": p["slug"], "name": p["name"]} for o, p in g] for k, g in dups.items()}

    report = {
        "orgs": [{"display": o["display"], "slug": o["slug"], "name": o["name"],
                  "source_file": o["source_file"], "teams": len(o["teams"]),
                  "projects": len(o["projects"])} for o in orgs],
        "project_name_collisions_HARD": _proj_json(proj_name_dups),
        "team_slug_collisions_HARD": team_slug_dups,
        "team_name_collisions_info": team_name_dups,
        "project_slug_collisions_info": _proj_json(proj_slug_dups),
        "similar_org_names": similar,
    }
    hard = len(proj_name_dups) + len(team_slug_dups)
    report["summary"] = {
        "orgs": len(orgs),
        "hard_collisions": hard,
        "project_name_collisions": len(proj_name_dups),
        "team_slug_collisions": len(team_slug_dups),
        "team_name_collisions": len(team_name_dups),
        "project_slug_collisions": len(proj_slug_dups),
        "similar_org_name_pairs": len(similar),
    }

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    logger.info("\n" + "=" * 64)
    logger.info(f"Summary: {len(orgs)} orgs | HARD collisions: {hard} "
                f"(project-name {len(proj_name_dups)}, team-slug {len(team_slug_dups)}) | "
                f"info: team-name {len(team_name_dups)}, project-slug {len(proj_slug_dups)}, "
                f"similar-names {len(similar)}")
    logger.info(f"Wrote {args.out}")

    if hard:
        logger.info(f"\nFOUND {hard} HARD collision group(s) that will break a merged migration. "
                    f"Resolve (rename/merge/drop) before migrating.")
        sys.exit(2)
    logger.info("\nNo hard collisions detected.")


if __name__ == "__main__":
    main()
