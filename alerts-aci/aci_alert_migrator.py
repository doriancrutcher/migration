#!/usr/bin/env python3
"""
aci_alert_migrator.py — make the UNCHANGED core AlertRuleMigrator write to the new
Workflows / Detectors ("ACI") API instead of the removed legacy alert endpoints.

WHY THIS EXISTS
---------------
Sentry removed the legacy alert routes on 2026-09-24 (getsentry/sentry #121879):
    POST /projects/{org}/{project}/rules/          (issue alerts)   -> 404
    POST /organizations/{org}/alert-rules/         (metric alerts)  -> 404

HOW IT WORKS
------------
The core migrator does all the hard, already-validated work — owner/team/user remapping,
Slack re-binding, action porting, the transaction->span translation, strict-degraded skipping —
and ends every alert by calling ONE of two methods with a legacy-shaped payload:

    create_issue_alert_rule(org, project_slug, legacy_payload)
    create_alert_rule(org, legacy_payload)

This subclass overrides only those two (plus the dedup fetch) and TRANSLATES the finished
legacy payload into the new API shape, mirroring Sentry's own server-side translators so the
result is what Sentry itself would have produced for the same rule:

    src/sentry/workflow_engine/migration_helpers/issue_alert_conditions.py   (rule -> conditions)
    src/sentry/workflow_engine/migration_helpers/issue_alert_migration.py    (rule -> workflow)
    src/sentry/workflow_engine/migration_helpers/alert_rule.py               (alertrule -> detector)
    src/sentry/workflow_engine/typings/notification_action.py                (actions)

ISSUE ALERT  (sentry.rule)  ->  1 Workflow
    POST /organizations/{org}/workflows/
      triggers      = the rule's "when" conditions (first-seen / regression / reappeared /
                      event-frequency ...), logic from action_match ('any' -> 'any-short')
      actionFilters = [ { the rule's "if" filters, logic from filter_match, actions: [...] } ]
      detectorIds   = [ the project's issue-stream detector ]   <- this is what scopes it to
                      the project now; the old URL-path project is gone.
      config        = { frequency: <minutes> }, environment, owner

METRIC ALERT (sentry.alertrule) -> 1 Workflow + 1 Detector per project
    POST /organizations/{org}/workflows/          (the notifications; created FIRST so a
                                                   failure never leaves an un-notifying monitor)
      actionFilters = one per trigger: priority >= HIGH/MEDIUM  OR  de-escalating from it
    POST /organizations/{org}/projects/{project}/detectors/   type=metric_issue
      dataSources    = [ snuba query: dataset/aggregate/query/timeWindow(seconds)/env ]
      conditionGroup = critical -> 75, warning -> 50, resolve -> 0   (gt/lt by threshold_type)
      workflowIds    = [ the workflow above ]

WHAT IS NEW vs THE LEGACY PATH (deliberate, documented)
    * Metric alerts now carry their ENVIRONMENT. The legacy core payload silently omitted it
      (so legacy-migrated metric alerts were env-less). The detector data source takes the env
      name and Sentry get-or-creates it on the project (snuba_query_validator.validate_environment).
      ISSUE workflows are different: `environment` must ALREADY exist in the org
      (EnvironmentField -> 400 "Environment is not part of this organization"), so a non-offline
      run pre-checks it via GET /organizations/{org}/environments/ and fails that alert early.
    * "Every event" is KEPT as a trigger when action_match=any (Sentry's own migrator drops it,
      which turns "every event OR first seen" into just "first seen"). Under action_match=all it
      is a no-op and is dropped.
    * Slack: across workspaces (--rebind-channel) there is no channel id, so Sentry resolves the
      channel by NAME. If that lookup times out, Sentry saves the action with an EMPTY channel id
      and returns 201 — a silent never-posts action. Every live create response is inspected and
      such actions are reported under `slack_unresolved`.
    * Guards: dynamic (anomaly) metric alerts are refused (they'd become a static threshold);
      detector names are truncated to 200 chars the way Sentry does.
    * Issue alerts have NO server-side duplicate check any more, so we dedupe ourselves:
      skip when a workflow with the same name is already attached to that project's
      issue-stream detector. Metric dedup = same-named metric_issue detector on the project.
    * Email-to-USER targets must be a Sentry *user id*; the user map holds SaaS *member* ids.
      We resolve member id -> user id via GET /organizations/{org}/members/. An invited member
      who hasn't accepted has no user id -> that alert fails loudly instead of silently dropping.

OFFLINE MODE
    offline=True performs NO network calls at all (not even GETs). Lookups that need the
    destination org (project ids, issue-stream detector ids, member->user ids) are emitted as
    readable placeholders like "<issue_stream_detector:bvs>" and listed under
    `unresolved_lookups`, so the translated payloads can still be reviewed/validated.
"""
import json
import time
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
_CORE = os.path.normpath(os.path.join(HERE, "..", "core"))
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

from migrate_alert_rules import (  # noqa: E402  (unchanged core)
    AlertRuleMigrator,
    SPAN_DATASET,
    TRANSACTION_DATASETS,
)

logger = logging.getLogger("aci_alert_migrator")

# --- legacy rule-node id -> new condition type -------------------------------------------------
# Trigger-group ("WHEN") conditions. Their handler group is WORKFLOW_TRIGGER, which the API only
# accepts under a triggers group with logicType 'any-short' on create.
_TRIGGER_TYPES = {
    "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition": "first_seen_event",
    "sentry.rules.conditions.regression_event.RegressionEventCondition": "regression_event",
    "sentry.rules.conditions.reappeared_event.ReappearedEventCondition": "reappeared_event",
    "sentry.rules.conditions.high_priority_issue.NewHighPriorityIssueCondition": "new_high_priority_issue",
    "sentry.rules.conditions.high_priority_issue.ExistingHighPriorityIssueCondition": "existing_high_priority_issue",
}
_EVERY_EVENT = "sentry.rules.conditions.every_event.EveryEventCondition"
# Frequency ("slow") conditions. Sentry's own migrator keeps them in the WHEN group.
_FREQ = {
    "sentry.rules.conditions.event_frequency.EventFrequencyCondition":
        ("event_frequency_count", "event_frequency_percent", int),
    "sentry.rules.conditions.event_frequency.EventUniqueUserFrequencyCondition":
        ("event_unique_user_frequency_count", "event_unique_user_frequency_percent", int),
    "sentry.rules.conditions.event_frequency.EventFrequencyPercentCondition":
        ("percent_sessions_count", "percent_sessions_percent", float),
}
_IS_SET = ("is", "ns")  # MatchType.IS_SET / NOT_SET -> no `value` allowed

# Legacy action_match / filter_match -> DataConditionGroup.Type
_LOGIC = {"any": "any-short", "all": "all", "none": "none"}

# Metric-alert trigger label -> DetectorPriorityLevel
_PRIORITY = {"critical": 75, "warning": 50}


class TranslationError(Exception):
    """The legacy rule can't be represented faithfully in the new API — fail it loudly."""


class AciApiError(Exception):
    """An API call failed. str() carries status + response body so results JSON is useful."""


class AciAlertRuleMigrator(AlertRuleMigrator):
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0",
                 dry_run: bool = False, offline: bool = False,
                 metric_context: Optional[Dict[Tuple, Dict[str, Any]]] = None,
                 team_map: Optional[Dict[str, str]] = None):
        super().__init__(auth_token, base_url=base_url, dry_run=dry_run)
        self.offline = offline
        # signature -> {"pk", "environment"} for metric alerts (built by the wrapper from the export;
        # the core's legacy payload doesn't carry the environment or the source pk).
        self.metric_context = metric_context or {}
        # source team pk -> SaaS team id (only needed to remap AssignedToFilter Team targets)
        self.team_map = team_map or {}
        self._project_ids: Dict[str, Any] = {}
        self._issue_stream: Dict[str, Any] = {}
        self._member_to_user: Optional[Dict[str, str]] = None
        self._user_ids: set = set()
        # (name, issue_stream_detector_id) -> [(workflow_id, fingerprint)]
        self._existing_issue: Optional[Dict[Tuple[str, str], List[Tuple[str, str]]]] = None
        self.create_twins = False                       # set by the wrapper (--create-twins)
        self.name_collisions: List[Dict[str, Any]] = [] # same name, DIFFERENT config
        # TEST-ONLY: swap every Slack action for an email so everything ELSE about a Slack alert
        # (conditions, filters, env, logic) can be exercised in an org without the Slack feature.
        self.slack_as_email = False
        self.slack_swapped: List[Dict[str, Any]] = []
        self._current_owner: Optional[str] = None
        self.unresolved_lookups: List[str] = []
        self.translated: List[Dict[str, Any]] = []     # every translated payload (for review)
        self.skipped_existing_issue: List[Dict[str, Any]] = []
        self.created: List[Dict[str, Any]] = []
        self.slack_unresolved: List[Dict[str, Any]] = []
        self._org_envs: Optional[set] = None

    # ------------------------------------------------------------------ HTTP helpers
    def _get_all(self, url: str) -> List[Dict]:
        out = []
        while url:
            for attempt in range(4):  # GETs are safe to retry (connection resets on long paginations)
                try:
                    r = requests.get(url, headers=self.headers, timeout=60)
                    break
                except requests.exceptions.ConnectionError as e:
                    if attempt == 3:
                        raise
                    logger.warning(f"GET {url.split('?')[0]} connection error ({e}); retrying ({attempt + 1}/3)")
                    time.sleep(2 * (attempt + 1))
            if r.status_code >= 400:
                raise AciApiError(f"GET {url} -> {r.status_code}: {r.text[:500]}")
            body = r.json()
            out.extend(body if isinstance(body, list) else [body])
            url = self._next_link(r.headers.get("Link", ""))
        return out

    def _post(self, url: str, payload: Dict) -> Dict:
        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)}")
            return {"id": "dry-run"}
        try:
            r = requests.post(url, headers=self.headers, json=payload, timeout=120)
        except requests.exceptions.Timeout as e:
            raise AciApiError(f"POST {url} TIMED OUT — it MAY have been created server-side; "
                              f"re-run (dedup will skip it if it exists) or check the UI. ({e})")
        if r.status_code >= 400:
            logger.error(f"POST {url} -> {r.status_code}")
            logger.error(f"Payload sent: {json.dumps(payload)}")
            logger.error(f"Response: {r.text}")
            raise AciApiError(f"POST {url} -> {r.status_code}: {r.text[:800]}")
        return r.json()

    def _placeholder(self, kind: str, key: str) -> str:
        ph = f"<{kind}:{key}>"
        if ph not in self.unresolved_lookups:
            self.unresolved_lookups.append(ph)
        return ph

    # ------------------------------------------------------------------ destination lookups
    def project_id(self, org: str, slug: str):
        if slug in self._project_ids:
            return self._project_ids[slug]
        if self.offline:
            pid = self._placeholder("project_id", slug)
        else:
            body = self._get_all(f"{self.base_url}/projects/{org}/{slug}/")[0]
            pid = int(body["id"])
        self._project_ids[slug] = pid
        return pid

    def issue_stream_detector_id(self, org: str, slug: str):
        """The per-project 'issue stream' detector every issue alert must be attached to
        (same lookup the Sentry UI does: type:issue_stream filtered to the project)."""
        if slug in self._issue_stream:
            return self._issue_stream[slug]
        if self.offline:
            did = self._placeholder("issue_stream_detector", slug)
        else:
            pid = self.project_id(org, slug)
            dets = self._get_all(f"{self.base_url}/organizations/{org}/detectors/"
                                 f"?project={pid}&query=type:issue_stream&per_page=100")
            match = [d for d in dets if d.get("type") == "issue_stream"
                     and str(d.get("projectId")) == str(pid)]
            if not match:
                raise TranslationError(f"project '{slug}' has no issue-stream detector in {org}")
            did = int(match[0]["id"])
        self._issue_stream[slug] = did
        return did

    def _load_members(self, org: str):
        if self._member_to_user is not None:
            return
        if self.offline:
            self._member_to_user = {}
            return
        try:
            members = self._get_all(f"{self.base_url}/organizations/{org}/members/?per_page=100")
        except AciApiError as e:
            if " 403" in str(e):
                raise TranslationError(
                    "token cannot list org members (403). Email-to-USER actions need the member id "
                    "-> user id lookup: re-create the token with the `member:read` scope") from e
            raise
        # Only cache AFTER a complete fetch: a half-loaded (or empty) cache made every later
        # lookup fail as "pending invite" after one connection reset (2026-10-01 live run).
        loaded = {}
        for m in members:
            uid = (m.get("user") or {}).get("id")
            if uid is not None:
                loaded[str(m.get("id"))] = str(uid)
                self._user_ids.add(str(uid))
        self._member_to_user = loaded

    def preflight(self, org: str, n_issue: int, n_metric: int, needs_slack: bool,
                  needs_members: bool) -> List[str]:
        """Read-only checks for org-level blockers that would otherwise fail every create.
        Returns a list of blocker strings (empty = OK)."""
        if self.offline:
            return []
        blockers = []
        org_body = self._get_all(f"{self.base_url}/organizations/{org}/")[0]
        feats = set(org_body.get("features") or [])
        plan = (org_body.get("plan") or org_body.get("planDetails") or {})
        if needs_slack and not feats:
            logger.warning("Preflight: the org response carried NO feature list for this token, so the "
                           "Slack-feature check can't be done — continuing; a missing feature will show "
                           "up as a per-alert 400 instead.")
        elif needs_slack and "integrations-alert-rule" not in feats:
            blockers.append(
                "org lacks the 'integrations-alert-rule' feature, so the API rejects every Slack action "
                "('Organization does not allow this action type: slack'). This is plan-gated on SaaS — "
                "the org is probably on the free/Developer plan or its trial lapsed. Fix the plan/trial.")
        n_wf = len({wid for v in self._existing_issue_keys(org).values() for wid, _ in v})
        planned = n_issue + n_metric
        if n_wf + planned > 1000:
            blockers.append(
                f"org already has {n_wf} workflows; +{planned} would exceed the default 1000-per-org cap "
                f"('You may not exceed 1000 workflows per organization'). Needs the "
                f"'organizations:more-workflows' flag (raises it to 10000) — ask Sentry support/eng — "
                f"or delete unneeded workflows first. (If the flag is already on, pass --skip-quota-check.)")
        if needs_members:
            try:
                self._get_all(f"{self.base_url}/organizations/{org}/members/?per_page=1")
            except AciApiError as e:
                if " 403" in str(e):
                    blockers.append("token lacks `member:read` (needed for email-to-user actions)")
        logger.info(f"Preflight: {n_wf} existing workflows, {len(feats)} org features visible, "
                    f"integrations-alert-rule={'integrations-alert-rule' in feats if feats else 'unknown'}")
        return blockers

    def require_environment(self, org: str, name: Optional[str]) -> None:
        """Issue workflows take the environment by NAME and it must already exist in the org."""
        if not name or self.offline:
            return
        if self._org_envs is None:
            self._org_envs = {e.get("name") for e in
                              self._get_all(f"{self.base_url}/organizations/{org}/environments/"
                                            f"?visibility=all")}
        if name not in self._org_envs:
            raise TranslationError(f"environment '{name}' does not exist in {org} — create it "
                                   f"(send one event with that environment, or seed it) and re-run")

    def user_id_for(self, org: str, mapped_id) -> str:
        """The user map holds SaaS org-MEMBER ids; the new email action wants a USER id."""
        v = str(mapped_id)
        if self.offline:
            return self._placeholder("user_id_for_member", v)
        self._load_members(org)
        if v in self._member_to_user:
            return self._member_to_user[v]
        if v in self._user_ids:
            return v  # already a user id
        raise TranslationError(f"SaaS member/user {v} has no user account in {org} "
                               f"(pending invite?) — can't target an email at them")

    # ------------------------------------------------------------------ dedup (read-only)
    def fetch_existing_metric_alerts(self, org_slug: str) -> set:
        """Override of the core's legacy GET /alert-rules/ (now 404). Returns {(name, project_slug)}
        for existing metric_issue detectors so re-runs don't create duplicates."""
        if self.offline:
            logger.info("[OFFLINE] skipping existing-metric-detector dedup fetch")
            return set()
        try:
            projs = self._get_all(f"{self.base_url}/organizations/{org_slug}/projects/?per_page=100")
            id_to_slug = {str(p["id"]): p["slug"] for p in projs}
            for p in projs:
                self._project_ids.setdefault(p["slug"], int(p["id"]))
            dets = self._get_all(f"{self.base_url}/organizations/{org_slug}/detectors/"
                                 f"?project=-1&query=type:metric_issue&per_page=100")
        except (AciApiError, requests.exceptions.RequestException) as e:
            logger.warning(f"Could not fetch existing metric detectors for dedup ({e}); "
                           f"proceeding WITHOUT metric dedup.")
            return set()
        existing = {(d.get("name"), id_to_slug.get(str(d.get("projectId")))) for d in dets
                    if d.get("type") == "metric_issue"}
        logger.info(f"Loaded {len(existing)} existing metric detector (name, project) key(s) for dedup")
        return existing

    @staticmethod
    def _snake_keys(o):
        import re as _re
        if isinstance(o, list):
            return [AciAlertRuleMigrator._snake_keys(x) for x in o]
        if isinstance(o, dict):
            return {_re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", k).lower(): AciAlertRuleMigrator._snake_keys(v)
                    for k, v in o.items()}
        return o

    @classmethod
    def fingerprint(cls, wf: Dict) -> str:
        """Config identity of a workflow, comparable between a translated payload and a workflow
        returned by GET /workflows/ (legacy dual-written or created by us). Ignores ids, names,
        Slack channel ids (cross-workspace) and '#' prefixes; keeps everything that changes behaviour."""
        w = cls._snake_keys(wf)

        def norm_cmp(ctype, cmp_):
            # issue_category include=true is the server default -> same as absent
            if ctype == "issue_category" and isinstance(cmp_, dict) and cmp_.get("include") is True:
                cmp_ = {k: v for k, v in cmp_.items() if k != "include"}
            return cmp_

        def conds(group):
            return sorted(json.dumps([c.get("type"), norm_cmp(c.get("type"), c.get("comparison"))],
                                     sort_keys=True, default=str)
                          for c in (group or {}).get("conditions") or [])

        def logic(group):
            # With 0 or 1 conditions all / any / any-short are equivalent (legacy dual-writes kept
            # 'all' for single-condition groups; the API now requires 'any-short' for triggers).
            lt = (group or {}).get("logic_type")
            n = len((group or {}).get("conditions") or [])
            if n == 0 or (n == 1 and lt in ("all", "any", "any-short")):
                return "single"  # empty / absent group behaves the same whatever its logic
            return "any-short" if lt == "any" else lt

        def acts(af):
            out = []
            for a in af.get("actions") or []:
                cfg = a.get("config") or {}
                tgt = (str(cfg.get("target_display") or "").lstrip("#") if a.get("type") == "slack"
                       else str(cfg.get("target_identifier")))
                out.append(json.dumps([a.get("type"), str(a.get("integration_id") or ""),
                                       cfg.get("target_type"), tgt], default=str))
            return sorted(out)

        trig = w.get("triggers") or {}
        fp = {
            "env": w.get("environment"),
            "freq": (w.get("config") or {}).get("frequency"),
            "when": [logic(trig), conds(trig)],
            "if": sorted(json.dumps([logic(af), conds(af), acts(af)])
                         for af in w.get("action_filters") or []),
        }
        return json.dumps(fp, sort_keys=True)

    def _existing_issue_keys(self, org: str) -> Dict[Tuple[str, str], List[Tuple[str, str]]]:
        if self._existing_issue is not None:
            return self._existing_issue
        self._existing_issue = {}
        if self.offline:
            return self._existing_issue
        try:
            for w in self._get_all(f"{self.base_url}/organizations/{org}/workflows/"
                                   f"?project=-1&per_page=100"):
                fp = self.fingerprint(w)
                for did in w.get("detectorIds") or []:
                    self._existing_issue.setdefault((w.get("name"), str(did)), []).append(
                        (str(w.get("id")), fp))
        except (AciApiError, requests.exceptions.RequestException) as e:
            logger.warning(f"Could not fetch existing workflows for dedup ({e}); proceeding WITHOUT "
                           f"issue-alert dedup (a re-run may create duplicates).")
        logger.info(f"Loaded {len(self._existing_issue)} existing (workflow name, detector) key(s)")
        return self._existing_issue

    # ------------------------------------------------------------------ issue-alert translation
    @staticmethod
    def _cond(ctype: str, comparison: Any) -> Dict:
        return {"type": ctype, "comparison": comparison, "conditionResult": True}

    def _translate_filter(self, n: Dict, org: str) -> Dict:
        """Legacy filter node -> new ACTION-FILTER condition (issue_alert_conditions.py)."""
        nid = n.get("id", "")
        if nid.endswith("TaggedEventFilter") or nid.endswith("TaggedEventCondition"):
            c = {"match": n["match"], "key": n["key"]}
            if n["match"] not in _IS_SET:
                c["value"] = str(n.get("value", ""))
            return self._cond("tagged_event", c)
        if nid.endswith("EventAttributeFilter") or nid.endswith("EventAttributeCondition"):
            c = {"match": n["match"], "attribute": n["attribute"]}
            if n["match"] not in _IS_SET:
                c["value"] = str(n.get("value", ""))
            return self._cond("event_attribute", c)
        if nid.endswith("LevelFilter") or nid.endswith("LevelCondition"):
            return self._cond("level", {"match": n["match"], "level": int(n["level"])})
        if nid.endswith("AgeComparisonFilter"):
            ctype, value = n["comparison_type"], int(n["value"])
            if value < 0:  # server normalises negatives the same way
                value, ctype = -value, ("newer" if ctype == "older" else "older")
            return self._cond("age_comparison", {"comparison_type": ctype, "value": value,
                                                 "time": n["time"]})
        if nid.endswith("AssignedToFilter"):
            tt = n["targetType"]
            c = {"target_type": tt}
            if tt != "Unassigned":
                src = str(n.get("targetIdentifier"))
                if tt == "Team":
                    new = self.team_map.get(src)
                    if new is None:
                        raise TranslationError(f"AssignedToFilter Team {src} has no team mapping")
                    c["target_identifier"] = int(new)
                else:
                    raise TranslationError(f"AssignedToFilter targetType {tt} (source id {src}) "
                                           f"needs a user remap — not supported in this step")
            return self._cond("assigned_to", c)
        if nid.endswith("IssueCategoryFilter"):
            c = {"value": int(n["value"])}
            if n.get("include") is not None:
                c["include"] = n["include"] if isinstance(n["include"], bool) else n["include"] != "false"
            return self._cond("issue_category", c)
        if nid.endswith("IssueTypeFilter"):
            c = {"value": str(n["value"])}
            if n.get("include") is not None:
                c["include"] = n["include"] if isinstance(n["include"], bool) else n["include"] != "false"
            return self._cond("issue_type", c)
        if nid.endswith("IssueOccurrencesFilter"):
            return self._cond("issue_occurrences", {"value": max(int(n["value"]), 0)})
        if nid.endswith("LatestReleaseFilter"):
            return self._cond("latest_release", True)
        if nid.endswith("LatestAdoptedReleaseFilter"):
            return self._cond("latest_adopted_release", {
                "release_age_type": n["oldest_or_newest"], "age_comparison": n["older_or_newer"],
                "environment": n["environment"]})
        raise TranslationError(f"unsupported filter node {nid}")

    def _translate_when(self, n: Dict) -> Tuple[str, Optional[Dict]]:
        """Legacy condition node -> ('trigger'|'freq'|'skip', new condition)."""
        nid = n.get("id", "")
        if nid == _EVERY_EVENT:
            return "every", self._cond("every_event", True)
        if nid in _TRIGGER_TYPES:
            return "trigger", self._cond(_TRIGGER_TYPES[nid], True)
        if nid in _FREQ:
            count_t, pct_t, cast = _FREQ[nid]
            c = {"interval": n["interval"], "value": max(cast(n["value"]), 0)}
            if (n.get("comparisonType") or "count") == "count":
                return "freq", self._cond(count_t, c)
            c["comparison_interval"] = n["comparisonInterval"]
            return "freq", self._cond(pct_t, c)
        raise TranslationError(f"unsupported condition node {nid}")

    def _translate_issue_action(self, a: Dict, org: str) -> Dict:
        """Core-ported legacy action -> new Action (typings/notification_action.py)."""
        aid = a.get("id", "")
        if aid.endswith("NotifyEmailAction"):
            tt = a.get("targetType")
            if tt == "Team":
                cfg = {"targetType": "team", "targetIdentifier": str(a["targetIdentifier"])}
                return {"type": "email", "config": cfg, "data": {}}
            if tt == "Member":
                cfg = {"targetType": "user",
                       "targetIdentifier": self.user_id_for(org, a["targetIdentifier"])}
                return {"type": "email", "config": cfg, "data": {}}
            if tt == "IssueOwners":
                return {"type": "email",
                        "config": {"targetType": "issue_owners", "targetIdentifier": None},
                        "data": {"fallthrough_type": a.get("fallthroughType") or "ActiveMembers"}}
            raise TranslationError(f"email targetType {tt} unsupported")
        if "slack" in aid.lower() and self.slack_as_email:
            return self._slack_swap(a.get("channel"))
        if "slack" in aid.lower():
            cfg = {"targetType": "specific", "targetDisplay": a.get("channel")}
            if a.get("channel_id"):
                cfg["targetIdentifier"] = a["channel_id"]
            data = {"tags": a["tags"]} if a.get("tags") else {}
            if a.get("notes"):
                data["notes"] = a["notes"]
            return {"type": "slack", "integrationId": int(a["workspace"]), "config": cfg, "data": data}
        raise TranslationError(f"action {aid.split('.')[-1]} not supported by the ACI translator "
                               f"(PagerDuty/Opsgenie/etc. are out of scope for this step)")

    def build_issue_workflow(self, org: str, project_slug: str, p: Dict) -> Dict:
        am = p.get("actionMatch") or "all"
        fm = p.get("filterMatch") or "all"
        triggers, freqs = [], []
        for n in p.get("conditions") or []:
            kind, c = self._translate_when(n)
            if kind == "trigger":
                triggers.append(c)
            elif kind == "freq":
                freqs.append(c)
            elif kind == "every" and am == "any":
                triggers.append(c)  # "every event OR x" fires on every event — keep it
            # kind == "every" under all/none: ALL(every_event, x) == x -> dropping is exact
        filters = [self._translate_filter(n, org) for n in p.get("filters") or []]

        # WHEN group. Mirrors Sentry's migrator (all "when" conditions incl. frequency live in
        # triggers). The API requires 'any-short' whenever a trigger-type condition is present.
        if am == "any":
            when_logic, when = "any-short", triggers + freqs
        elif am == "all":
            if len(triggers) >= 2:
                raise TranslationError("action_match=all with 2+ trigger conditions can't be "
                                       "expressed (the API requires any-short for triggers)")
            if not triggers:
                when_logic, when = "all", freqs
            elif not freqs:
                when_logic, when = "any-short", triggers  # ALL of one == ANY of one
            elif fm == "all" or not filters:
                # first-seen AND frequency AND filters: move the frequency into the ALL filter group
                when_logic, when = "any-short", triggers
                filters, fm = filters + freqs, "all"
            else:
                raise TranslationError("action_match=all mixing a trigger + frequency with "
                                       "filter_match!=all can't be expressed faithfully")
        else:  # 'none'
            if triggers:
                raise TranslationError("action_match=none with trigger conditions is not expressible")
            when_logic, when = "none", freqs

        filter_logic = _LOGIC.get(fm, "all") if filters else "all"
        self._current_owner = p.get("owner")
        actions = [self._translate_issue_action(a, org) for a in p.get("actions") or []]
        # dedupe identical swapped emails (e.g. Slack + team email -> two identical team emails)
        _seen, _uniq = set(), []
        for _a in actions:
            _k = json.dumps(_a, sort_keys=True)
            if _k not in _seen:
                _seen.add(_k); _uniq.append(_a)
        actions = _uniq
        if not actions:
            raise TranslationError("no actions after translation")

        self.require_environment(org, p.get("environment"))
        wf = {
            "name": p["name"],
            "enabled": True,
            "environment": p.get("environment"),
            "config": {"frequency": int(p.get("frequency") or 30)},
            "detectorIds": [self.issue_stream_detector_id(org, project_slug)],
            "triggers": {"logicType": when_logic, "conditions": when},
            "actionFilters": [{"logicType": filter_logic, "conditions": filters, "actions": actions}],
        }
        if p.get("owner"):
            wf["owner"] = p["owner"]
        return wf

    def _check_slack_resolved(self, body: Dict, name: str, where: str) -> None:
        """A Slack name lookup that timed out server-side is saved with an empty channel id (201)."""
        if self.dry_run:
            return
        for af in body.get("actionFilters") or []:
            for a in af.get("actions") or []:
                if a.get("type") == "slack" and not (a.get("config") or {}).get("targetIdentifier"):
                    rec = {"workflow_id": body.get("id"), "name": name, "where": where,
                           "channel": (a.get("config") or {}).get("targetDisplay")}
                    self.slack_unresolved.append(rec)
                    logger.warning(f"Workflow {body.get('id')} '{name}' ({where}): Slack channel "
                                   f"'{rec['channel']}' saved WITHOUT a channel id (Sentry's name "
                                   f"lookup timed out) — it will never post. Fix in the UI.")

    def create_issue_alert_rule(self, org_slug: str, project_slug: str, payload: Dict) -> Dict:
        try:
            wf = self.build_issue_workflow(org_slug, project_slug, payload)
        except TranslationError as e:
            logger.error(f"Issue alert '{payload.get('name')}' on {project_slug}: TRANSLATION FAILED: {e}")
            raise
        self.translated.append({"kind": "issue", "project": project_slug, "workflow": wf})

        det = wf["detectorIds"][0]
        same_name = self._existing_issue_keys(org_slug).get((wf["name"], str(det))) or []
        if same_name:
            mine = self.fingerprint(wf)
            identical = [wid for wid, fp in same_name if fp == mine]
            if identical:
                self.skipped_existing_issue.append({"name": wf["name"], "project": project_slug,
                                                    "existing_workflow_ids": identical})
                logger.info(f"Issue alert '{wf['name']}' on {project_slug}: SKIPPED — identical "
                            f"workflow already exists (id {', '.join(identical)})")
                return {"id": "skipped-existing", "name": wf["name"], "project": project_slug,
                        "skipped_existing": True, "existing_workflow_ids": identical}
            coll = {"name": wf["name"], "project": project_slug,
                    "existing_workflow_ids": [wid for wid, _ in same_name],
                    "existing_fingerprints": [json.loads(fp) for _, fp in same_name],
                    "this_fingerprint": json.loads(mine)}
            if not self.create_twins:
                self.name_collisions.append({**coll, "action": "skipped"})
                logger.warning(f"Issue alert '{wf['name']}' on {project_slug}: NAME COLLISION — a "
                               f"workflow with this name exists (id {coll['existing_workflow_ids']}) but "
                               f"its CONFIG DIFFERS. Not created. Pass --create-twins to create it as "
                               f"'{wf['name']} (v24)'. Diff is in results -> name_collisions.")
                return {"id": "skipped-name-collision", "name": wf["name"], "project": project_slug,
                        "skipped_existing": True, "name_collision": True}
            wf = dict(wf, name=f"{wf['name']} (v24)"[:256])
            self.name_collisions.append({**coll, "action": f"created as '{wf['name']}'"})
            logger.warning(f"Issue alert on {project_slug}: name collision with different config — "
                           f"creating as '{wf['name']}' (--create-twins)")

        body = self._post(f"{self.base_url}/organizations/{org_slug}/workflows/", wf)
        self._check_slack_resolved(body, wf["name"], project_slug)
        rec = {"id": body.get("id"), "workflow_id": body.get("id"), "name": wf["name"],
               "project": project_slug, "api": "workflows", "dry_run": self.dry_run}
        self.created.append({"kind": "issue", **rec})
        return rec

    # ------------------------------------------------------------------ metric-alert translation
    @staticmethod
    def metric_signature(name, projects, query, aggregate, triggers) -> Tuple:
        """Identity of a metric alert as seen in the core's legacy payload. Used to recover the
        source pk + environment the legacy payload doesn't carry."""
        trig = tuple(sorted((t.get("label"), float(t.get("alertThreshold") or 0)) for t in triggers))
        return (name, tuple(sorted(projects)), (query or "").strip(), (aggregate or "").strip(), trig)

    def _slack_swap(self, channel) -> Dict:
        owner = self._current_owner or ""
        self.slack_swapped.append({"channel": channel, "owner": owner})
        if owner.startswith("team:"):
            return {"type": "email", "data": {},
                    "config": {"targetType": "team", "targetIdentifier": owner.split(":", 1)[1]}}
        return {"type": "email", "data": {"fallthrough_type": "ActiveMembers"},
                "config": {"targetType": "issue_owners", "targetIdentifier": None}}

    def _translate_metric_action(self, a: Dict, org: str) -> Dict:
        if a.get("type") == "email":
            if a.get("targetType") == "team":
                return {"type": "email", "data": {},
                        "config": {"targetType": "team", "targetIdentifier": str(a["targetIdentifier"])}}
            if a.get("targetType") == "user":
                if a.get("targetIdentifier") in (None, "", "None"):
                    raise TranslationError("metric email action has no user target (unmapped owner)")
                return {"type": "email", "data": {},
                        "config": {"targetType": "user",
                                   "targetIdentifier": self.user_id_for(org, a["targetIdentifier"])}}
        if a.get("type") == "slack" and self.slack_as_email:
            return self._slack_swap(a.get("targetIdentifier"))
        if a.get("type") == "slack":
            cfg = {"targetType": "specific", "targetDisplay": (a.get("targetIdentifier") or "").lstrip("#")}
            if a.get("inputChannelId"):
                cfg["targetIdentifier"] = a["inputChannelId"]
            return {"type": "slack", "integrationId": int(a["integrationId"]), "config": cfg, "data": {}}
        raise TranslationError(f"metric action {a} not supported by the ACI translator")

    def build_metric(self, org: str, p: Dict) -> Tuple[Dict, List[Tuple[str, Dict]], Dict]:
        sig = self.metric_signature(p["name"], p["projects"], p.get("query"), p.get("aggregate"),
                                    p.get("triggers") or [])
        ctx = self.metric_context.get(sig)
        if ctx is None:
            raise TranslationError("could not match this metric alert back to its source row "
                                   "(signature not found) — refusing to guess its environment")
        if ctx.get("ambiguous"):
            raise TranslationError("metric alert signature is ambiguous in the export — refusing to guess")
        if (ctx.get("detection_type") or "static") != "static" and not p.get("comparisonDelta"):
            raise TranslationError(f"detection_type={ctx.get('detection_type')} (anomaly) alerts are "
                                   f"not supported by this translator — rebuild manually")

        above = int(p.get("thresholdType") or 0) == 0
        cmp_t, res_t = ("gt", "lte") if above else ("lt", "gte")
        conds, thresholds = [], {}
        for t in p.get("triggers") or []:
            prio = _PRIORITY.get(t.get("label"), 75)
            thresholds[t.get("label")] = t.get("alertThreshold")
            conds.append({"type": cmp_t, "comparison": t.get("alertThreshold"), "conditionResult": prio})
        resolve = p.get("resolveThreshold")
        if resolve is None:  # same rule as migration_helpers.alert_rule.get_resolve_threshold
            resolve = thresholds.get("warning", thresholds.get("critical"))
        conds.append({"type": res_t, "comparison": resolve, "conditionResult": 0})

        is_eap = p.get("dataset") == SPAN_DATASET
        tw = int(p["timeWindow"]) if is_eap else int(p["timeWindow"]) * 60  # classic = minutes
        ds = {
            "dataset": p.get("dataset"),
            "query": p.get("query") or "",
            "aggregate": p.get("aggregate"),
            "timeWindow": tw,
            "queryType": p.get("queryType", 0),
            "eventTypes": p.get("eventTypes") or ["error"],
            "environment": ctx.get("environment"),
        }
        if is_eap:
            ds["extrapolationMode"] = "unknown"
        config = {"detectionType": "static"}
        if p.get("comparisonDelta"):
            config = {"detectionType": "percent", "comparisonDelta": int(p["comparisonDelta"]) * 60}

        self._current_owner = p.get("owner")
        action_filters = []
        for t in p.get("triggers") or []:
            prio = _PRIORITY.get(t.get("label"), 75)
            action_filters.append({
                "logicType": "any-short",
                "conditions": [
                    {"type": "issue_priority_greater_or_equal", "comparison": prio, "conditionResult": True},
                    {"type": "issue_priority_deescalating", "comparison": prio, "conditionResult": True},
                ],
                "actions": [self._translate_metric_action(a, org) for a in t.get("actions") or []],
            })
        workflow = {"name": p["name"], "enabled": True, "actionFilters": action_filters}
        if p.get("owner"):
            workflow["owner"] = p["owner"]

        detectors = []
        for slug in p["projects"]:
            dname = p["name"] if len(p["name"]) < 200 else p["name"][:197] + "..."
            d = {"name": dname, "type": "metric_issue", "enabled": True,
                 "dataSources": [ds], "config": config,
                 "conditionGroup": {"logicType": "any", "conditions": conds}}
            if p.get("owner"):
                d["owner"] = p["owner"]
            detectors.append((slug, d))
        return workflow, detectors, ctx

    def _find_orphan_workflow(self, org: str, workflow: Dict) -> Optional[str]:
        if self.offline:
            return None
        if not hasattr(self, "_all_workflows"):
            try:
                self._all_workflows = self._get_all(
                    f"{self.base_url}/organizations/{org}/workflows/?project=-1&per_page=100")
            except (AciApiError, requests.exceptions.RequestException):
                self._all_workflows = []
        mine = self.fingerprint(workflow)
        for w in self._all_workflows:
            if (w.get("name") == workflow["name"] and not w.get("detectorIds")
                    and self.fingerprint(w) == mine):
                return str(w["id"])
        return None

    def create_alert_rule(self, org_slug: str, payload: Dict) -> Dict:
        try:
            workflow, detectors, ctx = self.build_metric(org_slug, payload)
        except TranslationError as e:
            logger.error(f"Metric alert '{payload.get('name')}': TRANSLATION FAILED: {e}")
            raise
        self.translated.append({"kind": "metric", "source_pk": ctx.get("pk"),
                                "workflow": workflow, "detectors": [
                                    {"project": s, "detector": d} for s, d in detectors]})

        # 1) the notifications first — a detector without its workflow would fire silently.
        # Re-run safety: if an earlier run created this workflow but its monitor failed (e.g. a
        # quota 400), a same-named workflow with NO monitors and the same config is left behind.
        # Re-attach to it instead of creating a second orphan.
        orphan = self._find_orphan_workflow(org_slug, workflow)
        if orphan:
            wf_id = orphan
            logger.info(f"Metric alert '{workflow['name']}': re-using detached workflow {wf_id} "
                        f"from an earlier partial run")
        else:
            wf_body = self._post(f"{self.base_url}/organizations/{org_slug}/workflows/", workflow)
            self._check_slack_resolved(wf_body, workflow["name"], ",".join(s for s, _ in detectors))
            wf_id = wf_body.get("id")
        det_ids = []
        for slug, d in detectors:
            d = dict(d, workflowIds=[int(wf_id)] if str(wf_id).isdigit() else [wf_id])
            try:
                body = self._post(f"{self.base_url}/organizations/{org_slug}/projects/{slug}/detectors/", d)
            except AciApiError as e:
                raise AciApiError(f"{e}  [NOTE: workflow {wf_id} '{workflow['name']}' was created but "
                                  f"has no monitor — it can never fire; delete or re-attach it]")
            det_ids.append(body.get("id"))
        rec = {"id": wf_id, "workflow_id": wf_id, "detector_ids": det_ids, "name": workflow["name"],
               "projects": [s for s, _ in detectors], "source_pk": ctx.get("pk"),
               "environment": ctx.get("environment"), "api": "workflows+detectors",
               "dry_run": self.dry_run}
        self.created.append({"kind": "metric", **rec})
        return rec


def build_metric_context(reduced: List[Dict], source_org: Optional[str]) -> Dict[Tuple, Dict[str, Any]]:
    """From the (scoped) export, compute the same signature the core's legacy payload will carry
    for each metric alert -> {pk, environment}. Uses the core's own translation so the query /
    aggregate match exactly what the core puts in the payload."""
    core = AlertRuleMigrator("x")
    source_pk = core.resolve_source_org_pk(reduced, source_org)
    snuba = core.build_snuba_index(reduced)
    envs = core.build_environments(reduced)
    slugs = core.build_project_slugs(reduced, source_pk=source_pk)
    rprojects = core.build_rule_projects(reduced)
    rtriggers = core.build_rule_triggers(reduced)
    out: Dict[Tuple, Dict[str, Any]] = {}
    for i in reduced:
        if not isinstance(i, dict) or i.get("model") != "sentry.alertrule":
            continue
        f = i.get("fields") or {}
        s = snuba.get(f.get("snuba_query")) or {}
        q, agg = s.get("query", ""), s.get("aggregate", "count()")
        if s.get("dataset") in TRANSACTION_DATASETS:
            ov, _ = AlertRuleMigrator.translate_transaction_snuba(s)
            if ov:
                q, agg = ov["query"], ov["aggregate"]
        projects = [slugs[p] for p in rprojects.get(i["pk"], []) if p in slugs]
        trig = rtriggers.get(i["pk"]) or [{"label": "critical", "alertThreshold": 100}]
        sig = AciAlertRuleMigrator.metric_signature(f.get("name"), projects, q, agg, trig)
        entry = {"pk": i["pk"],
                 "environment": envs.get(s.get("environment")) if s.get("environment") else None,
                 "detection_type": f.get("detection_type") or "static"}
        if sig in out:
            out[sig] = {"ambiguous": True, "pks": [out[sig].get("pk"), i["pk"]]}
        else:
            out[sig] = entry
    return out
