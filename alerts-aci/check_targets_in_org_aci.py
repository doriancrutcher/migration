#!/usr/bin/env python3
"""
check_targets_in_org_aci.py — READ-ONLY. For every target in a --targets file, is it already in the org?

Issue targets  : looks at every workflow attached to the project's issue-stream detector.
                 PRESENT-SAME    same name, same config fingerprint
                 PRESENT-DIFF    same name, config differs (name collision)
                 RENAMED-COPY    different name, identical config (someone renamed it)
                 SIMILAR         different name, same triggers/filters/env but different actions
                                 (e.g. rebuilt by hand with a PagerDuty action) -> probably a duplicate
                 ELSEWHERE       same name only on other project(s)
                 MISSING         none of the above  -> the run should create it
Metric targets : metric_issue detectors on the project with the same name (+ linked workflows);
                 SIMILAR = a differently-named monitor on the project with the same aggregate + query.

Uses the translated payloads from a dry run for the config fingerprint (defaults to newest dry run
for the org). Writes check_targets_<org>_<ts>.json and .md (with links) next to this script.
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aci_alert_migrator import AciAlertRuleMigrator  # noqa: E402

BASE = "https://sentry.io/api/0"
HERE = os.path.dirname(os.path.abspath(__file__))


def _norm_q(q):
    toks = (q or "").replace("is_transaction:True", "is_transaction:true").split()
    return " ".join(sorted(t for t in toks if t != "is_transaction:true"))


def loose(fp_json):
    """Fingerprint without actions and frequency: same WHEN / IF conditions + environment."""
    fp = json.loads(fp_json)
    return json.dumps({"env": fp.get("env"), "when": fp.get("when"),
                       "if": sorted(json.dumps(json.loads(g)[:2]) for g in fp.get("if") or [])},
                      sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("auth_token")
    ap.add_argument("org_slug")
    ap.add_argument("--targets", required=True)
    ap.add_argument("--dry-run-results", default=None)
    ap.add_argument("--base-url", default=BASE)
    args = ap.parse_args()

    dr = args.dry_run_results
    if not dr:
        c = sorted(glob.glob(os.path.join(HERE, "dry-run-outputs",
                                          f"aci_safe_alert_migration_results_*_{args.org_slug}_*.json")))
        if not c:
            raise SystemExit(f"No dry-run results for {args.org_slug} in dry-run-outputs/")
        dr = c[-1]
    payloads = json.load(open(dr)).get("translated_payloads") or []
    issue_fp = {(p["project"], p["workflow"]["name"]): AciAlertRuleMigrator.fingerprint(p["workflow"])
                for p in payloads if p["kind"] == "issue"}
    metric_q = {}
    for p in payloads:
        if p["kind"] == "metric":
            ds = ((p["detectors"] or [{}])[0].get("detector") or {}).get("dataSources") or [{}]
            metric_q[p.get("source_pk")] = (_norm_q(ds[0].get("aggregate")), _norm_q(ds[0].get("query")))
    issue_loose = {(p["project"], p["workflow"]["name"]): loose(AciAlertRuleMigrator.fingerprint(p["workflow"]))
                   for p in payloads if p["kind"] == "issue"}
    targets = json.load(open(args.targets))["targets"]

    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {args.auth_token}"})

    def get_all(path):
        out, url = [], f"{args.base_url}{path}"
        while url:
            r = s.get(url, timeout=120)
            if r.status_code != 200:
                raise SystemExit(f"GET {url} -> {r.status_code}: {r.text[:300]}")
            out.extend(r.json())
            url = None
            for part in r.headers.get("Link", "").split(","):
                if 'rel="next"' in part and 'results="true"' in part:
                    url = part.split(";")[0].strip().strip("<>")
        return out

    print(f"Reading {args.org_slug} (read-only) ...")
    projs = get_all(f"/organizations/{args.org_slug}/projects/?per_page=100")
    slug_to_id = {p["slug"]: str(p["id"]) for p in projs}
    id_to_slug = {v: k for k, v in slug_to_id.items()}
    streams = get_all(f"/organizations/{args.org_slug}/detectors/?project=-1&query=type:issue_stream&per_page=100")
    stream_of = {id_to_slug.get(str(d["projectId"])): str(d["id"]) for d in streams if d.get("type") == "issue_stream"}
    metrics = [d for d in get_all(f"/organizations/{args.org_slug}/detectors/?project=-1&query=type:metric_issue&per_page=100")
               if d.get("type") == "metric_issue"]
    wfs = get_all(f"/organizations/{args.org_slug}/workflows/?project=-1&per_page=100")
    print(f"  projects {len(projs)} | issue-stream detectors {len(stream_of)} | metric monitors {len(metrics)} | workflows {len(wfs)}")

    on_det = defaultdict(list)
    by_name = defaultdict(list)
    for w in wfs:
        for d in w.get("detectorIds") or []:
            on_det[str(d)].append(w)
        by_name[(w.get("name") or "").strip().lower()].append(w)

    link = f"https://{args.org_slug}.sentry.io/monitors"
    rows = []
    for t in targets:
        name, slug, kind = t["name"], t["project_slug"], t["type"]
        row = {"pk": t["pk"], "type": kind, "name": name, "project": slug, "status": None, "ids": [], "note": ""}
        if slug not in slug_to_id:
            row["status"], row["note"] = "NO-PROJECT", "project not in org"
            rows.append(row)
            continue
        if kind == "issue":
            det = stream_of.get(slug)
            here = on_det.get(det, [])
            want = issue_fp.get((slug, name))
            same_name = [w for w in here if (w.get("name") or "").strip().lower() == name.strip().lower()]
            if same_name:
                fps = [AciAlertRuleMigrator.fingerprint(w) for w in same_name]
                row["ids"] = [str(w["id"]) for w in same_name]
                if want is None:
                    row["status"], row["note"] = "PRESENT-?", "no dry-run payload to compare"
                elif want in fps:
                    row["status"] = "PRESENT-SAME"
                else:
                    row["status"] = "PRESENT-DIFF"
                    if issue_loose.get((slug, name)) in [loose(f) for f in fps]:
                        row["note"] = "only the actions/frequency differ"
                if len(same_name) > 1:
                    row["note"] = (row["note"] + f" | {len(same_name)} copies on this project").strip(" |")
            else:
                copies = [w for w in here if want and AciAlertRuleMigrator.fingerprint(w) == want]
                elsewhere = [w for w in by_name.get(name.strip().lower(), []) if w not in here]
                if copies:
                    row["status"], row["ids"] = "RENAMED-COPY", [str(w["id"]) for w in copies]
                    row["note"] = "as " + ", ".join(repr(w.get("name")) for w in copies)
                elif issue_loose.get((slug, name)) and [w for w in here if loose(AciAlertRuleMigrator.fingerprint(w)) == issue_loose[(slug, name)]]:
                    sim = [w for w in here if loose(AciAlertRuleMigrator.fingerprint(w)) == issue_loose[(slug, name)]]
                    row["status"], row["ids"] = "SIMILAR", [str(w["id"]) for w in sim]
                    row["note"] = "same conditions, different actions: " + ", ".join(repr(w.get("name")) for w in sim)
                elif elsewhere:
                    row["status"], row["ids"] = "ELSEWHERE", [str(w["id"]) for w in elsewhere]
                    row["note"] = f"same name on {len(elsewhere)} workflow(s) not attached to {slug}"
                else:
                    row["status"] = "MISSING"
                row["note"] = (row["note"] + f" | {len(here)} alerts on project").strip(" |")
        else:
            pid = slug_to_id[slug]
            hits = [d for d in metrics if str(d.get("projectId")) == pid
                    and (d.get("name") or "").strip().lower() == name.strip().lower()]
            if hits:
                row["status"] = "PRESENT"
                row["ids"] = [str(d["id"]) for d in hits]
                q = ((hits[0].get("dataSources") or [{}])[0].get("queryObj") or {}).get("snubaQuery") or {}
                row["note"] = f"{q.get('aggregate')} | {q.get('query') or '-'} | env {q.get('environment')}"
                if len(hits) > 1:
                    row["note"] += f" | {len(hits)} copies"
            else:
                elsewhere = [d for d in metrics if (d.get("name") or "").strip().lower() == name.strip().lower()]
                def dq(d):
                    q = ((d.get("dataSources") or [{}])[0].get("queryObj") or {}).get("snubaQuery") or {}
                    return (_norm_q(q.get("aggregate")), _norm_q(q.get("query")))
                sim = [d for d in metrics if str(d.get("projectId")) == pid and metric_q.get(t["pk"])
                       and dq(d) == metric_q[t["pk"]]]
                if sim and not elsewhere:
                    row["status"], row["ids"] = "SIMILAR", [str(d["id"]) for d in sim]
                    row["note"] = "same aggregate+query on this project: " + ", ".join(repr(d.get("name")) for d in sim)
                    rows.append(row)
                    continue
                row["status"] = "ELSEWHERE" if elsewhere else "MISSING"
                if elsewhere:
                    row["ids"] = [str(d["id"]) for d in elsewhere]
                    row["note"] = "same name on " + ", ".join(sorted({id_to_slug.get(str(d.get('projectId')), '?') for d in elsewhere}))
        rows.append(row)

    order = ["MISSING", "SIMILAR", "ELSEWHERE", "RENAMED-COPY", "PRESENT-DIFF", "PRESENT-?", "PRESENT-SAME", "PRESENT", "NO-PROJECT"]
    rows.sort(key=lambda r: (order.index(r["status"]), r["type"], r["project"], r["name"]))
    tally = defaultdict(int)
    for r in rows:
        tally[r["status"]] += 1

    def links(r):
        if r["type"] == "issue":
            return " ".join(f"[{i}]({link}/alerts/{i}/)" for i in r["ids"])
        return " ".join(f"[{i}]({link}/{i}/)" for i in r["ids"])

    print()
    for r in rows:
        print(f"  {r['status']:<13} {r['type']:<6} pk{r['pk']:<5} {r['project']:<28} {r['name'][:45]:<45} {','.join(r['ids'])[:40]}  {r['note']}")
    print("\n" + " | ".join(f"{k}: {tally[k]}" for k in order if tally[k]))

    ts = f"{datetime.now():%Y%m%d_%H%M%S}"
    jp = os.path.join(HERE, f"check_targets_{args.org_slug}_{ts}.json")
    json.dump({"org": args.org_slug, "dry_run_results": dr, "tally": tally, "rows": rows}, open(jp, "w"), indent=2)
    md = [f"# Safe-set presence check — {args.org_slug} ({ts})", "",
          " · ".join(f"**{k}** {tally[k]}" for k in order if tally[k]), "",
          "| Status | Type | pk | Project | Name | Existing | Note |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['status']} | {r['type']} | {r['pk']} | {r['project']} | {r['name'].replace('|', '/')} | {links(r) or '—'} | {r['note']} |")
    mp = jp[:-5] + ".md"
    open(mp, "w").write("\n".join(md) + "\n")
    print(f"\nJSON: {jp}\nMD  : {mp}")


if __name__ == "__main__":
    main()
