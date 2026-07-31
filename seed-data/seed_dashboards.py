"""Seed a representative custom dashboard into the self-hosted org for the migration test.

Creates one dashboard with a mix of widget types (big number, time series, issue table,
transaction widget) so the dashboards migration can be proven end-to-end. Idempotent by title:
re-running skips a dashboard that already exists.

Run via:
  docker compose run --rm -T web django shell < seed-data/seed_dashboards.py
"""
from sentry.models.organization import Organization
from sentry.models.organizationmember import OrganizationMember
from sentry.models.dashboard import Dashboard
from sentry.models.dashboard_widget import (
    DashboardWidget,
    DashboardWidgetQuery,
    DashboardWidgetDisplayTypes,
    DashboardWidgetTypes,
)

ORG_SLUG = "migration-test-org"
DASHBOARD_TITLE = "Migration Test Dashboard"

org = Organization.objects.get(slug=ORG_SLUG)
print(f"Using org: {org.slug} (id={org.id})")

# A user to own the dashboard (created_by is required).
owner_om = (
    OrganizationMember.objects.filter(organization=org, role="owner").first()
    or OrganizationMember.objects.filter(organization=org).exclude(user_id=None).first()
)
created_by_id = owner_om.user_id if owner_om else None
print(f"created_by_id={created_by_id}")

dashboard, created = Dashboard.objects.get_or_create(
    organization=org,
    title=DASHBOARD_TITLE,
    defaults={"created_by_id": created_by_id},
)
print(f"dashboard '{DASHBOARD_TITLE}': {'created' if created else 'exists'} (id={dashboard.id})")

# Widget specs: (title, display_type, widget_type, query dict)
WIDGETS = [
    {
        "title": "Total Errors",
        "display_type": DashboardWidgetDisplayTypes.BIG_NUMBER,
        "widget_type": DashboardWidgetTypes.DISCOVER,
        "query": {
            "name": "",
            "fields": ["count()"],
            "aggregates": ["count()"],
            "columns": [],
            "conditions": "event.type:error",
            "orderby": "",
        },
    },
    {
        "title": "Errors Over Time",
        "display_type": DashboardWidgetDisplayTypes.LINE_CHART,
        "widget_type": DashboardWidgetTypes.DISCOVER,
        "query": {
            "name": "Errors",
            "fields": ["count()"],
            "aggregates": ["count()"],
            "columns": [],
            "conditions": "event.type:error",
            "orderby": "",
        },
    },
    {
        "title": "Unresolved Issues",
        "display_type": DashboardWidgetDisplayTypes.TABLE,
        "widget_type": DashboardWidgetTypes.ISSUE,
        "query": {
            "name": "",
            "fields": ["assignee", "title", "status"],
            "aggregates": [],
            "columns": ["assignee", "title", "status"],
            "conditions": "is:unresolved",
            "orderby": "",
        },
    },
    {
        "title": "p95 Transaction Duration",
        "display_type": DashboardWidgetDisplayTypes.LINE_CHART,
        "widget_type": DashboardWidgetTypes.DISCOVER,
        "query": {
            "name": "p95",
            "fields": ["p95(transaction.duration)"],
            "aggregates": ["p95(transaction.duration)"],
            "columns": [],
            "conditions": "event.type:transaction",
            "orderby": "",
        },
    },
]

if not created and DashboardWidget.objects.filter(dashboard=dashboard).exists():
    print("widgets already present; skipping widget creation")
else:
    for order, spec in enumerate(WIDGETS):
        try:
            widget = DashboardWidget.objects.create(
                dashboard=dashboard,
                order=order,
                title=spec["title"],
                display_type=spec["display_type"],
                widget_type=spec["widget_type"],
                interval="5m",
            )
            q = spec["query"]
            DashboardWidgetQuery.objects.create(
                widget=widget,
                order=0,
                name=q["name"],
                fields=q["fields"],
                aggregates=q["aggregates"],
                columns=q["columns"],
                conditions=q["conditions"],
                orderby=q["orderby"],
            )
            print(f"  widget '{spec['title']}': created (id={widget.id})")
        except Exception as e:
            print(f"  widget '{spec['title']}': FAILED {e}")

print("--- summary ---")
print("dashboards:", Dashboard.objects.filter(organization=org).count())
print("widgets on this dashboard:", DashboardWidget.objects.filter(dashboard=dashboard).count())
