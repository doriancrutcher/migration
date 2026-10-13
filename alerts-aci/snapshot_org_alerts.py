#!/usr/bin/env python3
"""
snapshot_org_alerts.py — READ-ONLY. Saves every alert (workflow) and monitor (detector) in the org,
plus projects, to one JSON file so the full self-hosted-vs-SaaS audit can be done offline
(audit_all_alerts_offline.py).

    python3 snapshot_org_alerts.py "$SAAS_TOKEN" "$DEST_ORG"
"""
import json
import os
import sys
import time
from datetime import datetime

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
B = "https://sentry.io/api/0"


def main():
    if len(sys.argv) != 3 or sys.argv[1] in ("-h", "--help"):
        raise SystemExit(__doc__)
    token, org = sys.argv[1], sys.argv[2]
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})

    def get_all(path):
        out, url = [], f"{B}{path}"
        while url:
            for attempt in range(4):
                try:
                    r = s.get(url, timeout=120)
                    break
                except requests.exceptions.ConnectionError:
                    if attempt == 3:
                        raise
                    time.sleep(2 * (attempt + 1))
            if r.status_code != 200:
                raise SystemExit(f"GET {url} -> {r.status_code}: {r.text[:300]}")
            out.extend(r.json())
            url = None
            for part in r.headers.get("Link", "").split(","):
                if 'rel="next"' in part and 'results="true"' in part:
                    url = part.split(";")[0].strip().strip("<>")
        return out

    print(f"Reading {org} (read-only) ...")
    projects = [{"id": p["id"], "slug": p["slug"]} for p in get_all(f"/organizations/{org}/projects/?per_page=100")]
    print(f"  projects: {len(projects)}")
    detectors = get_all(f"/organizations/{org}/detectors/?project=-1&per_page=100")
    print(f"  monitors: {len(detectors)}")
    workflows = get_all(f"/organizations/{org}/workflows/?project=-1&per_page=100")
    print(f"  alerts:   {len(workflows)}")
    out = os.path.join(HERE, f"org_snapshot_{org}_{datetime.now():%Y%m%d_%H%M%S}.json")
    json.dump({"org": org, "read_at": datetime.now().isoformat(timespec="seconds"),
               "projects": projects, "detectors": detectors, "workflows": workflows}, open(out, "w"))
    print(f"\nJSON: {out}")


if __name__ == "__main__":
    main()
