#!/usr/bin/env python3
"""
validate_aci_payloads.py — offline pre-flight for migrate_safe_alerts_aci.py results.

Re-checks every translated workflow / detector payload against the SAME rules the Sentry
server applies on POST, mirrored from the source in ../sentry (master @ 10b6966eb43):

  workflow_engine/endpoints/validators/base/{workflow,action,data_condition,data_condition_group}.py
  workflow_engine/handlers/condition/*_handler.py         (comparison_json_schema per type)
  notifications/notification_action/action_handler_registry/email_handler.py, slack handler
  incidents/metric_issue_detector.py, snuba/snuba_query_validator.py, incidents/grouptype.py

It cannot check things only the live org knows (team/user existence, Slack channel names,
environment names on the org) — those are what the non-offline dry run's GETs + the live
API are for. Exit code 1 if any payload fails.

    python3 validate_aci_payloads.py dry-run-outputs/aci_safe_alert_migration_results_<ts>.json
"""
import json
import re
import sys

import jsonschema

# ---- request key conversion (CamelSnakeSerializer: recursive camel -> snake on keys) ----------
_re_camel = re.compile(r"(((?<=[a-z])[A-Z])|([A-Z](?![A-Z]|$)))")


def snake(obj):
    if isinstance(obj, list):
        return [snake(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    return {_re_camel.sub(r"_\1", k).strip("_").lower(): snake(v) for k, v in obj.items()}


MATCH = ["co", "ew", "eq", "is", "in", "nc", "new", "ne", "ns", "nsw", "nin", "sw"]
IS_SET = ["is", "ns"]
ATTRS = ("message platform environment type error.handled error.unhandled error.main_thread "
         "exception.type exception.value user.id user.email user.username user.ip_address "
         "http.method http.url http.status_code sdk.name stacktrace.code stacktrace.module "
         "stacktrace.filename stacktrace.abs_path stacktrace.package unreal.crash_type "
         "app.in_foreground os.distribution_name os.distribution_version symbolicated_in_app "
         "ota_updates.channel ota_updates.runtime_version ota_updates.update_id").split()
TAG_KEY = {"type": "string", "pattern": r"^[a-zA-Z0-9_\.:-]+$"}
INTERVALS = ["1m", "5m", "15m", "1h", "1d", "1w", "30d"]


def _kv(field, field_schema):
    return {
        "type": "object",
        "properties": {field: field_schema, "match": {"type": "string", "enum": MATCH},
                       "value": {"type": "string"}},
        "oneOf": [
            {"properties": {field: field_schema, "match": {"enum": IS_SET}},
             "required": [field, "match"], "not": {"required": ["value"]}},
            {"properties": {field: field_schema, "match": {"not": {"enum": IS_SET}},
                            "value": {"type": "string"}},
             "required": [field, "match", "value"]},
        ],
        "additionalProperties": False,
    }


TAGGED = _kv("key", TAG_KEY)
ATTRIB = _kv("attribute", {"type": "string", "enum": ATTRS})
FREQ_FILTERS = {"type": "array", "items": {"anyOf": [TAGGED, ATTRIB]}}

TRIGGER_TYPES = {"first_seen_event", "regression_event", "reappeared_event", "every_event",
                 "existing_high_priority_issue", "new_high_priority_issue", "issue_resolved_trigger",
                 "issue_resolution_change", "seer_activity_trigger"}

COND_SCHEMA = {
    "first_seen_event": {"type": "boolean"},
    "every_event": {"type": "boolean"},
    "regression_event": {"type": "boolean"},
    "reappeared_event": {"type": "boolean"},
    "new_high_priority_issue": {"type": "boolean"},
    "existing_high_priority_issue": {"type": "boolean"},
    "latest_release": {"type": "boolean"},
    "tagged_event": TAGGED,
    "event_attribute": ATTRIB,
    "level": {"type": "object", "properties": {
        "level": {"type": "integer", "enum": [0, 10, 20, 30, 40, 50]},
        "match": {"type": "string", "enum": ["eq", "gte", "lte"]}},
        "required": ["level", "match"], "additionalProperties": False},
    "age_comparison": {"type": "object", "properties": {
        "comparison_type": {"type": "string", "enum": ["older", "newer"]},
        "value": {"type": "integer", "minimum": 0},
        "time": {"type": "string", "enum": ["minute", "hour", "day", "week"]}},
        "required": ["comparison_type", "value", "time"], "additionalProperties": False},
    "assigned_to": {"type": "object", "properties": {
        "target_type": {"type": "string", "enum": ["Unassigned", "Team", "Member"]},
        "target_identifier": {"type": ["integer", "string"]}},
        "required": ["target_type"], "additionalProperties": False,
        "allOf": [{"if": {"properties": {"target_type": {"const": "Unassigned"}}},
                   "then": {"required": ["target_type"]},
                   "else": {"required": ["target_type", "target_identifier"]}}]},
    "issue_category": {"type": "object", "properties": {
        "value": {"type": "integer", "minimum": 1}, "include": {"type": "boolean"}},
        "required": ["value"], "additionalProperties": False},
    "issue_occurrences": {"type": "object", "properties": {
        "value": {"type": "integer", "minimum": 0}},
        "required": ["value"], "additionalProperties": False},
    "event_frequency_count": {"type": "object", "properties": {
        "interval": {"type": "string", "enum": INTERVALS},
        "value": {"type": "integer", "minimum": 0}, "filters": FREQ_FILTERS},
        "required": ["interval", "value"], "additionalProperties": False},
    "event_unique_user_frequency_count": {"type": "object", "properties": {
        "interval": {"type": "string", "enum": INTERVALS},
        "value": {"type": "integer", "minimum": 0}, "filters": FREQ_FILTERS},
        "required": ["interval", "value"], "additionalProperties": False},
    "event_frequency_percent": {"type": "object", "properties": {
        "interval": {"type": "string", "enum": INTERVALS},
        "value": {"type": "integer", "minimum": 0},
        "comparison_interval": {"type": "string", "enum": ["5m", "15m", "1h", "1d", "1w", "30d"]},
        "filters": FREQ_FILTERS},
        "required": ["interval", "value", "comparison_interval"], "additionalProperties": False},
    "event_unique_user_frequency_percent": {"type": "object", "properties": {
        "interval": {"type": "string", "enum": INTERVALS},
        "value": {"type": "integer", "minimum": 0},
        "comparison_interval": {"type": "string", "enum": ["5m", "15m", "1h", "1d", "1w", "30d"]},
        "filters": FREQ_FILTERS},
        "required": ["interval", "value", "comparison_interval"], "additionalProperties": False},
    "issue_priority_greater_or_equal": {"type": "integer", "enum": [25, 50, 75]},
    "issue_priority_deescalating": {"type": "integer", "enum": [25, 50, 75]},
}

EMAIL_CONFIG_API = {  # email_handler.config_schema with target_type as its API string
    "type": "object",
    "properties": {"target_identifier": {"type": ["string", "null"]},
                   "target_display": {"type": ["null"]},
                   "target_type": {"type": "string", "enum": ["user", "team", "issue_owners"]}},
    "required": ["target_type"], "additionalProperties": False,
    "allOf": [{"if": {"properties": {"target_type": {"enum": ["user", "team"]}}},
               "then": {"properties": {"target_identifier": {"type": "string"}},
                        "required": ["target_type", "target_identifier"]}}]}
EMAIL_DATA = {"type": "object", "properties": {"fallthrough_type": {
    "type": "string", "enum": ["AllMembers", "ActiveMembers", "NoOne"]}}, "additionalProperties": False}
SLACK_CONFIG_API = {  # MESSAGING_ACTION_CONFIG_SCHEMA, API form
    "type": "object",
    "properties": {"target_identifier": {"type": ["string"]}, "target_display": {"type": ["string"]},
                   "target_type": {"type": "string", "enum": ["specific"]}},
    "required": ["target_display", "target_type"], "additionalProperties": False}
SLACK_DATA = {"type": "object", "properties": {"tags": {"type": "string"}, "notes": {"type": "string"}},
              "additionalProperties": False}
WORKFLOW_CONFIG = {"type": "object", "properties": {"frequency": {"type": "integer", "minimum": 0}},
                   "additionalProperties": False}
DETECTOR_CONFIG = {"type": "object", "required": ["detection_type"], "properties": {
    "comparison_delta": {"type": ["integer", "null"]},
    "detection_type": {"type": "string", "enum": ["static", "percent", "dynamic"]}}}
LOGIC = {"any", "any-short", "all", "none"}
EAP_TW = [300, 600, 900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400]


def is_placeholder(v):
    return isinstance(v, str) and v.startswith("<") and v.endswith(">")


def check_cond(c, where, errs):
    t = c.get("type")
    if t not in COND_SCHEMA:
        errs.append(f"{where}: unknown/unchecked condition type {t!r}")
        return
    if c.get("condition_result") is not True:
        errs.append(f"{where}: {t} condition_result must be true")
    try:
        jsonschema.validate(c.get("comparison"), COND_SCHEMA[t])
    except jsonschema.ValidationError as e:
        errs.append(f"{where}: {t} comparison {c.get('comparison')!r} invalid: {e.message}")


def check_action(a, where, errs):
    t = a.get("type")
    if t == "email":
        if a.get("integration_id") is not None:
            errs.append(f"{where}: email must not carry integration_id")
        cfg = dict(a.get("config") or {})
        if is_placeholder(cfg.get("target_identifier")):
            cfg["target_identifier"] = "0"
        for schema, val, lbl in ((EMAIL_CONFIG_API, cfg, "config"), (EMAIL_DATA, a.get("data"), "data")):
            try:
                jsonschema.validate(val, schema)
            except jsonschema.ValidationError as e:
                errs.append(f"{where}: email {lbl} invalid: {e.message}")
        tt, ti = cfg.get("target_type"), cfg.get("target_identifier")
        if tt in ("user", "team") and not str(ti).isdigit():
            errs.append(f"{where}: email {tt} target_identifier must be an integer string, got {ti!r}")
    elif t == "slack":
        if a.get("integration_id") is None:
            errs.append(f"{where}: slack requires integration_id")
        for schema, val, lbl in ((SLACK_CONFIG_API, a.get("config"), "config"), (SLACK_DATA, a.get("data"), "data")):
            try:
                jsonschema.validate(val, schema)
            except jsonschema.ValidationError as e:
                errs.append(f"{where}: slack {lbl} invalid: {e.message}")
    else:
        errs.append(f"{where}: action type {t!r} not covered by this validator")


def check_workflow(wf_raw, where, errs):
    wf = snake(wf_raw)
    if not wf.get("name") or len(wf["name"]) > 256:
        errs.append(f"{where}: bad name")
    try:
        jsonschema.validate(wf.get("config", {}), WORKFLOW_CONFIG)
    except jsonschema.ValidationError as e:
        errs.append(f"{where}: config invalid: {e.message}")
    owner = wf.get("owner")
    if owner is not None and not re.fullmatch(r"(team|user):\d+", str(owner)):
        errs.append(f"{where}: owner {owner!r} not 'team:<id>'/'user:<id>'")
    trig = wf.get("triggers")
    if trig is not None:
        lt = trig.get("logic_type")
        if lt not in LOGIC:
            errs.append(f"{where}: triggers logic {lt!r} invalid")
        conds = trig.get("conditions") or []
        if any(c.get("type") in TRIGGER_TYPES for c in conds) and lt != "any-short":
            errs.append(f"{where}: Triggers' logic type must be 'any-short' (API rule on create)")
        for k, c in enumerate(conds):
            check_cond(c, f"{where}.triggers[{k}]", errs)
    for j, af in enumerate(wf.get("action_filters") or []):
        if af.get("logic_type") not in LOGIC:
            errs.append(f"{where}.actionFilters[{j}]: logic {af.get('logic_type')!r} invalid")
        if "actions" not in af:
            errs.append(f"{where}.actionFilters[{j}]: Missing actions key")
        for k, c in enumerate(af.get("conditions") or []):
            if c.get("type") in TRIGGER_TYPES:
                errs.append(f"{where}.actionFilters[{j}]: trigger-type {c['type']} placed in a filter")
            check_cond(c, f"{where}.actionFilters[{j}].conditions[{k}]", errs)
        if not af.get("actions"):
            errs.append(f"{where}.actionFilters[{j}]: no actions — alert would notify nobody")
        for k, a in enumerate(af.get("actions") or []):
            check_action(a, f"{where}.actionFilters[{j}].actions[{k}]", errs)


def check_detector(d_raw, where, errs):
    d = snake(d_raw)
    if d.get("type") != "metric_issue":
        errs.append(f"{where}: type must be metric_issue")
    if not d.get("name") or len(d["name"]) > 200:
        errs.append(f"{where}: bad name")
    try:
        jsonschema.validate(d.get("config"), DETECTOR_CONFIG)
    except jsonschema.ValidationError as e:
        errs.append(f"{where}: config invalid: {e.message}")
    cg = d.get("condition_group") or {}
    conds = cg.get("conditions") or []
    if not conds:
        errs.append(f"{where}: conditionGroup.conditions required")
    if len(conds) > 3:
        errs.append(f"{where}: Too many conditions (max 3)")
    for k, c in enumerate(conds):
        if c.get("type") not in ("gt", "lt", "gte", "lte", "anomaly_detection"):
            errs.append(f"{where}.conditions[{k}]: Unsupported type {c.get('type')}")
        if c.get("condition_result") not in (75, 50, 0):
            errs.append(f"{where}.conditions[{k}]: Unsupported condition result {c.get('condition_result')}")
        if not isinstance(c.get("comparison"), (int, float)):
            errs.append(f"{where}.conditions[{k}]: comparison must be a number")
    if not any(c.get("condition_result") == 0 for c in conds):
        errs.append(f"{where}: Resolution condition required for metric issue detector.")
    ds_list = d.get("data_sources") or []
    if len(ds_list) != 1:
        errs.append(f"{where}: expected exactly 1 data source")
    for ds in ds_list:
        for req in ("dataset", "query", "aggregate", "time_window", "event_types"):
            if req not in ds:
                errs.append(f"{where}: dataSource missing {req}")
        if "environment" not in ds:
            errs.append(f"{where}: dataSource.environment is required (may be null)")
        if ds.get("dataset") in ("transactions", "generic_metrics"):
            errs.append(f"{where}: transaction dataset creation is disabled — must be events_analytics_platform")
        if ds.get("dataset") == "events_analytics_platform":
            if ds.get("event_types") != ["trace_item_span"]:
                errs.append(f"{where}: EAP event_types must be ['trace_item_span']")
            if ds.get("query_type") != 1:
                errs.append(f"{where}: EAP query_type must be 1 (performance)")
            if ds.get("time_window") not in EAP_TW:
                errs.append(f"{where}: EAP time_window {ds.get('time_window')}s not in {EAP_TW}")
            if ds.get("extrapolation_mode") in ("server_weighted", "none"):
                errs.append(f"{where}: extrapolation_mode not allowed for new detectors")
        if ds.get("aggregate") == "upsampled_count()":
            errs.append(f"{where}: upsampled_count() not allowed as input")


def main(path):
    r = json.load(open(path))
    tp = r.get("translated_payloads") or []
    errs = []
    n_wf = n_det = 0
    for i, t in enumerate(tp):
        label = f"[{t['kind']} {t.get('project') or t.get('source_pk')} '{t['workflow']['name']}']"
        check_workflow(t["workflow"], label + ".workflow", errs)
        n_wf += 1
        if t["kind"] == "issue":
            if len(t["workflow"].get("detectorIds") or []) != 1:
                errs.append(f"{label}: issue workflow must attach to exactly one issue-stream detector")
        for d in t.get("detectors") or []:
            check_detector(d["detector"], f"{label}.detector[{d['project']}]", errs)
            n_det += 1
    print(f"Checked {n_wf} workflow payload(s) and {n_det} detector payload(s) from {path}")
    if errs:
        print(f"\nFAIL — {len(errs)} problem(s):")
        for e in errs:
            print("  " + e)
        return 1
    print("PASS — every payload satisfies the server-side schema/logic rules mirrored here.")
    ph = r.get("unresolved_lookups") or []
    if ph:
        print(f"(note: {len(ph)} offline placeholder id(s) were substituted for this check)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
