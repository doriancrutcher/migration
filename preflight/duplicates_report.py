"""Step 1 of the migration suite: cross-org duplicates / collision report (offline, export-based).

When several self-hosted orgs are consolidated into ONE SaaS org, project names, team slugs, and team
names can collide -- and a team that exists in two orgs may have a *different roster* in each. This tool
reads one JSON export per org and reports those overlaps BEFORE any migration runs, so they can be
resolved (rename / merge / drop) up front.

Source: JSON exports only (no live instance) -- see DECISIONS.md D7. One export file == one org.

What counts as a hard blocker for a merged create (vs. informational):
  - PROJECT collision        -> DANGER. Projects are created by name and SaaS derives the slug from it, so
    projects whose names slugify to the same value clash. Detected on the DERIVED slug (slugify(name)),
    which also catches different names that map to the same slug (e.g. "Payments API" vs "payments-api").
  - TEAM SLUG collision      -> DANGER. Teams are created with an explicit slug, which must be unique.
  - TEAM NAME collision      -> informational, but flagged with a MEMBERSHIP DIFF (same team name, but a
    different set of people in each org -- a real merge hazard).
  - SIMILAR ORG NAMES        -> informational (helps spot Dor-Org1 / Dor-Org2 / Dor-Org3 families).

Usage:
  python duplicates_report.py org1.json org2.json [org3.json ...] [--label PATH=DisplayName ...]
      [--similarity 0.6] [--out duplicate_report.json] [--html [duplicate_report.html]]

Writes duplicate_report.json (and, with --html, a self-contained duplicate_report.html) and exits
non-zero if any HARD collision is found.
"""
import re
import sys
import json
import html as html_lib
import argparse
import logging
from datetime import datetime
from difflib import SequenceMatcher
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def slugify(name: str) -> str:
    """Approximate how Sentry derives a project slug from its name (lowercase, non-alphanumeric -> '-').
    This is what actually gets created on SaaS, so two names that slugify the same will clash."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


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
    """Group projects across orgs by their DERIVED slug (slugify(name)). The migration creates projects by
    name and SaaS derives the slug, so a shared derived slug is what actually clashes in a merged org --
    this also catches different names that slugify to the same value (e.g. 'Payments API' vs 'payments-api')."""
    def extract(o):
        rows = []
        for p in o["projects"]:
            derived = slugify(p["name"]) or _norm(p["slug"])
            rows.append((derived, {"slug": p["slug"], "name": p["name"], "derived": derived}))
        return rows
    return _group(orgs, extract)


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
        where = ", ".join(f"{org} (name '{p['name']}', slug '{p['slug']}')" for org, p in group)
        logger.info(f"  derived-slug '{key}' in {len(group)} orgs: {where}   [{note}]")


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


def esc(s) -> str:
    return html_lib.escape(str(s), quote=True)


def _html_project_section(title, note_class, dups):
    if not dups:
        return f'<section><h2>{esc(title)}</h2><p class="empty">none</p></section>'
    rows = []
    for key, group in sorted(dups.items()):
        items = []
        for item in group:
            note = ""
            if _norm(item["slug"]) != _norm(item.get("derived_slug", "")):
                note = f' <span class="note">(source slug <code>{esc(item["slug"])}</code>)</span>'
            items.append(
                f'<li><span class="org">{esc(item["org"])}</span> '
                f'&mdash; name &ldquo;{esc(item["name"])}&rdquo;{note}</li>'
            )
        orgs = "".join(items)
        rows.append(
            f'<tr><td class="key"><code>{esc(key)}</code></td>'
            f'<td><span class="badge {note_class}">{esc(note_class.upper())}</span></td>'
            f'<td><ul class="orglist">{orgs}</ul></td></tr>'
        )
    return (
        f'<section><h2>{esc(title)}</h2>'
        f'<table><thead><tr><th>Derived slug</th><th>Severity</th><th>Appears in</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></section>'
    )


def _html_team_section(title, note_class, collisions):
    if not collisions:
        return f'<section><h2>{esc(title)}</h2><p class="empty">none</p></section>'
    blocks = []
    for key, info in sorted(collisions.items()):
        diff_badge = (
            '<span class="badge different">DIFFERENT rosters</span>'
            if not info["identical_rosters"]
            else '<span class="badge same">identical rosters</span>'
        )
        common = (
            ", ".join(esc(m) for m in info["common_members"]) if info["common_members"] else "(none)"
        )
        per_org = []
        for org_display, m in info["membership"].items():
            members = ", ".join(esc(x) for x in m["members"]) or "(none)"
            unique = ", ".join(esc(x) for x in m["unique_to_this_org"]) or "(none)"
            per_org.append(
                f'<div class="orgroster"><div class="org">{esc(org_display)}</div>'
                f'<div class="mline"><span class="mlabel">members:</span> {members}</div>'
                f'<div class="mline"><span class="mlabel">unique to this org:</span> '
                f'<span class="unique">{unique}</span></div></div>'
            )
        blocks.append(
            f'<div class="teamblock">'
            f'<div class="teamhead"><code class="key">{esc(info["label"])}</code> '
            f'<span class="badge {note_class}">{esc(note_class.upper())}</span> {diff_badge} '
            f'<span class="inorgs">in {esc(", ".join(info["orgs"]))}</span></div>'
            f'<div class="common"><span class="mlabel">common members:</span> {common}</div>'
            f'<div class="rosters">{"".join(per_org)}</div></div>'
        )
    return f'<section><h2>{esc(title)}</h2>{"".join(blocks)}</section>'


def render_html(report: dict, exports: list, generated_at: str) -> str:
    s = report["summary"]
    hard = s["hard_collisions"]
    hard_class = "danger" if hard else "ok"

    org_cards = "".join(
        f'<div class="card"><div class="cardname">{esc(o["display"])}</div>'
        f'<div class="cardsub">{esc(o["name"])}</div>'
        f'<div class="cardstats">{o["teams"]} teams &middot; {o["projects"]} projects</div>'
        f'<div class="cardfile"><code>{esc(o["source_file"])}</code></div></div>'
        for o in report["orgs"]
    )

    files = ", ".join(esc(f) for f in exports)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duplicate / collision report</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    max-width: 980px; margin: 0 auto; padding: 24px; line-height: 1.5; color: #1a1a1a; background: #fff; }}
  h1 {{ margin: 0 0 4px; font-size: 24px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
  .meta code {{ font-size: 12px; }}
  .summary {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
    border: 1px solid #e2e2e2; border-radius: 10px; padding: 16px; margin-bottom: 24px; }}
  .bignum {{ font-size: 40px; font-weight: 700; line-height: 1; padding: 6px 16px; border-radius: 8px; }}
  .bignum.danger {{ background: #fdecea; color: #b3261e; }}
  .bignum.ok {{ background: #e8f5e9; color: #1e7e34; }}
  .counts {{ font-size: 14px; color: #333; }}
  .counts b {{ font-size: 16px; }}
  .legend {{ border: 1px solid #e2e2e2; border-radius: 10px; padding: 12px 16px; margin-bottom: 24px; }}
  .legtitle {{ font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
    color: #666; margin-bottom: 8px; }}
  .legitem {{ font-size: 13px; margin-bottom: 6px; }}
  .legitem .badge {{ margin-right: 8px; vertical-align: middle; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }}
  .card {{ border: 1px solid #e2e2e2; border-radius: 8px; padding: 12px 14px; min-width: 160px; }}
  .cardname {{ font-weight: 600; }}
  .cardsub {{ color: #666; font-size: 13px; }}
  .cardstats {{ margin-top: 6px; font-size: 13px; }}
  .cardfile {{ margin-top: 4px; font-size: 11px; color: #888; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 17px; border-bottom: 2px solid #eee; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; vertical-align: top; }}
  th {{ color: #555; font-weight: 600; background: #fafafa; }}
  code {{ background: #f2f2f2; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  .key {{ font-weight: 600; }}
  .orglist {{ margin: 0; padding-left: 18px; }}
  .note {{ color: #888; font-size: 12px; }}
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 999px;
    letter-spacing: .02em; }}
  .badge.danger {{ background: #fdecea; color: #b3261e; }}
  .badge.info {{ background: #fff4e5; color: #8a5a00; }}
  .badge.different, .badge.same {{ background: #ececec; color: #444; }}
  .empty {{ color: #1e7e34; font-style: italic; }}
  .teamblock {{ border: 1px solid #e2e2e2; border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }}
  .teamhead {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }}
  .inorgs {{ color: #666; font-size: 13px; }}
  .common {{ font-size: 13px; margin-bottom: 8px; }}
  .rosters {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .orgroster {{ background: #fafafa; border: 1px solid #eee; border-radius: 6px; padding: 8px 10px;
    font-size: 13px; min-width: 220px; flex: 1; }}
  .orgroster .org {{ font-weight: 600; margin-bottom: 4px; }}
  .mlabel {{ color: #666; }}
  .unique {{ color: #b3261e; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e6e6; background: #141414; }}
    .summary, .card, .teamblock, .legend {{ border-color: #333; }}
    .legtitle, .legitem {{ color: #9a9a9a; }}
    th {{ background: #1e1e1e; color: #bbb; }}
    th, td {{ border-color: #2a2a2a; }}
    code {{ background: #262626; }}
    .orgroster {{ background: #1c1c1c; border-color: #2a2a2a; }}
    h2 {{ border-color: #2a2a2a; }}
    .cardsub, .cardfile, .inorgs, .mlabel, .counts {{ color: #9a9a9a; }}
    .unique {{ color: #ff6b5e; }}
    .badge.different, .badge.same {{ background: #2e2e2e; color: #cfcfcf; }}
  }}
</style></head>
<body>
  <h1>Duplicate / collision report</h1>
  <div class="meta">Generated {esc(generated_at)} &middot; sources: <code>{files}</code></div>

  <div class="summary">
    <div class="bignum {hard_class}">{hard}</div>
    <div class="counts">
      <div><b>{hard}</b> Danger collision group(s) &mdash; block a merged migration</div>
      <div>project {s["project_collisions"]} &middot; team-slug {s["team_slug_collisions"]}
        &middot; team-name {s["team_name_collisions"]} (info) &middot;
        similar-names {s["similar_org_name_pairs"]} (info)</div>
    </div>
  </div>

  <div class="legend">
    <div class="legtitle">Severity reference</div>
    <div class="legitem"><span class="badge danger">DANGER</span>Will break a merged migration: the
      create fails or silently merges into the wrong object. Resolve (rename / merge / drop) before
      migrating. Any Danger group makes the tool exit non-zero. Covers project collisions (names that map
      to the same derived slug) and team-slug collisions.</div>
    <div class="legitem"><span class="badge info">INFO</span>Won't block the migration, but a human should
      review it. Covers team-name collisions (watch for different rosters).</div>
  </div>

  <div class="cards">{org_cards}</div>

  {_html_project_section("Project collisions (Danger - names map to the same derived slug)", "danger", report["project_collisions_HARD"])}
  {_html_team_section("Team slug collisions (Danger - slug must be unique)", "danger", report["team_slug_collisions_HARD"])}
  {_html_team_section("Team name collisions (Info - watch roster diffs)", "info", report["team_name_collisions_info"])}
</body></html>
"""


def main():
    parser = argparse.ArgumentParser(description="Cross-org duplicates/collision report from JSON exports")
    parser.add_argument("exports", nargs="+", help="One export.json per org")
    parser.add_argument("--label", action="append", default=[], metavar="PATH=NAME",
                        help="Override an org's display name for a given export path (repeatable)")
    parser.add_argument("--similarity", type=float, default=0.6,
                        help="Org-name similarity threshold 0..1 for the 'similar names' section (default 0.6)")
    parser.add_argument("--out", default="duplicate_report.json", help="Output JSON path")
    parser.add_argument("--html", nargs="?", const="duplicate_report.html", default=None, metavar="PATH",
                        help="Also write a self-contained HTML report (default duplicate_report.html)")
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

    proj_dups = project_collisions(orgs)
    team_slug_dups = team_collisions_with_membership(orgs, "slug")
    team_name_dups = team_collisions_with_membership(orgs, "name")
    similar = similar_org_names(orgs, args.similarity)

    _print_project_section("PROJECT collisions (DANGER - names map to the same derived slug)", proj_dups, "DANGER")
    _print_team_section("TEAM SLUG collisions (DANGER - slug must be unique)", team_slug_dups, "DANGER")
    _print_team_section("TEAM NAME collisions (INFO - watch roster diffs)", team_name_dups, "info")

    logger.info("\n=== SIMILAR ORG NAMES (informational) ===")
    if similar:
        for p in similar:
            logger.info(f"  '{p['a']}' ~ '{p['b']}'  (ratio {p['ratio']})")
    else:
        logger.info("  none above threshold")

    def _proj_json(dups):
        return {k: [{"org": o, "slug": p["slug"], "name": p["name"], "derived_slug": p["derived"]}
                    for o, p in g] for k, g in dups.items()}

    report = {
        "orgs": [{"display": o["display"], "slug": o["slug"], "name": o["name"],
                  "source_file": o["source_file"], "teams": len(o["teams"]),
                  "projects": len(o["projects"])} for o in orgs],
        "project_collisions_HARD": _proj_json(proj_dups),
        "team_slug_collisions_HARD": team_slug_dups,
        "team_name_collisions_info": team_name_dups,
        "similar_org_names": similar,
    }
    hard = len(proj_dups) + len(team_slug_dups)
    report["summary"] = {
        "orgs": len(orgs),
        "hard_collisions": hard,
        "project_collisions": len(proj_dups),
        "team_slug_collisions": len(team_slug_dups),
        "team_name_collisions": len(team_name_dups),
        "similar_org_name_pairs": len(similar),
    }

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    if args.html:
        with open(args.html, "w") as f:
            f.write(render_html(report, args.exports, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    logger.info("\n" + "=" * 64)
    logger.info(f"Summary: {len(orgs)} orgs | Danger collisions: {hard} "
                f"(project {len(proj_dups)}, team-slug {len(team_slug_dups)}) | "
                f"info: team-name {len(team_name_dups)}, similar-names {len(similar)}")
    logger.info(f"Wrote {args.out}")
    if args.html:
        logger.info(f"Wrote {args.html}")

    if hard:
        logger.info(f"\nFOUND {hard} Danger collision group(s) that will break a merged migration. "
                    f"Resolve (rename/merge/drop) before migrating.")
        sys.exit(2)
    logger.info("\nNo hard collisions detected.")


if __name__ == "__main__":
    main()
