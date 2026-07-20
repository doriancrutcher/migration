"""Unit tests for issue-alert (sentry.rule) migration in core/migrate_alert_rules.py.

Hermetic: `requests` is stubbed before importing the module, so these run with plain
`python3` -- no network, no third-party deps. `requests.post` is recorded so we can
assert the exact payload/URL the migrator would send.

Run:  python3 -m unittest discover -s tests
  or: python3 tests/test_issue_alerts.py
"""
import json
import logging
import os
import sys
import tempfile
import types
import unittest

logging.disable(logging.CRITICAL)  # keep test output clean

# --- stub `requests` before importing the module under test ---
_POSTS = []  # each: {"url":..., "headers":..., "json":...}


class _FakeRequestException(Exception):
    pass


class _FakeResponse:
    def __init__(self, payload):
        self._payload = {"id": "new-id", "name": payload.get("name"), "echo": payload}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _fake_post(url, headers=None, json=None):
    _POSTS.append({"url": url, "headers": headers, "json": json})
    return _FakeResponse(json or {})


_fake_requests = types.ModuleType("requests")
_fake_requests.post = _fake_post
_fake_requests.exceptions = types.SimpleNamespace(RequestException=_FakeRequestException)
sys.modules["requests"] = _fake_requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import migrate_alert_rules as mar  # noqa: E402


def make_rule(pk=1, project=2, label="rule", owner_team=None, environment_id=None,
              action_match="any", filter_match="all", frequency=None,
              conditions=None, filters=None):
    blob = {"action_match": action_match, "filter_match": filter_match,
            "conditions": conditions if conditions is not None else [],
            "filters": filters if filters is not None else [],
            "actions": []}
    if frequency is not None:
        blob["frequency"] = frequency
    return {"model": "sentry.rule", "pk": pk, "fields": {
        "project": project, "environment_id": environment_id, "label": label,
        "owner_team": owner_team, "data": json.dumps(blob)}}


class IssueAlertMigrationTests(unittest.TestCase):
    def setUp(self):
        _POSTS.clear()
        self.m = mar.AlertRuleMigrator("tok", dry_run=False)
        self.project_slugs = {2: "checkout-service", 3: "payments-api"}
        self.team_map = {"50": "99123"}
        self.env_index = {1: "production"}

    def _run(self, rules):
        return self.m.migrate_issue_alerts(rules, "dest-org", self.project_slugs,
                                           self.team_map, self.env_index)

    def _only_payload(self):
        self.assertEqual(len(_POSTS), 1, "expected exactly one POST")
        return _POSTS[0]

    # ---- happy paths ----
    def test_no_owner_defaults_to_issue_owners(self):
        migrated, failed = self._run([make_rule(owner_team=None)])
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actions"], [{
            "id": "sentry.mail.actions.NotifyEmailAction",
            "targetType": "IssueOwners", "targetIdentifier": None,
            "fallthroughType": "ActiveMembers"}])
        self.assertNotIn("owner", payload)

    def test_owner_team_mapped_emails_team(self):
        migrated, failed = self._run([make_rule(owner_team=50)])
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actions"][0]["targetType"], "Team")
        self.assertEqual(payload["actions"][0]["targetIdentifier"], "99123")
        self.assertEqual(payload["owner"], "team:99123")

    def test_owner_team_unmapped_falls_back(self):
        # owner team present in export but no mapping -> IssueOwners fallback, no owner
        migrated, failed = self._run([make_rule(owner_team=777)])
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actions"][0]["targetType"], "IssueOwners")
        self.assertNotIn("owner", payload)

    def test_conditions_and_filters_preserved(self):
        conds = [{"id": "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition"}]
        filts = [{"id": "sentry.rules.filters.age_comparison.AgeComparisonFilter",
                  "comparison_type": "older", "value": 10, "time": "minute"}]
        self._run([make_rule(conditions=conds, filters=filts)])
        payload = self._only_payload()["json"]
        self.assertEqual(payload["conditions"], conds)
        self.assertEqual(payload["filters"], filts)

    def test_match_modes_and_frequency(self):
        self._run([make_rule(action_match="all", filter_match="none", frequency=60)])
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actionMatch"], "all")
        self.assertEqual(payload["filterMatch"], "none")
        self.assertEqual(payload["frequency"], 60)

    def test_frequency_defaults_to_30(self):
        self._run([make_rule(frequency=None)])
        self.assertEqual(self._only_payload()["json"]["frequency"], 30)

    def test_environment_mapped_and_null(self):
        self._run([make_rule(pk=1, environment_id=1)])
        self.assertEqual(self._only_payload()["json"]["environment"], "production")
        _POSTS.clear()
        self._run([make_rule(pk=2, environment_id=None)])
        self.assertIsNone(self._only_payload()["json"]["environment"])

    def test_url_is_project_rules_endpoint(self):
        self._run([make_rule(project=3)])
        self.assertEqual(self._only_payload()["url"],
                         "https://sentry.io/api/0/projects/dest-org/payments-api/rules/")

    def test_name_from_label(self):
        self._run([make_rule(label="Send a notification for high priority issues")])
        self.assertEqual(self._only_payload()["json"]["name"],
                         "Send a notification for high priority issues")

    # ---- error handling: no POST, recorded as failed ----
    def test_missing_project_is_failed_not_posted(self):
        migrated, failed = self._run([make_rule(project=999)])
        self.assertEqual((len(migrated), len(failed)), (0, 1))
        self.assertEqual(len(_POSTS), 0)

    def test_unparseable_data_is_failed_not_posted(self):
        bad = {"model": "sentry.rule", "pk": 5, "fields": {
            "project": 2, "environment_id": None, "label": "bad",
            "owner_team": None, "data": "{not valid json"}}
        migrated, failed = self._run([bad])
        self.assertEqual((len(migrated), len(failed)), (0, 1))
        self.assertEqual(len(_POSTS), 0)

    def test_non_rule_models_ignored(self):
        migrated, failed = self._run([{"model": "sentry.project", "pk": 2, "fields": {}}])
        self.assertEqual((len(migrated), len(failed), len(_POSTS)), (0, 0, 0))

    # ---- dry-run makes no network calls ----
    def test_dry_run_does_not_post(self):
        dry = mar.AlertRuleMigrator("tok", dry_run=True)
        migrated, failed = dry.migrate_issue_alerts(
            [make_rule(owner_team=50)], "dest-org", self.project_slugs,
            self.team_map, self.env_index)
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        self.assertEqual(len(_POSTS), 0)


class MigrateAlertRulesIntegrationTests(unittest.TestCase):
    """Exercises the top-level orchestration: results shape + --skip-issue-alerts."""

    def setUp(self):
        _POSTS.clear()
        self.tmp = tempfile.mkdtemp()
        # export: one project + one issue alert, no metric alerts
        export = [
            {"model": "sentry.project", "pk": 2, "fields": {"slug": "checkout-service"}},
            make_rule(project=2, owner_team=50),
        ]
        self.export_file = os.path.join(self.tmp, "export.json")
        with open(self.export_file, "w") as f:
            json.dump(export, f)
        self.map_file = os.path.join(self.tmp, "map.json")
        with open(self.map_file, "w") as f:
            json.dump({"team_id_mappings": [{"old_pk": 50, "new_id": "99123"}]}, f)

    def test_results_shape_and_counts(self):
        m = mar.AlertRuleMigrator("tok", dry_run=False)
        res = m.migrate_alert_rules(self.export_file, "dest-org", self.map_file)
        self.assertEqual(set(res.keys()), {"metric", "issue"})
        self.assertEqual(len(res["metric"]["migrated"]), 0)
        self.assertEqual(len(res["issue"]["migrated"]), 1)
        self.assertEqual(len(_POSTS), 1)

    def test_skip_issue_alerts_flag(self):
        m = mar.AlertRuleMigrator("tok", dry_run=False)
        res = m.migrate_alert_rules(self.export_file, "dest-org", self.map_file,
                                    migrate_issue=False)
        self.assertEqual(len(res["issue"]["migrated"]), 0)
        self.assertEqual(len(_POSTS), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
