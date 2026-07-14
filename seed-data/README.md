# Seed data (synthetic test scripts)

Scripts that populate a **throwaway, local/test** self-hosted Sentry instance with synthetic data so the
migration toolkit can be exercised end to end. Everything created uses fake `@example.com` personas and
non-real orgs — no customer or production data.

**Do not run these against a real or production instance.** They create orgs, teams, projects, members,
and alerts.

## Scripts

| Script | What it creates | Used to test |
|--------|-----------------|--------------|
| `seed_selfhosted.py` | A single self-hosted org with projects, teams, members, and metric alerts | The core migration (projects, teams & membership, alert rules) |
| `seed_multi_org.py` | Multiple orgs with **deliberately overlapping** project names, team slugs/names, and shared members | The pre-flight duplicates report (cross-org collision detection) |

## How to run

They run through the self-hosted Django shell. On a local Docker Compose stack:

```bash
# single org
docker compose run --rm -T web django shell < seed_selfhosted.py

# multiple overlapping orgs
docker compose run --rm -T web django shell < seed_multi_org.py
```

On a host with the `sentry` CLI available, `sentry django shell < seed_multi_org.py` works the same way.

After seeding, produce a relocation export per org (see the top-level [README](../README.md) Step 0) to
feed the migration tools / duplicates report.
