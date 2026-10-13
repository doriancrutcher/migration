#!/usr/bin/env python3
"""
migrate_safe_alerts_aci.py — migrate a PINNED list of self-hosted alerts (issue + metric) to a SaaS
org through the Workflows / Detectors API ("ACI").

WHY THIS EXISTS
---------------
Sentry removed the legacy alert endpoints (/projects/{org}/{proj}/rules/ and
/organizations/{org}/alert-rules/) on 2026-09-24 (getsentry/sentry #121879); they now return 404.

  * The core AlertRuleMigrator (../core/migrate_alert_rules.py) is still used UNCHANGED for every
    decision (scoping, owner / team / user remapping, Slack rebind, action porting,
    transaction->span, strict skipping).
  * It is wrapped by AciAlertRuleMigrator (aci_alert_migrator.py), which translates the core's
    finished legacy payload into:
        issue alert  -> POST /organizations/{org}/workflows/  (attached to the project's
                        issue-stream detector)
        metric alert -> POST /organizations/{org}/workflows/  +
                        POST /organizations/{org}/projects/{proj}/detectors/  (metric_issue)
  * Dedup happens client-side (the new API has no duplicate-rule check).
  * Metric alerts keep their environment (the legacy payload dropped it).
  * --offline: no network calls at all; destination ids become placeholders. For reviewing the
    translated payloads without a token. A normal dry run (no --offline) does read-only GETs.
  * Every translated payload is written into the results JSON so you can see exactly what
    would be / was sent.

HOW IT WORKS
------------
Scoping is by the exact (type, pk) IDENTITY of each target, never by alert NAME. Many alerts
share generic labels, so name-matching fans out across hundreds of projects. Instead we build a
REDUCED copy of the export containing only the target sentry.rule / sentry.alertrule rows — while
keeping every SUPPORTING row (orgs, projects, teams, environments, snuba queries + event types,
alertruleprojects, alertruletrigger, alertruletriggeraction, organizationmember) — then hand that
to the UNCHANGED core. Because the non-target rules are gone, the core can only see the targets.

TARGETS FILE (--targets)
------------------------
    {"generated_utc": "...", "targets": [
        {"type": "issue",  "pk": 123, "project_slug": "my-project", "name": "My alert"},
        {"type": "metric", "pk": 45,  "project_slug": "my-project", "name": "Error rate"}]}
`type` + `pk` are required (pk = the row pk in the export); the rest is for humans.

TRANSFORMS THIS SCRIPT ADDS (all recorded in the results JSON)
---------------------------------------------------------------
  --scale-count-thresholds (ON by default)
      The transaction->span translation forces metric alerts onto the EAP dataset, whose allowed
      windows start at 300s. A 60s window snaps to 300s, which makes a COUNT threshold ~5x more
      sensitive. We scale the threshold by the same ratio (count() > 100 @ 60s -> > 500 @ 300s).
      Ratio / percentile aggregates (failure_rate(), pXX(), avg()) are NOT scaled.
  --pagerduty-as-email   PagerDuty actions -> email to the owner team (PD services don't carry
                         over); every swap is listed under pagerduty_swapped for follow-up.
  --drop-member ID       drop email actions to a source user (e.g. a pending SaaS invite).
  --exclude issue:PK     leave one target out.  --only-remaining RESULTS  retry held-back/failed.

GUARDS
------
  * refuses a team/user map that contains 'dryrun' ids (they produce "Could not parse actor")
  * refuses targets that are disabled / snoozed at the source (they would be created ENABLED)
  * preflight: workflow quota, Slack feature, member lookups — blocks a live run
  * --protect-org SLUG: orgs where test-only flags (--test-slack-as-email) are refused

OUTPUTS
-------
  <out-dir>/logs/migrate_safe_alerts_aci_<ts>.log                      full console tee
  <out-dir>/aci_safe_alert_migration_results_<src>_<dest>_<ts>.json   live results
  <out-dir>/dry-run-outputs/...                                        dry-run results
Dry-run by default. Pass --run_on_real_data=true to write.

Usage (run from this folder; needs `requests`)
-----
    export SAAS_TOKEN=...   # scopes: org:read project:read project:write alerts:write member:read
                            # (+ team:admin to assign owner teams the token's user isn't in)

    # 0) OFFLINE translation preview (no token needed — pass any string)
    python3 migrate_safe_alerts_aci.py x "$DEST_ORG" --offline --export "$EXPORT" \
        --source-org "$SRC_ORG" --targets targets.json --team-map team_map.json --user-map user_map.json

    # 1) dry run (read-only GETs resolve real project / detector / user ids)
    python3 migrate_safe_alerts_aci.py "$SAAS_TOKEN" "$DEST_ORG" --export "$EXPORT" \
        --source-org "$SRC_ORG" --targets targets.json --team-map team_map.json --user-map user_map.json \
        --slack-integration-id "$SLACK_INTEGRATION_ID"

    # 2) apply: same command + --run_on_real_data=true, then verify_safe_alerts_aci.py
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATION = os.path.normpath(os.path.join(HERE, ".."))
CORE = os.path.join(MIGRATION, "core")
COMMON = os.path.join(MIGRATION, "common")
for p in (CORE, COMMON):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from migrate_alert_rules import AlertRuleMigrator  # noqa: F401  unchanged core logic
except Exception as e:  # noqa: BLE001
    print(f"ERROR: could not import core migrate_alert_rules from {CORE}: {e}", file=sys.stderr)
    raise SystemExit(2)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from aci_alert_migrator import AciAlertRuleMigrator, build_metric_context  # noqa: E402
try:
    from run_logging import start_run_log
except Exception:  # noqa: BLE001
    def start_run_log(name, log_dir=None):
        return None

ISSUE_MODEL = "sentry.rule"
METRIC_MODEL = "sentry.alertrule"

# Mirrors the core's constants — kept local so this file never mutates core behaviour.
TRANSACTION_DATASETS = ("transactions", "generic_metrics")
_EAP_TW_SECONDS = [300, 600, 900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400]
# Aggregates whose value scales with window width. ONLY these get threshold scaling.
_COUNT_AGG = re.compile(r"^count\b", re.IGNORECASE)


def _tf(v):
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _snap(value, allowed):
    return min(allowed, key=lambda a: (abs(a - value), a))


def load_targets(path):
    with open(path) as f:
        data = json.load(f)
    issue_pks, metric_pks = set(), set()
    for t in data.get("targets", []):
        if t.get("type") == "issue":
            issue_pks.add(t["pk"])
        elif t.get("type") == "metric":
            metric_pks.add(t["pk"])
    return issue_pks, metric_pks, data


def build_scoped_export(export_path, issue_pks, metric_pks):
    """Keep every non-target row; keep target rows only when their pk is pinned.
    This is what prevents any fan-out."""
    with open(export_path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"ERROR: export {export_path} is not a Django dumpdata list.")
    reduced, kept_issue, kept_metric = [], set(), set()
    for item in data:
        if not isinstance(item, dict):
            reduced.append(item)
            continue
        model = item.get("model")
        if model == ISSUE_MODEL:
            if item.get("pk") in issue_pks:
                reduced.append(item)
                kept_issue.add(item["pk"])
        elif model == METRIC_MODEL:
            if item.get("pk") in metric_pks:
                reduced.append(item)
                kept_metric.add(item["pk"])
        else:
            reduced.append(item)  # supporting row — always keep
    stats = {
        "kept_issue": sorted(kept_issue), "kept_metric": sorted(kept_metric),
        "missing_issue": sorted(issue_pks - kept_issue),
        "missing_metric": sorted(metric_pks - kept_metric),
        "total_rows_in": len(data), "total_rows_out": len(reduced),
    }
    return reduced, stats


def scale_count_thresholds(reduced, metric_pks):
    """Preserve the firing RATE of count-based metric alerts whose window widens when
    they move to the span (EAP) dataset.

    Only applies when ALL of:
      * the alert is one of our pinned metric targets
      * its snuba query is on a transaction-like dataset (so it WILL be translated)
      * its aggregate is count-based (volume-sensitive)
      * the EAP window snap actually widens the window

    Returns a list of adjustment records (also written to the results JSON).
    """
    ar = {i["pk"]: i for i in reduced
          if isinstance(i, dict) and i.get("model") == METRIC_MODEL}
    sq = {i["pk"]: (i.get("fields") or {}) for i in reduced
          if isinstance(i, dict) and i.get("model") == "sentry.snubaquery"}
    triggers = [i for i in reduced
                if isinstance(i, dict) and i.get("model") == "sentry.alertruletrigger"]

    adjustments = []
    for pk in sorted(metric_pks):
        item = ar.get(pk)
        if not item:
            continue
        f = item.get("fields") or {}
        snuba = sq.get(f.get("snuba_query")) or {}
        dataset = snuba.get("dataset")
        aggregate = (snuba.get("aggregate") or "").strip()
        tw_src = snuba.get("time_window")

        if dataset not in TRANSACTION_DATASETS:
            continue                       # not translated -> window untouched
        if not _COUNT_AGG.match(aggregate):
            continue                       # ratio/percentile -> scale-invariant, leave alone
        if not isinstance(tw_src, int) or tw_src <= 0:
            continue
        tw_new = _snap(tw_src, _EAP_TW_SECONDS)
        if tw_new == tw_src:
            continue                       # no widening -> nothing to correct

        ratio = tw_new / tw_src
        for t in triggers:
            tf = t.get("fields") or {}
            if tf.get("alert_rule") != pk:
                continue
            old = tf.get("alert_threshold")
            if not isinstance(old, (int, float)) or old == 0:
                continue                   # 0 / null thresholds carry no rate to preserve
            new = old * ratio
            tf["alert_threshold"] = new
            adjustments.append({
                "alert_rule_pk": pk, "alert_name": f.get("name"),
                "trigger_pk": t.get("pk"), "trigger_label": tf.get("label"),
                "aggregate": aggregate, "dataset": dataset,
                "time_window_src_s": tw_src, "time_window_sent_s": tw_new,
                "ratio": ratio, "threshold_before": old, "threshold_after": new,
                "reason": "count-based aggregate; window widened by EAP snap — "
                          "threshold scaled to preserve the original rate",
            })
    return adjustments


def pagerduty_as_email(reduced, issue_pks, metric_pks):
    """--pagerduty-as-email: in the SCOPED export only, replace each PagerDuty action on a pinned
    target with an email to the alert's owner team (IssueOwners/ActiveMembers for an issue alert
    with no owner team; the owning user for a user-owned metric alert). Everything else about the
    alert is untouched and the core still does all remapping. PagerDuty services don't carry
    over, so every swap is recorded with the source PD service it replaced, for whoever re-adds PD.

    Returns (records, errors). A target that has no owner to fall back to is an error (not guessed).
    """
    # PD service id -> display name, from every PD metric action in the export (issue actions carry
    # only the id; the self-hosted PD service table itself is not in the export).
    svc_name = {}
    for i in reduced:
        if isinstance(i, dict) and i.get("model") == "sentry.alertruletriggeraction":
            f = i.get("fields") or {}
            if f.get("type") == 1 and f.get("target_display"):
                svc_name.setdefault(str(f.get("target_identifier")), f["target_display"])

    projects = {i["pk"]: (i.get("fields") or {}).get("slug") for i in reduced
                if isinstance(i, dict) and i.get("model") == "sentry.project"}
    teams = {i["pk"]: (i.get("fields") or {}).get("slug") for i in reduced
             if isinstance(i, dict) and i.get("model") == "sentry.team"}
    records, errors = [], []

    for i in reduced:
        if not (isinstance(i, dict) and i.get("model") == ISSUE_MODEL and i.get("pk") in issue_pks):
            continue
        f = i["fields"]
        raw = f.get("data")
        blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
        pd = [a for a in blob.get("actions") or [] if "pagerduty" in (a.get("id") or "").lower()]
        if not pd:
            continue
        team = f.get("owner_team")
        if team is not None:
            fb = {"id": "sentry.mail.actions.NotifyEmailAction", "targetType": "Team",
                  "targetIdentifier": team, "fallthroughType": "ActiveMembers"}
            fb_label = f"email team {teams.get(team, team)}"
        else:
            fb = {"id": "sentry.mail.actions.NotifyEmailAction", "targetType": "IssueOwners",
                  "targetIdentifier": "", "fallthroughType": "ActiveMembers"}
            fb_label = "email issue owners (fallthrough: active members)"
        kept = [a for a in blob["actions"] if a not in pd]
        same = lambda a: (a.get("id", "").endswith("NotifyEmailAction")
                          and a.get("targetType") == fb["targetType"]
                          and str(a.get("targetIdentifier") or "") == str(fb["targetIdentifier"] or ""))
        already = any(same(a) for a in kept)
        blob["actions"] = kept + ([] if already else [fb])
        f["data"] = json.dumps(blob) if isinstance(raw, str) else blob
        for a in pd:
            records.append({"type": "issue", "pk": i["pk"], "name": f.get("label"),
                            "project": projects.get(f.get("project")),
                            "pd_account": a.get("account"), "pd_service": a.get("service"),
                            "pd_service_name": svc_name.get(str(a.get("service"))),
                            "pd_severity": a.get("severity"),
                            "fallback": fb_label + (" (already on the alert — not duplicated)" if already else ""),
                            "other_actions_kept": len(kept)})

    ar = {i["pk"]: i for i in reduced
          if isinstance(i, dict) and i.get("model") == METRIC_MODEL and i.get("pk") in metric_pks}
    trig = {i["pk"]: (i.get("fields") or {}).get("alert_rule") for i in reduced
            if isinstance(i, dict) and i.get("model") == "sentry.alertruletrigger"
            and (i.get("fields") or {}).get("alert_rule") in ar}
    rule_projects = {}
    for i in reduced:
        if isinstance(i, dict) and i.get("model") == "sentry.alertruleprojects":
            f = i.get("fields") or {}
            if f.get("alert_rule") in ar:
                rule_projects.setdefault(f["alert_rule"], []).append(projects.get(f.get("project")))
    by_trig = {}
    for i in reduced:
        if isinstance(i, dict) and i.get("model") == "sentry.alertruletriggeraction":
            t = (i.get("fields") or {}).get("alert_rule_trigger")
            if t in trig:
                by_trig.setdefault(t, []).append(i)
    swap_rows = []
    for t, rows in by_trig.items():
        pk = trig[t]
        rf = ar[pk]["fields"]
        if rf.get("team") is not None:
            tt, ident, label = 2, str(rf["team"]), f"email team {teams.get(rf['team'], rf['team'])}"
        elif rf.get("user_id") is not None:
            tt, ident, label = 1, str(rf["user_id"]), f"email owning user (source user {rf['user_id']})"
        else:
            if any((r.get("fields") or {}).get("type") == 1 for r in rows):
                errors.append(("metric", pk, rf.get("name"), "no owner team/user to fall back to"))
            continue
        already = any((r["fields"].get("type") == 0 and r["fields"].get("target_type") == tt
                       and str(r["fields"].get("target_identifier")) == ident) for r in rows)
        for r in rows:
            f = r["fields"]
            if f.get("type") != 1:
                continue
            records.append({"type": "metric", "pk": pk, "name": rf.get("name"),
                            "project": ",".join(p for p in rule_projects.get(pk, []) if p),
                            "trigger_pk": t, "pd_account": str(f.get("integration_id")),
                            "pd_service": str(f.get("target_identifier")),
                            "pd_service_name": f.get("target_display"),
                            "pd_severity": None,
                            "fallback": label + (" (already on the trigger — not duplicated)" if already else ""),
                            "other_actions_kept": sum(1 for x in rows if x["fields"].get("type") != 1)})
            if already:
                swap_rows.append((r, None))
            else:
                swap_rows.append((r, {"type": 0, "target_type": tt, "target_identifier": ident,
                                      "target_display": None, "integration_id": None,
                                      "sentry_app_config": None}))
                already = True  # a second PD action on the same trigger must not add a 2nd email
    drop = set()
    for r, new in swap_rows:
        if new is None:
            drop.add(id(r))
        else:
            r["fields"].update(new)
    if drop:
        reduced[:] = [i for i in reduced if id(i) not in drop]

    return records, errors


def drop_members(reduced, issue_pks, metric_pks, user_ids):
    """--drop-member: remove email actions that target these V24 USER ids (e.g. someone whose
    SaaS invite is still pending, which Sentry rejects as a notification target). Everything else
    about the alert is kept. An alert left with no action at all is an error, not a silent change."""
    user_ids = {str(u) for u in user_ids}
    records, errors = [], []
    for i in reduced:
        if not (isinstance(i, dict) and i.get("model") == ISSUE_MODEL and i.get("pk") in issue_pks):
            continue
        f = i["fields"]; raw = f.get("data")
        blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
        acts = blob.get("actions") or []
        gone = [a for a in acts if a.get("targetType") == "Member" and str(a.get("targetIdentifier")) in user_ids]
        if not gone:
            continue
        kept = [a for a in acts if a not in gone]
        if not kept:
            errors.append(("issue", i["pk"], f.get("label"), "no action left after dropping member(s)"))
            continue
        blob["actions"] = kept
        f["data"] = json.dumps(blob) if isinstance(raw, str) else blob
        records.append({"type": "issue", "pk": i["pk"], "name": f.get("label"),
                        "dropped_v24_users": [str(a.get("targetIdentifier")) for a in gone],
                        "actions_kept": len(kept)})
    trig = {i["pk"]: (i.get("fields") or {}).get("alert_rule") for i in reduced
            if isinstance(i, dict) and i.get("model") == "sentry.alertruletrigger"
            and (i.get("fields") or {}).get("alert_rule") in metric_pks}
    drop = []
    for i in reduced:
        if isinstance(i, dict) and i.get("model") == "sentry.alertruletriggeraction":
            f = i.get("fields") or {}
            if f.get("alert_rule_trigger") in trig and f.get("type") == 0 and f.get("target_type") == 1 \
                    and str(f.get("target_identifier")) in user_ids:
                drop.append(i)
                records.append({"type": "metric", "pk": trig[f["alert_rule_trigger"]], "name": None,
                                "dropped_v24_users": [str(f.get("target_identifier"))], "actions_kept": None})
    if drop:
        ids = {id(x) for x in drop}
        reduced[:] = [x for x in reduced if id(x) not in ids]
    return records, errors


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("auth_token", help="SaaS token (see runbook for required scopes)")
    ap.add_argument("org_slug", help="Destination SaaS org slug")
    ap.add_argument("--export", required=True,
                    help="Self-hosted relocation export (Django dumpdata JSON)")
    ap.add_argument("--source-org", required=True, help="Source org slug within the export")
    ap.add_argument("--targets", required=True,
                    help="Pinned target list (see TARGETS FILE above)")
    ap.add_argument("--team-map", default=os.path.join(HERE, "team_map.json"),
                    help="CLEAN numeric team map (no dryrun-* ids!)")
    ap.add_argument("--user-map", default=os.path.join(HERE, "user_map.json"),
                    help="CLEAN user map")
    ap.add_argument("--out-dir", default=HERE)
    ap.add_argument("--run_on_real_data", default="false", metavar="true|false",
                    help="'true' to write to SaaS; anything else is a dry run (default: false)")
    ap.add_argument("--slack-integration-id", default=None,
                    help="Destination Slack INTEGRATION id (not the org-integration id). "
                         "If any target carries Slack actions the core REFUSES to run without "
                         "this (or --override_slack_notifications_with_email).")
    ap.add_argument("--rebind-channel", action="store_true",
                    help="Cross-workspace only: resolve the Slack channel by NAME, dropping the "
                         "source channel id. Not needed when the destination Slack integration is "
                         "the same workspace as the source (the source channel ids are valid).")
    ap.add_argument("--override_slack_notifications_with_email", default="false",
                    metavar="true|false",
                    help="Replace every Slack action with the default owner-team email. "
                         "Use only when no Slack integration is available in the destination.")
    ap.add_argument("--scale-count-thresholds", dest="scale", action="store_true", default=True,
                    help="(default ON) scale count-based thresholds when the EAP window widens")
    ap.add_argument("--no-scale-count-thresholds", dest="scale", action="store_false",
                    help="Disable threshold scaling — migrates the raw threshold verbatim")
    ap.add_argument("--keep-scoped-export", action="store_true",
                    help="Keep the reduced export for inspection (default: deleted after run)")
    ap.add_argument("--create-twins", action="store_true",
                    help="When a same-named workflow already exists on the project but its CONFIG "
                         "differs, create this one as '<name> (v24)' instead of skipping it.")
    ap.add_argument("--protect-org", action="append", default=[], metavar="SLUG",
                    help="Production org(s) where test-only flags are refused (repeatable)")
    ap.add_argument("--skip-quota-check", action="store_true",
                    help="Skip the 1000-workflows-per-org preflight (only if more-workflows is enabled)")
    ap.add_argument("--only-email", action="store_true",
                    help="Leave out every target that sends to Slack (use while the org lacks the "
                         "Slack alert feature). Run again without it later for the Slack ones.")
    ap.add_argument("--exclude", action="append", default=[], metavar="issue:PK|metric:PK",
                    help="Leave out one target, e.g. --exclude metric:83 (repeatable)")
    ap.add_argument("--only-remaining", metavar="LIVE_RESULTS_JSON", default=None,
                    help="Only the targets a previous LIVE run held back or failed (e.g. after an "
                         "--only-email run). Everything it created is left out.")
    ap.add_argument("--test-slack-as-email", action="store_true",
                    help="TEST ORGS ONLY: replace every Slack action with an email to the alert's "
                         "owner team, so the rest of each Slack alert can be tested in an org "
                         "without the Slack feature. Refused for --protect-org orgs.")
    ap.add_argument("--pagerduty-as-email", action="store_true",
                    help="Replace each PagerDuty action with an email to the alert's owner team "
                         "(re-add PD by hand later). Every swap is recorded under pagerduty_swapped "
                         "in the results; pagerduty_followup_list.py turns that into a list.")
    ap.add_argument("--drop-member", action="append", default=[], metavar="SOURCE_USER_ID",
                    help="Remove email actions to this source (self-hosted) user id (e.g. a pending SaaS invite) and keep "
                         "the rest of the alert. Recorded under dropped_members in the results. Repeatable.")
    ap.add_argument("--offline", action="store_true",
                    help="No network calls at all (implies dry run). Destination ids become "
                         "placeholders; use to review the translated workflow/detector payloads.")
    args = ap.parse_args()
    if args.offline:
        args.run_on_real_data = "false"

    os.makedirs(args.out_dir, exist_ok=True)
    log_dir = os.path.join(args.out_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    dry_run = not _tf(args.run_on_real_data)

    for label, path in (("targets", args.targets), ("export", args.export),
                        ("team map", args.team_map)):
        if not os.path.exists(path):
            raise SystemExit(f"ERROR: {label} file not found: {path}")

    log_path = start_run_log("migrate_safe_alerts_aci", log_dir=log_dir)

    print("=" * 78)
    print("ALERT MIGRATION (ACI) — pinned targets -> Workflows / Detectors API")
    print("=" * 78)
    mode = ("EXECUTE (WRITES TO SaaS)" if not dry_run else
            "OFFLINE DRY RUN (no network)" if args.offline else "DRY RUN (read-only GETs, no writes)")
    print(f"Mode        : {mode}")
    print(f"Destination : {args.org_slug}")
    print(f"Export      : {os.path.basename(args.export)}  (source-org {args.source_org})")
    if log_path:
        print(f"Log         : {log_path}")

    # --- guard: a dry-run mapping file silently poisons every owner --------------
    for label, path in (("team map", args.team_map), ("user map", args.user_map)):
        if os.path.exists(path) and "dryrun" in open(path).read():
            raise SystemExit(
                f"ERROR: {label} {path} contains 'dryrun' ids. Feeding a dry-run mapping "
                f"file makes every owner fail with 'Could not parse actor'. "
                f"Regenerate it from a LIVE run before continuing.")

    issue_pks, metric_pks, tdata = load_targets(args.targets)
    held_back = []
    if args.test_slack_as_email and args.org_slug in args.protect_org:
        raise SystemExit("REFUSED: --test-slack-as-email is for test orgs only.")
    if args.only_remaining:
        prev = json.load(open(args.only_remaining))
        if prev.get("dry_run"):
            raise SystemExit("ERROR: --only-remaining needs a LIVE results file.")
        want_i = {h["pk"] for h in prev.get("held_back", []) if h["type"] == "issue"}
        want_m = {h["pk"] for h in prev.get("held_back", []) if h["type"] == "metric"}
        want_i |= {f[0] for f in prev["results"]["issue"].get("failed", [])}
        want_m |= {f[0] for f in prev["results"]["metric"].get("failed", [])}
        issue_pks &= want_i
        metric_pks &= want_m
        print(f"--only-remaining: {len(issue_pks)} issue + {len(metric_pks)} metric targets left over "
              f"from {os.path.basename(args.only_remaining)}")
    for ex in args.exclude:
        kind, _, pk = ex.partition(":")
        pool = issue_pks if kind == "issue" else metric_pks if kind == "metric" else None
        if pool is None or not pk.isdigit():
            raise SystemExit(f"ERROR: --exclude {ex!r} must look like issue:123 or metric:83")
        if int(pk) in pool:
            pool.discard(int(pk)); held_back.append((kind, int(pk), "--exclude"))
    if args.only_email:
        with open(args.export) as f:
            _rows = [r for r in json.load(f) if isinstance(r, dict)]
        for r in _rows:
            if r.get("model") == ISSUE_MODEL and r.get("pk") in issue_pks:
                raw = (r.get("fields") or {}).get("data")
                blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
                if any("slack" in (a.get("id") or "").lower() for a in blob.get("actions") or []):
                    issue_pks.discard(r["pk"]); held_back.append(("issue", r["pk"], "slack"))
        slack_trig = {(r.get("fields") or {}).get("alert_rule_trigger") for r in _rows
                      if r.get("model") == "sentry.alertruletriggeraction"
                      and (r.get("fields") or {}).get("type") == 2}
        for r in _rows:
            if (r.get("model") == "sentry.alertruletrigger" and r.get("pk") in slack_trig
                    and (r.get("fields") or {}).get("alert_rule") in metric_pks):
                pk = r["fields"]["alert_rule"]
                metric_pks.discard(pk); held_back.append(("metric", pk, "slack"))
        del _rows
    if held_back:
        print(f"\nHeld back this run: {len(held_back)} target(s) "
              f"({sum(1 for h in held_back if h[2] == 'slack')} Slack, "
              f"{sum(1 for h in held_back if h[2] == '--exclude')} --exclude) — re-run without the "
              f"flag(s) later to migrate them")
    total_targets = len(issue_pks) + len(metric_pks)
    print(f"\nPinned targets: {total_targets}  (issue {len(issue_pks)}, metric {len(metric_pks)})")
    print(f"  from {os.path.basename(args.targets)} generated {tdata.get('generated_utc')}")
    if tdata.get("excluded_tally"):
        print("  deliberately EXCLUDED (later steps): "
              + ", ".join(f"{v} {k}" for k, v in sorted(tdata["excluded_tally"].items())))

    reduced, stats = build_scoped_export(args.export, issue_pks, metric_pks)
    matched = len(stats["kept_issue"]) + len(stats["kept_metric"])
    print(f"\nScoped export: {stats['total_rows_out']} rows (from {stats['total_rows_in']}) — "
          f"only the {matched} target rules remain, so the core cannot fan out.")
    if stats["missing_issue"] or stats["missing_metric"]:
        print(f"WARNING: {len(stats['missing_issue'])} issue + {len(stats['missing_metric'])} "
              f"metric target pk(s) NOT in this export — wrong --export?")
        print(f"  missing issue : {stats['missing_issue']}")
        print(f"  missing metric: {stats['missing_metric']}")
    if matched == 0:
        raise SystemExit("ERROR: 0 targets matched this export — nothing to do.")

    adjustments = []
    if args.scale:
        adjustments = scale_count_thresholds(reduced, metric_pks)
        if adjustments:
            print(f"\nThreshold scaling — {len(adjustments)} trigger(s) adjusted so the EAP "
                  f"window change does not alter the firing rate:")
            for a in adjustments:
                print(f"  pk{a['alert_rule_pk']} '{a['alert_name']}' [{a['trigger_label']}]  "
                      f"{a['aggregate']}  {a['time_window_src_s']}s -> {a['time_window_sent_s']}s  "
                      f"threshold {a['threshold_before']} -> {a['threshold_after']}")
        else:
            print("\nThreshold scaling: no count-based alert needed adjustment.")
    else:
        print("\nThreshold scaling DISABLED (--no-scale-count-thresholds) — count-based alerts "
              "whose window widens will fire more readily than on V24.")

    dropped_members = []
    if args.drop_member:
        dropped_members, dm_err = drop_members(reduced, set(stats["kept_issue"]), set(stats["kept_metric"]),
                                               args.drop_member)
        print(f"\n--drop-member {', '.join(args.drop_member)}: removed from {len(dropped_members)} alert(s):")
        for r in dropped_members:
            print(f"  {r['type']:<6} pk{r['pk']:<5} {r['name'] or ''}  (dropped V24 user(s) {', '.join(r['dropped_v24_users'])})")
        if dm_err:
            raise SystemExit(f"ERROR: {dm_err}")

    pd_swapped = []
    if args.pagerduty_as_email:
        pd_swapped, pd_errors = pagerduty_as_email(reduced, set(stats["kept_issue"]), set(stats["kept_metric"]))
        print(f"\nPagerDuty -> email (--pagerduty-as-email): {len(pd_swapped)} PD action(s) replaced on "
              f"{len({(r['type'], r['pk']) for r in pd_swapped})} alert(s):")
        for r in pd_swapped:
            print(f"  {r['type']:<6} pk{r['pk']:<5} {(r['project'] or '')[:26]:<26} {r['name'][:40]:<40} "
                  f"PD svc {r['pd_service']} ({r['pd_service_name'] or 'name not in export'}) -> {r['fallback']}")
        if pd_errors:
            raise SystemExit(f"ERROR: {len(pd_errors)} PagerDuty alert(s) have no owner to fall back to: "
                             f"{pd_errors}. Exclude them or handle manually.")
        pd_pks = {(r["type"], r["pk"]) for r in pd_swapped}
        no_pd = [("issue", p) for p in stats["kept_issue"] if ("issue", p) not in pd_pks] + \
                [("metric", p) for p in stats["kept_metric"] if ("metric", p) not in pd_pks]
        if no_pd:
            print(f"  note: {len(no_pd)} target(s) had no PagerDuty action (unchanged): {no_pd}")

    if args.slack_integration_id:
        print(f"\nSlack: preserving actions via integration id {args.slack_integration_id}"
              + ("  (--rebind-channel: resolving by channel NAME)" if args.rebind_channel else ""))
    elif _tf(args.override_slack_notifications_with_email):
        print("\nSlack: replacing every Slack action with the default owner-team email "
              "(--override_slack_notifications_with_email)")
    else:
        print("\nNOTE: no --slack-integration-id given. If any target carries Slack actions the "
              "core will REFUSE to run rather than drop them silently.")

    scoped_path = os.path.join(args.out_dir, "_scoped_export_safe_aci.json")
    with open(scoped_path, "w") as f:
        json.dump(reduced, f)

    # Guard: the translator always creates alerts ENABLED. Refuse to silently re-enable a source
    # alert that was disabled / snoozed-forever on V24 (Sentry's own migrator preserves that).
    disabled = []
    snoozed = {(f.get("rule"), f.get("alert_rule")) for f in
               ((i.get("fields") or {}) for i in reduced
                if isinstance(i, dict) and i.get("model") == "sentry.rulesnooze")
               if f.get("user_id") is None and f.get("until") is None}
    for i in reduced:
        if not isinstance(i, dict):
            continue
        f = i.get("fields") or {}
        if i.get("model") == "sentry.rule" and (f.get("status") not in (0, None)
                                                 or (i["pk"], None) in snoozed):
            disabled.append(("issue", i["pk"], f.get("label")))
        if i.get("model") == "sentry.alertrule" and (f.get("status") not in (0, None)
                                                      or (None, i["pk"]) in snoozed):
            disabled.append(("metric", i["pk"], f.get("name")))
    if disabled:
        raise SystemExit(f"ERROR: {len(disabled)} target(s) are disabled/snoozed at the source and "
                         f"would be re-created ENABLED: {disabled}. Remove them from the targets file "
                         f"or handle them manually.")

    # The legacy payload the core builds for a metric alert carries neither the source pk nor the
    # environment; recover both by signature from the same (already threshold-scaled) rows.
    metric_context = build_metric_context(reduced, args.source_org)
    ambiguous = [v for v in metric_context.values() if v.get("ambiguous")]
    if ambiguous:
        print(f"WARNING: {len(ambiguous)} metric signature(s) ambiguous — those alerts will FAIL "
              f"rather than guess: {ambiguous}")

    migrator = AciAlertRuleMigrator(
        args.auth_token, dry_run=dry_run, offline=args.offline, metric_context=metric_context,
        team_map=AlertRuleMigrator("x").load_team_mappings(args.team_map))
    migrator.create_twins = args.create_twins
    migrator.slack_as_email = args.test_slack_as_email
    if args.test_slack_as_email:
        print("\nTEST MODE: every Slack action will be created as an EMAIL to the owner team "
              "(--test-slack-as-email). The Slack action itself is NOT being tested.")

    # --- preflight: org-level blockers (read-only). Fail BEFORE writing anything. ---------------
    if not args.offline:
        _rules = [i for i in reduced if isinstance(i, dict) and i.get("model") == "sentry.rule"]
        _needs_slack = any("slack" in json.dumps(i.get("fields", {}).get("data", "")).lower() for i in _rules)
        _kept_trig = {i["pk"] for i in reduced if isinstance(i, dict)
                      and i.get("model") == "sentry.alertruletrigger"
                      and (i.get("fields") or {}).get("alert_rule") in set(stats["kept_metric"])}
        _needs_members = any(
            (i.get("fields") or {}).get("target_type") == 1
            and (i.get("fields") or {}).get("alert_rule_trigger") in _kept_trig for i in reduced
            if isinstance(i, dict) and i.get("model") == "sentry.alertruletriggeraction")
        blockers = migrator.preflight(args.org_slug, len(stats["kept_issue"]), len(stats["kept_metric"]),
                                      _needs_slack and not args.test_slack_as_email, _needs_members)
        if args.skip_quota_check:
            blockers = [b for b in blockers if "1000" not in b]
        if blockers:
            print("\nPREFLIGHT BLOCKERS — nothing will be written until these are fixed:")
            for b in blockers:
                print("  * " + b)
            if not dry_run:
                raise SystemExit(3)
            print("  (dry run continues so you can still review the payloads)")
    user_map = args.user_map if os.path.exists(args.user_map) else None
    try:
        results = migrator.migrate_alert_rules(
            scoped_path, args.org_slug, args.team_map,
            source_org=args.source_org, migrate_issue=True, only_names=None,
            user_mappings_file=user_map,
            slack_integration_id=args.slack_integration_id,
            override_slack_with_email=_tf(args.override_slack_notifications_with_email),
            rebind_channel=args.rebind_channel,
        )
    finally:
        if not args.keep_scoped_export:
            try:
                os.remove(scoped_path)
            except OSError:
                pass

    # Issue alerts deduped client-side come back through the core's "migrated" list; move them out
    # so "created" counts only real creates.
    i_res = results["issue"]
    i_res["skipped_existing_alert"] = [r for r in i_res["migrated"]
                                       if isinstance(r, dict) and r.get("skipped_existing")
                                       and not r.get("name_collision")]
    i_res["skipped_name_collision"] = [r for r in i_res["migrated"]
                                       if isinstance(r, dict) and r.get("name_collision")]
    i_res["migrated"] = [r for r in i_res["migrated"]
                         if not (isinstance(r, dict) and r.get("skipped_existing"))]

    tag = f"{args.source_org}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
    outdir = os.path.join(args.out_dir, "dry-run-outputs") if dry_run else args.out_dir
    os.makedirs(outdir, exist_ok=True)
    results_path = os.path.join(outdir, f"aci_safe_alert_migration_results_{tag}.json")
    payload = {
        "step": "pinned alert migration via Workflows/Detectors API",
        "api": "workflow_engine (POST /workflows/, POST /projects/{p}/detectors/)",
        "offline": args.offline,
        "dry_run": dry_run,
        "dest_org": args.org_slug,
        "source_org": args.source_org,
        "export": os.path.abspath(args.export),
        "targets_file": os.path.abspath(args.targets),
        "targets_generated_utc": tdata.get("generated_utc"),
        "total_targets": total_targets,
        "matched_targets": matched,
        "scope_stats": stats,
        "slack_integration_id": args.slack_integration_id,
        "rebind_channel": args.rebind_channel,
        "override_slack_with_email": _tf(args.override_slack_notifications_with_email),
        "threshold_scaling_enabled": args.scale,
        "threshold_adjustments": adjustments,
        "held_back": [{"type": k, "pk": p, "why": w} for k, p, w in held_back],
        "log_file": log_path,
        "results": results,
        "created": migrator.created,
        "slack_unresolved": migrator.slack_unresolved,
        "test_slack_as_email": args.test_slack_as_email,
        "slack_swapped_to_email": migrator.slack_swapped,
        "name_collisions": migrator.name_collisions,
        "unresolved_lookups": migrator.unresolved_lookups,
        "dropped_members": dropped_members,
        "pagerduty_as_email": args.pagerduty_as_email,
        "pagerduty_swapped": pd_swapped,
        "translated_payloads": migrator.translated,
    }
    with open(results_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)

    m, i = results["metric"], results["issue"]
    print("\n=== summary ===")
    print(f"Issue  — {'would create' if dry_run else 'created'}: {len(i['migrated'])}, "
          f"failed: {len(i['failed'])}, skipped(identical exists): {len(i['skipped_existing_alert'])}, "
          f"skipped(same name, DIFFERENT config): {len(i['skipped_name_collision'])}, "
          f"skipped(degraded): {len(i.get('skipped_degraded', []))}")
    print(f"Metric — {'would create' if dry_run else 'created'}: {len(m['migrated'])}, "
          f"failed: {len(m['failed'])}, skipped(dedup/exists): {len(m.get('skipped_existing_alert', []))}, "
          f"flagged(manual span rebuild): {len(m.get('flagged_transaction_manual', []))}")
    created = len(i["migrated"]) + len(m["migrated"])
    print(f"TOTAL {'would create' if dry_run else 'created'}: {created} / {total_targets} targets")
    if migrator.slack_unresolved:
        print(f"\nWARNING: {len(migrator.slack_unresolved)} Slack action(s) saved WITHOUT a channel "
              f"id (Sentry's name lookup timed out) — they will never post. Fix in the UI:")
        for s_ in migrator.slack_unresolved:
            print(f"  workflow {s_['workflow_id']} '{s_['name']}' ({s_['where']}) -> #{s_['channel']}")
    if migrator.unresolved_lookups:
        print(f"\nOFFLINE placeholders ({len(migrator.unresolved_lookups)}) — resolved for real on a "
              f"non-offline run: {', '.join(migrator.unresolved_lookups[:8])}"
              + (" ..." if len(migrator.unresolved_lookups) > 8 else ""))
    if i["failed"] or m["failed"]:
        print("\nFAILURES — investigate before re-running:")
        for r in i["failed"] + m["failed"]:
            print(f"  {r}")
    print(f"\nResults JSON: {results_path}")
    if log_path:
        print(f"Run log     : {log_path}")
    if dry_run:
        print("\nDRY RUN — nothing was written. Re-run with --run_on_real_data=true to apply.")
    else:
        print(f"\nNEXT: verify with\n  python3 verify_safe_alerts_aci.py \"$SAAS_TOKEN\" "
              f"{args.org_slug} --results {results_path}")


if __name__ == "__main__":
    main()
