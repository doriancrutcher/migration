"""Seed three similarly-named self-hosted orgs to exercise the duplicates report.

Idempotent: safe to re-run. Run via:
  docker compose run --rm -T web django shell < seed_multi_org.py

Planted collisions (what duplicates_report.py should catch):
  - Project NAME collision (HARD): "Payments API" and "Checkout Service" in Dor-Org1 AND Dor-Org2.
  - Team SLUG/NAME collision with DIFFERENT rosters: team "fe"/"Frontend" in Dor-Org1
    (dorian/derek/daniel) vs Dor-Org3 (mandy/mikey/mitch).
  - Similar org names: dor-org1 / dor-org2 / dor-org3.
"""
from sentry.models.organization import Organization
from sentry.models.team import Team
from sentry.models.project import Project
from sentry.models.organizationmember import OrganizationMember
from sentry.models.organizationmemberteam import OrganizationMemberTeam
from sentry.users.models.user import User

# org_slug -> {name, teams: {slug: (name, [emails])}, projects: [(slug, name, platform, team_slug)]}
ORGS = {
    "dor-org1": {
        "name": "Dor-Org1",
        "teams": {
            "fe": ("Frontend", ["dorian@example.com", "derek@example.com", "daniel@example.com",
                                "shared@example.com"]),
            "be": ("Backend", ["dorian@example.com"]),
            # collides with org3 on BOTH slug + name (different roster)
            "qa": ("QA", ["derek@example.com"]),
            # collides with org3 on NAME only (org3 uses slug 'platform-team')
            "platform": ("Platform", ["dorian@example.com", "daniel@example.com"]),
            # unique to org1
            "design": ("Design", ["daniel@example.com"]),
            # unique to org1 (with a brand-new person)
            "sre": ("SRE", ["dorian@example.com", "nina@example.com"]),
        },
        "projects": [
            ("payments-api", "Payments API", "node", "be"),          # DUP name with org2 (+ org3 now)
            ("checkout-service", "Checkout Service", "python", "be"),  # DUP name with org2
            ("web-dashboard", "Web Dashboard", "javascript-react", "fe"),  # collides with org2 now
            ("notifications", "Notifications", "python", "sre"),      # unique to org1
        ],
    },
    "dor-org2": {
        "name": "Dor-Org2",
        "teams": {
            "data": ("Data", ["sam@example.com"]),
            # collides with org1 'be' (slug + name), different roster
            "be": ("Backend", ["sam@example.com"]),
        },
        "projects": [
            ("payments-api", "Payments API", "node", "data"),          # DUP name with org1
            ("checkout-service", "Checkout Service", "python", "data"),  # DUP name with org1
            ("analytics", "Analytics", "python", "data"),               # unique
            ("web-dashboard", "Web Dashboard", "javascript", "be"),     # collides with org1 web-dashboard
            ("billing", "Billing", "python", "be"),                     # unique
        ],
    },
    "dor-org3": {
        "name": "Dor-Org3",
        "teams": {
            # same slug/name as org1's "fe"; mostly different roster + one shared member
            "fe": ("Frontend", ["mandy@example.com", "mikey@example.com", "mitch@example.com",
                                "shared@example.com"]),
            # collides with org1 on BOTH slug + name (different roster)
            "qa": ("QA", ["mandy@example.com"]),
            # NAME 'Platform' collides with org1, but slug differs -> name-only collision
            "platform-team": ("Platform", ["mitch@example.com", "mikey@example.com"]),
            # unique to org3
            "data": ("Data", ["mandy@example.com"]),
            # collides with org1 'design' (slug + name), different roster
            "design": ("Design", ["mitch@example.com"]),
        },
        "projects": [
            ("internal-tools", "Internal Tools", "python", "fe"),       # unique
            ("payments-api", "Payments API", "node", "fe"),             # payments-api now spans 3 orgs
        ],
    },
}


def get_user(email):
    user, created = User.objects.get_or_create(
        username=email, defaults={"email": email, "is_active": True}
    )
    if created:
        user.set_unusable_password()
        user.save()
    return user


for org_slug, spec in ORGS.items():
    org, created = Organization.objects.get_or_create(slug=org_slug, defaults={"name": spec["name"]})
    print(f"\norg {org_slug}: {'created' if created else 'exists'} (id={org.id})")

    teams = {}
    for tslug, (tname, _emails) in spec["teams"].items():
        t, c = Team.objects.get_or_create(organization=org, slug=tslug, defaults={"name": tname})
        teams[tslug] = t
        print(f"  team {tslug}: {'created' if c else 'exists'} (id={t.id})")

    for pslug, pname, platform, team_slug in spec["projects"]:
        p, c = Project.objects.get_or_create(
            organization=org, slug=pslug, defaults={"name": pname, "platform": platform}
        )
        p.add_team(teams[team_slug])
        print(f"  project {pslug}: {'created' if c else 'exists'} (id={p.id}) -> team {team_slug}")

    for tslug, (_tname, emails) in spec["teams"].items():
        for email in emails:
            user = get_user(email)
            om, _ = OrganizationMember.objects.get_or_create(
                organization=org, user_id=user.id, defaults={"role": "member"}
            )
            OrganizationMemberTeam.objects.get_or_create(organizationmember=om, team=teams[tslug])
            print(f"  member {email} -> team {tslug}")

print("\n--- summary ---")
for org_slug in ORGS:
    org = Organization.objects.get(slug=org_slug)
    print(
        f"{org_slug}: teams={Team.objects.filter(organization=org).count()} "
        f"projects={Project.objects.filter(organization=org).count()} "
        f"members={OrganizationMember.objects.filter(organization=org).count()}"
    )
