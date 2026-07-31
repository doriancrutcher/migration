"""Unit tests for dashboard migration in dashboards/migrate_dashboards.py.

Hermetic: `requests` is stubbed before importing the module, so these run with plain
`python3` -- no network, no third-party deps.

Run:  python3 -m unittest discover -s tests
  or: python3 tests/test_dashboards.py
"""
import logging
import os
import sys
import types
import unittest

logging.disable(logging.CRITICAL)

# --- stub `requests` before importing the module under test ---
_POSTS = []
_GET_QUEUE = []          # queued _FakeResponse objects returned by requests.get
_POST_BEHAVIOR = {"raise": False, "text": "boom"}


class _FakeRequestException(Exception):
    pass


class _FakeResponse:
    def __init__(self, payload, headers=None, raise_exc=False, text=""):
        self._payload = payload
        self.headers = headers or {}
        self._raise = raise_exc
        self.text = text

    def raise_for_status(self):
        if self._raise:
            e = _FakeRequestException("HTTP error")
            e.response = types.SimpleNamespace(text=self.text)
            raise e

    def json(self):
        return self._payload


def _fake_post(url, headers=None, json=None):
    _POSTS.append({"url": url, "json": json})
    if _POST_BEHAVIOR.get("raise"):
        return _FakeResponse({}, raise_exc=True, text=_POST_BEHAVIOR.get("text", "boom"))
    return _FakeResponse({"id": "new-1", "title": (json or {}).get("title"),
                          "widgets": (json or {}).get("widgets", [])})


def _fake_get(url, headers=None, params=None):
    if _GET_QUEUE:
        return _GET_QUEUE.pop(0)
    return _FakeResponse([], headers={})


_fake_requests = types.ModuleType("requests")
_fake_requests.post = _fake_post
_fake_requests.get = _fake_get
_fake_requests.exceptions = types.SimpleNamespace(RequestException=_FakeRequestException)
sys.modules["requests"] = _fake_requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboards"))
import migrate_dashboards as md  # noqa: E402


def widget(title="W", display="line", wtype="discover", conditions="", fields=None,
           aggregates=None, columns=None, extra=None):
    q = {
        "id": "99", "name": "", "fields": fields if fields is not None else ["count()"],
        "aggregates": aggregates if aggregates is not None else ["count()"],
        "columns": columns if columns is not None else [], "fieldAliases": [],
        "conditions": conditions, "orderby": "", "widgetId": "99",
        "onDemand": [], "isHidden": False, "selectedAggregate": None,
    }
    w = {"id": "1", "title": title, "displayType": display, "interval": "5m",
         "widgetType": wtype, "layout": None, "limit": None, "queries": [q],
         "datasetSource": "unknown", "dateCreated": "x"}
    if extra:
        w.update(extra)
    return w


class ProjectMapTests(unittest.TestCase):
    def setUp(self):
        self.m = md.DashboardMigrator("tok", dry_run=False)

    def test_maps_matched_by_name(self):
        src = [{"id": 10, "slug": "pay-api", "name": "Payments API"},
               {"id": 11, "slug": "web", "name": "Web Dashboard"}]
        dst = [{"id": 500, "slug": "payments-api", "name": "payments api"},  # case-insensitive
               {"id": 501, "slug": "web-dashboard", "name": "Web Dashboard"}]
        id_map, slug_map = self.m.build_project_maps(src, dst)
        self.assertEqual(id_map, {"10": "500", "11": "501"})
        self.assertEqual(slug_map, {"pay-api": "payments-api", "web": "web-dashboard"})

    def test_unmatched_source_project_omitted(self):
        src = [{"id": 10, "slug": "orphan", "name": "No Match"}]
        id_map, slug_map = self.m.build_project_maps(src, [])
        self.assertEqual(id_map, {})
        self.assertEqual(slug_map, {})


class ConditionRemapTests(unittest.TestCase):
    def setUp(self):
        self.m = md.DashboardMigrator("tok", dry_run=False)

    def test_slug_token_rewritten(self):
        new, unmapped = self.m.remap_conditions("event.type:error project:web", {"web": "web-dashboard"}, {})
        self.assertEqual(new, "event.type:error project:web-dashboard")
        self.assertEqual(unmapped, [])

    def test_id_token_rewritten(self):
        new, unmapped = self.m.remap_conditions("project.id:10", {}, {"10": "500"})
        self.assertEqual(new, "project.id:500")
        self.assertEqual(unmapped, [])

    def test_unmapped_recorded_and_left_intact(self):
        new, unmapped = self.m.remap_conditions("project:ghost", {}, {})
        self.assertEqual(new, "project:ghost")
        self.assertEqual(unmapped, ["project:ghost"])

    def test_empty_conditions(self):
        new, unmapped = self.m.remap_conditions("", {"a": "b"}, {})
        self.assertEqual(new, "")
        self.assertEqual(unmapped, [])


class WidgetPayloadTests(unittest.TestCase):
    def setUp(self):
        self.m = md.DashboardMigrator("tok", dry_run=False)

    def test_forwarded_fields_only(self):
        w = widget(title="Errors", conditions="project:web", fields=["count()"])
        payload, _, _ = self.m.build_widget_payload(w, {"web": "web-dashboard"}, {})
        # forwarded widget fields
        self.assertEqual(set(payload.keys()), {"title", "displayType", "interval",
                                               "widgetType", "layout", "limit", "queries"})
        # query keeps only whitelisted keys, drops id/widgetId/onDemand/etc.
        q = payload["queries"][0]
        self.assertEqual(set(q.keys()), set(md.QUERY_FIELDS))
        self.assertNotIn("onDemand", q)
        self.assertEqual(q["conditions"], "project:web-dashboard")

    def test_fields_aggregates_columns_preserved(self):
        w = widget(fields=["assignee", "title"], aggregates=[], columns=["assignee", "title"])
        payload, _, _ = self.m.build_widget_payload(w, {}, {})
        q = payload["queries"][0]
        self.assertEqual(q["fields"], ["assignee", "title"])
        self.assertEqual(q["columns"], ["assignee", "title"])
        self.assertEqual(q["aggregates"], [])


class WidgetTypeTranslationTests(unittest.TestCase):
    def setUp(self):
        self.m = md.DashboardMigrator("tok", dry_run=False)

    def test_discover_error_widget_becomes_error_events(self):
        w = widget(wtype="discover", conditions="event.type:error", fields=["count()"])
        payload, _, tr = self.m.build_widget_payload(w, {}, {})
        self.assertEqual(payload["widgetType"], "error-events")
        self.assertEqual(tr, {"widget": "W", "from": "discover", "to": "error-events"})

    def test_discover_transaction_by_condition_becomes_spans(self):
        w = widget(wtype="discover", conditions="event.type:transaction", fields=["count()"])
        payload, _, tr = self.m.build_widget_payload(w, {}, {})
        self.assertEqual(payload["widgetType"], "spans")
        # event.type:transaction rewritten to the spans-dataset filter
        self.assertEqual(payload["queries"][0]["conditions"], "is_transaction:true")

    def test_discover_transaction_by_field_becomes_spans_and_rewrites_field(self):
        w = widget(wtype="discover", conditions="", fields=["p95(transaction.duration)"],
                   aggregates=["p95(transaction.duration)"])
        payload, _, tr = self.m.build_widget_payload(w, {}, {})
        self.assertEqual(payload["widgetType"], "spans")
        q = payload["queries"][0]
        self.assertEqual(q["fields"], ["p95(span.duration)"])
        self.assertEqual(q["aggregates"], ["p95(span.duration)"])
        self.assertIn("is_transaction:true", q["conditions"])

    def test_issue_type_passthrough_no_translation(self):
        w = widget(wtype="issue", conditions="is:unresolved",
                   fields=["assignee"], aggregates=[], columns=["assignee"])
        payload, _, tr = self.m.build_widget_payload(w, {}, {})
        self.assertEqual(payload["widgetType"], "issue")
        self.assertIsNone(tr)


class BuildPayloadTests(unittest.TestCase):
    def setUp(self):
        self.m = md.DashboardMigrator("tok", dry_run=False)

    def test_projects_remapped(self):
        dash = {"title": "D", "widgets": [widget()], "projects": [10, 11]}
        payload, unmapped, _ = self.m.build_payload(dash, {"10": "500", "11": "501"}, {})
        self.assertEqual(payload["projects"], [500, 501])
        self.assertEqual(unmapped, [])
        self.assertEqual(payload["title"], "D")

    def test_all_projects_passthrough(self):
        dash = {"title": "D", "widgets": [], "projects": [-1]}
        payload, _, _ = self.m.build_payload(dash, {}, {})
        self.assertEqual(payload["projects"], [-1])

    def test_unmapped_dashboard_project_recorded(self):
        dash = {"title": "D", "widgets": [], "projects": [999]}
        payload, unmapped, _ = self.m.build_payload(dash, {}, {})
        self.assertEqual(payload["projects"], [])
        self.assertIn("dashboard-project-id:999", unmapped)

    def test_filters_passthrough(self):
        dash = {"title": "D", "widgets": [], "projects": [], "filters": {"environment": ["prod"]}}
        payload, _, _ = self.m.build_payload(dash, {}, {})
        self.assertEqual(payload["filters"], {"environment": ["prod"]})

    def test_translations_aggregated(self):
        dash = {"title": "D", "projects": [], "widgets": [
            widget(title="E", wtype="discover", conditions="event.type:error"),
            widget(title="T", wtype="discover", conditions="event.type:transaction"),
        ]}
        _, _, translations = self.m.build_payload(dash, {}, {})
        self.assertEqual(len(translations), 2)
        self.assertEqual({t["to"] for t in translations}, {"error-events", "spans"})


class IsCustomTests(unittest.TestCase):
    def test_numeric_id_is_custom(self):
        self.assertTrue(md.is_custom({"id": "1"}))

    def test_prebuilt_string_id_not_custom(self):
        self.assertFalse(md.is_custom({"id": "default-overview"}))


class CreateAndVerifyTests(unittest.TestCase):
    def setUp(self):
        _POSTS.clear()
        _GET_QUEUE.clear()
        _POST_BEHAVIOR["raise"] = False

    def test_dry_run_no_post(self):
        m = md.DashboardMigrator("tok", dry_run=True)
        out = m.create_dashboard("org", {"title": "D", "widgets": []})
        self.assertEqual(out, {"dry_run": True})
        self.assertEqual(_POSTS, [])

    def test_live_post_records_and_returns(self):
        m = md.DashboardMigrator("tok", dry_run=False)
        out = m.create_dashboard("org", {"title": "D", "widgets": [widget()]})
        self.assertEqual(len(_POSTS), 1)
        self.assertTrue(_POSTS[0]["url"].endswith("/organizations/org/dashboards/"))
        self.assertEqual(out["id"], "new-1")

    def test_post_failure_raises_runtimeerror_with_body(self):
        _POST_BEHAVIOR["raise"] = True
        _POST_BEHAVIOR["text"] = '{"widgetType":["invalid dataset"]}'
        m = md.DashboardMigrator("tok", dry_run=False)
        with self.assertRaises(RuntimeError) as ctx:
            m.create_dashboard("org", {"title": "D", "widgets": []})
        self.assertIn("invalid dataset", str(ctx.exception))

    def test_verify_detects_widget_count_mismatch(self):
        m = md.DashboardMigrator("tok", dry_run=False)
        _GET_QUEUE.append(_FakeResponse({"widgets": [{"title": "A"}]}))
        issues = m.verify("org", "1", {"widgets": [{"title": "A"}, {"title": "B"}]})
        self.assertIn("widget_count", issues)

    def test_verify_passes_on_match(self):
        m = md.DashboardMigrator("tok", dry_run=False)
        _GET_QUEUE.append(_FakeResponse({"widgets": [{"title": "A"}, {"title": "B"}]}))
        issues = m.verify("org", "1", {"widgets": [{"title": "B"}, {"title": "A"}]})
        self.assertEqual(issues, {})


if __name__ == "__main__":
    unittest.main()
