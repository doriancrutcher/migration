"""Seed representative test data into the self-hosted org for the migration test.

Idempotent: safe to re-run. Run via:
  docker compose run --rm -T web django shell < seed_selfhosted.py
"""
from sentry.models.organization import Organization
from sentry.models.team import Team
from sentry.models.project import Project
from sentry.models.organizationmember import OrganizationMember
from sentry.models.organizationmemberteam import OrganizationMemberTeam
from sentry.users.models.user import User
from sentry.incidents.logic import create_alert_rule
from sentry.incidents.models.alert_rule import AlertRule, AlertRuleThresholdType
from sentry.snuba.dataset import Dataset
from sentry.snuba.models import SnubaQueryEventType

ORG_SLUG = "migration-test-org"
org = Organization.objects.get(slug=ORG_SLUG)
print(f"Using org: {org.slug} (id={org.id})")

TEAMS = [
    ("backend", "Backend"),
    ("frontend", "Frontend"),
    ("platform", "Platform"),
]
PROJECTS = [
    ("checkout-service", "Checkout Service", "python", "backend"),
    ("payments-api", "Payments API", "node", "backend"),
    ("web-dashboard", "Web Dashboard", "javascript-react", "frontend"),
    ("mobile-app", "Mobile App", "apple-ios", "frontend"),
    ("data-pipeline", "Data Pipeline", "python", "platform"),
]
MEMBERS = [
    ("alice@example.com", ["backend"]),
    ("bob@example.com", ["frontend", "platform"]),
    ("carol@example.com", ["backend", "frontend"]),
]

teams = {}
for slug, name in TEAMS:
    t, created = Team.objects.get_or_create(organization=org, slug=slug, defaults={"name": name})
    teams[slug] = t
    print(f"team {slug}: {'created' if created else 'exists'} (id={t.id})")

projects = {}
for slug, name, platform, team_slug in PROJECTS:
    p, created = Project.objects.get_or_create(
        organization=org, slug=slug, defaults={"name": name, "platform": platform}
    )
    p.add_team(teams[team_slug])
    projects[slug] = (p, team_slug)
    print(f"project {slug}: {'created' if created else 'exists'} (id={p.id}) -> team {team_slug}")

for email, team_slugs in MEMBERS:
    user, created = User.objects.get_or_create(
        username=email, defaults={"email": email, "is_active": True}
    )
    if created:
        user.set_unusable_password()
        user.save()
    om, _ = OrganizationMember.objects.get_or_create(
        organization=org, user_id=user.id, defaults={"role": "member"}
    )
    for ts in team_slugs:
        OrganizationMemberTeam.objects.get_or_create(organizationmember=om, team=teams[ts])
    print(f"member {email}: {'created' if created else 'exists'} (user_id={user.id}) -> teams {team_slugs}")

# one metric alert rule per project, owned by the project's team
owner_for = None
try:
    from sentry.types.actor import Actor
    def owner_for(team):
        return Actor.from_orm_team(team)
except Exception as e:
    print(f"Actor import failed, alert rules will have no owner: {e}")
    def owner_for(team):
        return None

for slug, (p, team_slug) in projects.items():
    name = f"{slug} error spike"
    if AlertRule.objects.filter(organization=org, name=name).exists():
        print(f"alert rule '{name}': exists")
        continue
    try:
        owner = owner_for(teams[team_slug])
    except Exception as e:
        print(f"owner build failed for {team_slug}: {e}")
        owner = None
    try:
        rule = create_alert_rule(
            org,
            [p],
            name,
            query="",
            aggregate="count()",
            time_window=60,
            threshold_type=AlertRuleThresholdType.ABOVE,
            threshold_period=1,
            owner=owner,
            dataset=Dataset.Events,
            event_types=[SnubaQueryEventType.EventType.ERROR],
        )
        try:
            from sentry.incidents.logic import create_alert_rule_trigger
            create_alert_rule_trigger(rule, "critical", 100)
        except Exception as e:
            print(f"  (trigger add failed for '{name}': {e})")
        print(f"alert rule '{name}': created (id={rule.id}, owner={'team:'+team_slug if owner else 'none'})")
    except Exception as e:
        print(f"alert rule '{name}': FAILED {e}")

print("--- summary ---")
print("teams:", Team.objects.filter(organization=org).count())
print("projects:", Project.objects.filter(organization=org).count())
print("members:", OrganizationMember.objects.filter(organization=org).count())
print("alert rules:", AlertRule.objects.filter(organization=org).count())
