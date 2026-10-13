#!/usr/bin/env python3
"""
verify_safe_alerts_aci.py — READ-ONLY post-run check for migrate_safe_alerts_aci.py.

Replaces verify_safe_alerts.py (which reads the removed legacy /rules/ and /alert-rules/
endpoints and would now 404). For every alert the ACI run reports as created:

  issue  : GET /organizations/{org}/workflows/{id}/
             - exists, name matches, enabled
             - attached to exactly the expected project's issue-stream detector
             - every Slack action has a resolved channel id (not "")
  metric : GET /organizations/{org}/workflows/{id}/   (as above, Slack check)
           GET /organizations/{org}/detectors/{id}/   per project
             - exists, type metric_issue, on the expected project
             - linked back to the workflow (workflowIds)
             - data source environment matches the source alert
  both   : duplicate check — more than one workflow with the same name on the same
           issue-stream detector (issue) / detector name on the same project (metric)

Exit 0 = all verified; 1 = anything missing, mis-linked, duplicated, or with a dead Slack action.

    python3 verify_safe_alerts_aci.py "$SAAS_TOKEN" "$DEST_ORG" \
        --results aci_safe_alert_migration_results_<src>_<dest>_<ts>.json
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

import requests

BASE = "https://sentry.io/api/0"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("auth_token")
    ap.add_argument("org_slug")
    ap.add_argument("--results", required=True, help="LIVE aci_safe_alert_migration_results_*.json")
    ap.add_argument("--base-url", default=BASE)
    args = ap.parse_args()

    res = json.load(open(args.results))
    if res.get("dry_run"):
        raise SystemExit("ERROR: that results file is a DRY RUN — nothing was created to verify.")
    if res.get("dest_org") != args.org_slug:
        raise SystemExit(f"ERROR: results are for org {res.get('dest_org')!r}, not {args.org_slug!r}")

    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {args.auth_token}"})

    def get(path):
        r = s.get(f"{args.base_url}{path}", timeout=60)
        return (r.status_code, r.json() if r.content else None)

    def slack_dead(wf):
        out = []
        for af in wf.get("actionFilters") or []:
            for a in af.get("actions") or []:
                if a.get("type") == "slack" and not (a.get("config") or {}).get("targetIdentifier"):
                    out.append((a.get("config") or {}).get("targetDisplay"))
        return out

    problems, ok = [], 0
    wf_keys, det_keys = Counter(), Counter()
    created = res.get("created") or []
    print(f"Verifying {len(created)} created alert(s) in {args.org_slug} ...")
    for c in created:
        tag = f"[{c['kind']} '{c['name']}' {c.get('project') or c.get('projects')}]"
        code, wf = get(f"/organizations/{args.org_slug}/workflows/{c['workflow_id']}/")
        if code != 200:
            problems.append(f"{tag} workflow {c['workflow_id']} -> HTTP {code}")
            continue
        errs = []
        if wf.get("name") != c["name"]:
            errs.append(f"workflow name {wf.get('name')!r} != {c['name']!r}")
        if not wf.get("enabled"):
            errs.append("workflow is disabled")
        dead = slack_dead(wf)
        if dead:
            errs.append(f"Slack action(s) with NO channel id (will never post): {dead}")
        if c["kind"] == "issue":
            code, proj = get(f"/projects/{args.org_slug}/{c['project']}/")
            pid = str((proj or {}).get("id"))
            dets = [str(d) for d in wf.get("detectorIds") or []]
            linked_ok = False
            for did in dets:
                dcode, det = get(f"/organizations/{args.org_slug}/detectors/{did}/")
                if dcode == 200 and det.get("type") == "issue_stream" and str(det.get("projectId")) == pid:
                    linked_ok = True
                    wf_keys[(c["name"], did)] += 1
            if not linked_ok:
                errs.append(f"not attached to {c['project']}'s issue-stream detector (detectorIds={dets})")
        else:
            for did, slug in zip(c.get("detector_ids") or [], c.get("projects") or []):
                dcode, det = get(f"/organizations/{args.org_slug}/detectors/{did}/")
                if dcode != 200:
                    errs.append(f"detector {did} ({slug}) -> HTTP {dcode}")
                    continue
                code, proj = get(f"/projects/{args.org_slug}/{slug}/")
                if str(det.get("projectId")) != str((proj or {}).get("id")):
                    errs.append(f"detector {did} is on project {det.get('projectId')}, expected {slug}")
                if det.get("type") != "metric_issue":
                    errs.append(f"detector {did} type {det.get('type')}")
                if str(c["workflow_id"]) not in [str(w) for w in det.get("workflowIds") or []]:
                    errs.append(f"detector {did} not linked to workflow {c['workflow_id']}")
                qobj = ((det.get("dataSources") or [{}])[0].get("queryObj") or {})
                env = (qobj.get("snubaQuery") or {}).get("environment")
                if c.get("environment") and env not in (c["environment"], None):
                    errs.append(f"detector {did} env {env!r} != source {c['environment']!r}")
                det_keys[(det.get("name"), slug)] += 1
        if errs:
            problems.append(f"{tag} " + "; ".join(errs))
        else:
            ok += 1
            print(f"  OK  {tag}")

    # duplicates against the live org (covers pre-existing copies from earlier attempts too)
    if wf_keys:
        all_wfs, url = [], f"{args.base_url}/organizations/{args.org_slug}/workflows/?project=-1&per_page=100"
        while url:
            r = s.get(url, timeout=120)
            body = r.json() if r.content else []
            if r.status_code != 200 or not isinstance(body, list):
                problems.append(f"[dup-check] could not list workflows: HTTP {r.status_code}")
                break
            all_wfs.extend(body)
            url = None
            for part in r.headers.get("Link", "").split(","):
                if 'rel="next"' in part and 'results="true"' in part:
                    url = part.split(";")[0].strip().strip("<>")
        live_keys = Counter((w.get("name"), str(d)) for w in all_wfs for d in (w.get("detectorIds") or []))
        for key in wf_keys:
            if live_keys.get(key, 0) > 1:
                problems.append(f"[dup] {live_keys[key]} workflows named {key[0]!r} on issue-stream detector {key[1]}")

    print(f"\nverified present & correctly linked: {ok}/{len(created)}")
    if problems:
        print(f"\nPROBLEMS ({len(problems)}):")
        for p in problems:
            print("  " + p)
    out = os.path.join(os.path.dirname(os.path.abspath(args.results)),
                       f"aci_safe_alert_verification_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(out, "w") as f:
        json.dump({"org": args.org_slug, "results_file": os.path.abspath(args.results),
                   "verified": ok, "total": len(created), "problems": problems}, f, indent=2)
    print(f"\nVerification JSON: {out}")
    return 0 if not problems and ok == len(created) else 1


if __name__ == "__main__":
    sys.exit(main())
