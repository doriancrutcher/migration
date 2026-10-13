#!/usr/bin/env python3
"""
delete_verified_aci.py — delete exactly the alerts a LIVE migrate_safe_alerts_aci.py run created,
AFTER verify_safe_alerts_aci.py passed for that run. TEST ORG ONLY. Dry run by default.

Why: frees a test org's plan quotas (metric monitors, workflows) so further targets can be
tested once the ones already proven have been checked.

It deletes ONLY ids listed under "created" in the results file (monitors first, then workflows).
It never touches a workflow the run did not create. The org must be passed with --allow-org.

    python3 delete_verified_aci.py "$SAAS_TOKEN" "$TEST_ORG" --allow-org "$TEST_ORG" \
        --results "<live results json>" --verification "<aci_safe_alert_verification_*.json>"
    # then add --run_on_real_data=true
"""
import argparse
import json
import os
import sys
from datetime import datetime

import requests

BASE = "https://sentry.io/api/0"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("auth_token")
    ap.add_argument("org_slug")
    ap.add_argument("--results", required=True, help="LIVE aci_safe_alert_migration_results_*.json")
    ap.add_argument("--verification", required=True, help="aci_safe_alert_verification_*.json (must be a PASS)")
    ap.add_argument("--allow-org", action="append", default=[], required=True,
                    help="Test org(s) this script may delete from (repeatable)")
    ap.add_argument("--run_on_real_data", default="false", metavar="true|false")
    ap.add_argument("--base-url", default=BASE)
    args = ap.parse_args()
    live = str(args.run_on_real_data).lower() in ("true", "1", "yes")

    if args.org_slug not in args.allow_org:
        raise SystemExit(f"REFUSED: '{args.org_slug}' is not an allowed TEST org.")
    res = json.load(open(args.results))
    ver = json.load(open(args.verification))
    if res.get("dry_run") or res.get("dest_org") != args.org_slug:
        raise SystemExit("ERROR: --results must be a LIVE run against this org.")
    if (os.path.abspath(ver.get("results_file", "")) != os.path.abspath(args.results)
            or ver.get("problems") or ver.get("verified") != ver.get("total")):
        raise SystemExit("ERROR: --verification is not a clean PASS for this results file — refusing.")

    wfs, dets = [], []
    for c in res.get("created") or []:
        wfs.append((str(c["workflow_id"]), c["name"], c.get("project") or ",".join(c.get("projects") or [])))
        for d in c.get("detector_ids") or []:
            dets.append((str(d), c["name"], ",".join(c.get("projects") or [])))

    print(f"{'DELETING' if live else 'PLAN (dry run)'}: {len(dets)} metric monitors + {len(wfs)} workflows "
          f"created by {os.path.basename(args.results)}")
    for kind, items in (("detectors", dets), ("workflows", wfs)):
        for i, name, proj in items:
            print(f"  {kind[:-1]} {i:>10}  {proj:<28} {name}")

    out = {"org": args.org_slug, "live": live, "results_file": os.path.abspath(args.results),
           "detectors": dets, "workflows": wfs, "deleted": [], "failed": []}
    if live:
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {args.auth_token}"})
        for kind, items in (("detectors", dets), ("workflows", wfs)):
            for i, name, proj in items:
                r = s.delete(f"{args.base_url}/organizations/{args.org_slug}/{kind}/{i}/", timeout=120)
                (out["deleted"] if r.status_code in (200, 202, 204, 404) else out["failed"]).append(
                    {"kind": kind, "id": i, "name": name, "status": r.status_code})
        print(f"\nDeleted {len(out['deleted'])}, failed {len(out['failed'])}")
        for f in out["failed"]:
            print(f"  FAILED {f}")

    path = os.path.join(os.path.dirname(os.path.abspath(args.results)),
                        f"delete_verified_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}.json")
    json.dump(out, open(path, "w"), indent=2)
    print(f"\nRecord: {path}")
    if not live:
        print("DRY RUN — nothing deleted. Re-run with --run_on_real_data=true.")
    return 1 if out["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
