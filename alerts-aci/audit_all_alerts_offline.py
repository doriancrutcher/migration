#!/usr/bin/env python3
"""
audit_all_alerts_offline.py — OFFLINE. Compares EVERY alert in one or more self-hosted exports
(e.g. several instances merged into one SaaS org) with an org snapshot from snapshot_org_alerts.py.
One row per source alert:

  PRESENT        alert with the same name on the same project (COPIES=n if more than one)
  MISSING        nothing with that name on the project
  NO-PROJECT     the project does not exist in the org
  plus flags:    MUTED/DISABLED at source, SENTRY-DEFAULT (default name + default action),
                 CUSTOMIZED-DEFAULT (default name, but notifies someone specific)

    python3 audit_all_alerts_offline.py org_snapshot_<org>_<ts>.json \
        --export "V24=export_v24.json:org-a" --export "V25=export_v25.json:org-b,org-c"

--export is LABEL=PATH:ORG[,ORG...] (repeatable): a label for the source column, the export
file, and which source org slug(s) inside it to include.
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LABELS = {"Send a notification for high priority issues", "Send a notification for new issues",
                  "Send a notification for new issues (Legacy)", "Send a notification for new and regressed issues"}


def is_default_action(actions):
    return all(a.get("id", "").endswith("NotifyEmailAction") and a.get("targetType") == "IssueOwners"
               for a in actions) and actions


def action_summary(actions, users, teams):
    out = []
    for a in actions:
        aid = a.get("id", "").split(".")[-1]
        if aid == "NotifyEmailAction":
            tt = a.get("targetType")
            if tt == "Member":
                out.append("email " + (users.get(int(a["targetIdentifier"]), {}).get("email") or f"user {a['targetIdentifier']}"))
            elif tt == "Team":
                out.append("email team " + teams.get(int(a["targetIdentifier"]), str(a["targetIdentifier"])))
            else:
                out.append("email issue owners")
        elif "Slack" in aid:
            out.append("Slack #" + str(a.get("channel", "")).lstrip("#"))
        elif "PagerDuty" in aid:
            out.append("PagerDuty")
        else:
            out.append(aid)
    return "; ".join(out)


def parse_export(spec):
    label, eq, rest = spec.partition("=")
    path, colon, orgs = rest.rpartition(":")
    if not (eq and colon and label and path and orgs):
        raise argparse.ArgumentTypeError(f"--export {spec!r} must look like LABEL=PATH:ORG[,ORG]")
    return label, path, {o.strip() for o in orgs.split(",") if o.strip()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshot", help="org_snapshot_<org>_<ts>.json from snapshot_org_alerts.py")
    ap.add_argument("--export", dest="exports", action="append", type=parse_export, required=True,
                    metavar="LABEL=PATH:ORG[,ORG]")
    args = ap.parse_args()
    snap = json.load(open(args.snapshot))
    proj_id = {p["slug"]: str(p["id"]) for p in snap["projects"]}
    stream = {str(d["projectId"]): str(d["id"]) for d in snap["detectors"] if d.get("type") == "issue_stream"}
    wf_by_det = defaultdict(list)
    for w in snap["workflows"]:
        for d in w.get("detectorIds") or []:
            wf_by_det[str(d)].append(w)
    met_by_proj = defaultdict(list)
    for d in snap["detectors"]:
        if d.get("type") == "metric_issue":
            met_by_proj[str(d.get("projectId"))].append(d)

    rows = []
    for src, path, orgslugs in args.exports:
        d = [x for x in json.load(open(path)) if isinstance(x, dict)]
        orgs = {i["pk"]: i["fields"]["slug"] for i in d if i.get("model") == "sentry.organization"}
        keep_org = {pk for pk, s in orgs.items() if s in orgslugs}
        proj = {i["pk"]: i["fields"]["slug"] for i in d if i.get("model") == "sentry.project"
                and i["fields"].get("organization") in keep_org}
        users = {i["pk"]: i["fields"] for i in d if i.get("model") == "sentry.user"}
        teams = {i["pk"]: i["fields"]["slug"] for i in d if i.get("model") == "sentry.team"}
        snoozed = {(i["fields"].get("rule"), i["fields"].get("alert_rule")) for i in d
                   if i.get("model") == "sentry.rulesnooze" and i["fields"].get("user_id") is None
                   and i["fields"].get("until") is None}
        # issue alerts
        for i in d:
            if i.get("model") != "sentry.rule" or i["fields"]["project"] not in proj:
                continue
            f = i["fields"]
            blob = json.loads(f["data"]) if isinstance(f["data"], str) else f["data"]
            acts = blob.get("actions") or []
            slug, name = proj[f["project"]], f["label"]
            flags = []
            if f.get("status") not in (0, None):
                flags.append("DISABLED")
            if (i["pk"], None) in snoozed:
                flags.append("MUTED")
            if name in DEFAULT_LABELS:
                flags.append("SENTRY-DEFAULT" if is_default_action(acts) else "CUSTOMIZED-DEFAULT")
            pid = proj_id.get(slug)
            if pid is None:
                status, ids = "NO-PROJECT", []
            else:
                same = [w for w in wf_by_det.get(stream.get(pid), []) if (w.get("name") or "").strip().lower() == name.strip().lower()]
                status, ids = ("PRESENT" if same else "MISSING"), [str(w["id"]) for w in same]
            rows.append({"source": src, "type": "issue", "pk": i["pk"], "project": slug, "name": name,
                         "status": status, "copies": len(ids), "flags": ",".join(flags),
                         "actions": action_summary(acts, users, teams), "dest_ids": " ".join(ids)})
        # metric alerts
        arp = defaultdict(list)
        for i in d:
            if i.get("model") == "sentry.alertruleprojects":
                arp[i["fields"]["alert_rule"]].append(i["fields"]["project"])
        for i in d:
            if i.get("model") != "sentry.alertrule" or i["fields"].get("organization") not in keep_org:
                continue
            f = i["fields"]
            if f.get("status") == 4:      # snapshot rows (archival copies)
                continue
            flags = []
            if f.get("status") not in (0, None):
                flags.append("DISABLED")
            if (None, i["pk"]) in snoozed:
                flags.append("MUTED")
            for p in arp.get(i["pk"], []):
                slug = proj.get(p)
                if not slug:
                    continue
                pid = proj_id.get(slug)
                if pid is None:
                    status, ids = "NO-PROJECT", []
                else:
                    same = [m for m in met_by_proj.get(pid, []) if (m.get("name") or "").strip().lower() == f["name"].strip().lower()]
                    status, ids = ("PRESENT" if same else "MISSING"), [str(m["id"]) for m in same]
                rows.append({"source": src, "type": "metric", "pk": i["pk"], "project": slug, "name": f["name"],
                             "status": status, "copies": len(ids), "flags": ",".join(flags), "actions": "",
                             "dest_ids": " ".join(ids)})

    out = os.path.join(HERE, "alert_audit_" + os.path.basename(args.snapshot).replace("org_snapshot_", "").replace(".json", ".csv"))
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} source alerts -> {out}")


if __name__ == "__main__":
    main()
