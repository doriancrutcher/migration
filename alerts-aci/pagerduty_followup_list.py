#!/usr/bin/env python3
"""
pagerduty_followup_list.py — OFFLINE. Builds the list someone needs to put PagerDuty back.

Reads a migrate_safe_alerts_aci.py results file from a --pagerduty-as-email run and writes, next to
it, pagerduty_followup_<org>_<ts>.md and .csv: one row per replaced PagerDuty action with

  alert name, type, project, link to the alert (and monitor for metric alerts),
  the V24 PagerDuty service it used to page (id + name where the export has it),
  the fallback email that was added (the one to remove once PD is re-added),
  and whether the alert was created / already existed / failed in this run.

    python3 pagerduty_followup_list.py aci_safe_alert_migration_results_<src>_<dest>_<ts>.json
"""
import csv
import json
import os
import sys
from datetime import datetime


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    r = json.load(open(path))
    swaps = r.get("pagerduty_swapped") or []
    if not r.get("pagerduty_as_email") or not swaps:
        raise SystemExit("That results file is not from a --pagerduty-as-email run (no pagerduty_swapped).")
    org = r["dest_org"]
    base = f"https://{org}.sentry.io/monitors"

    created_issue = {(c["name"], c.get("project")): c for c in r.get("created") or [] if c["kind"] == "issue"}
    created_metric = {c.get("source_pk"): c for c in r.get("created") or [] if c["kind"] == "metric"}
    res = r.get("results") or {}
    failed = {("issue", f[0]): f[1] for f in (res.get("issue") or {}).get("failed", [])}
    failed.update({("metric", f[0]): f[1] for f in (res.get("metric") or {}).get("failed", [])})
    skipped_i = {(x["name"], x.get("project")) for x in (res.get("issue") or {}).get("skipped_existing_alert", [])
                 + (res.get("issue") or {}).get("skipped_name_collision", [])}
    skipped_m = {x["pk"] for x in (res.get("metric") or {}).get("skipped_existing_alert", [])}

    rows = []
    for s in swaps:
        link, monitor, status = "", "", ""
        if s["type"] == "issue":
            c = created_issue.get((s["name"], s["project"]))
            if c:
                link, status = f"{base}/alerts/{c['workflow_id']}/", "created"
            elif (s["name"], s["project"]) in skipped_i:
                status = "NOT created — same-named alert already exists"
            elif ("issue", s["pk"]) in failed:
                status = f"FAILED: {failed[('issue', s['pk'])][:120]}"
        else:
            c = created_metric.get(s["pk"])
            if c:
                link = f"{base}/alerts/{c['workflow_id']}/"
                monitor = " ".join(f"{base}/{d}/" for d in c.get("detector_ids") or [])
                status = "created"
            elif s["pk"] in skipped_m:
                status = "NOT created — same-named monitor already exists"
            elif ("metric", s["pk"]) in failed:
                status = f"FAILED: {failed[('metric', s['pk'])][:120]}"
        if r.get("dry_run"):
            status = "dry run — " + (status or "would create")
        rows.append({
            "alert": s["name"], "type": s["type"], "project": s["project"],
            "alert_link": link, "monitor_link": monitor,
            "v24_pagerduty_service_id": s["pd_service"],
            "v24_pagerduty_service_name": s.get("pd_service_name") or "(not in export — see project)",
            "severity": s.get("pd_severity") or "",
            "fallback_email_to_remove": s["fallback"],
            "status": status, "source_pk": s["pk"],
        })
    rows.sort(key=lambda x: (x["type"], x["project"] or "", x["alert"]))

    ts = f"{datetime.now():%Y%m%d_%H%M%S}"
    out = os.path.join(os.path.dirname(os.path.abspath(path)), f"pagerduty_followup_{org}_{ts}")
    with open(out + ".csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    md = [f"# PagerDuty follow-up — {org}", "",
          f"{len(rows)} alert(s) were migrated with an **email in place of their PagerDuty action**. "
          "For each: add the PagerDuty action for the service below, then remove the fallback email.", "",
          "Service ids are the self-hosted ids — match them to the SaaS service by name.", "",
          "| # | Alert | Type | Project | V24 PD service | Fallback email to remove | Link | Status |",
          "|---|---|---|---|---|---|---|---|"]
    for n, x in enumerate(rows, 1):
        links = " · ".join(l for l in (f"[alert]({x['alert_link']})" if x["alert_link"] else "",
                                       f"[monitor]({x['monitor_link'].split()[0]})" if x["monitor_link"] else "") if l)
        md.append(f"| {n} | {x['alert'].replace('|', '/')} | {x['type']} | {x['project']} | "
                  f"{x['v24_pagerduty_service_id']} — {x['v24_pagerduty_service_name']} | "
                  f"{x['fallback_email_to_remove']} | {links or '—'} | {x['status']} |")
    open(out + ".md", "w").write("\n".join(md) + "\n")
    print(f"{len(rows)} row(s)\nMD : {out}.md\nCSV: {out}.csv")


if __name__ == "__main__":
    main()
