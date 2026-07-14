# Seed data (synthetic test fixtures)

Scripts and sample exports used to exercise the toolkit against **throwaway, synthetic** self-hosted data.
Everything here uses fake `@example.com` personas and non-real orgs — no customer or production data.

## Scripts

Run against a **local/test** self-hosted Sentry instance via its Django shell (they create orgs, teams,
projects, and members). Never run these against a real instance.

| Script | Purpose |
|--------|---------|
| `seed_selfhosted.py` | Seed a single self-hosted org with projects/teams/members/alerts for core-migration testing |
| `seed_multi_org.py` | Seed multiple orgs with deliberately overlapping projects/teams/members to exercise the pre-flight duplicates report |

Example (local Docker):

```bash
docker compose run --rm -T web django shell < seed_multi_org.py
```

## example-exports/

Pre-generated relocation exports produced from the multi-org seed, plus a sample pre-flight report — handy
for reviewing the [duplicates report](../preflight/) without standing up an instance:

| File | What it is |
|------|-----------|
| `dor-org1.json`, `dor-org2.json`, `dor-org3.json` | Synthetic per-org exports (the report's input) |
| `duplicates-report-sample.html` | Example rendered duplicates report (open in a browser) |
| `duplicates-report-sample.json` | Example machine-readable duplicates report |

Regenerate the report from the sample exports:

```bash
python3 preflight/duplicates_report.py \
  seed-data/example-exports/dor-org1.json \
  seed-data/example-exports/dor-org2.json \
  seed-data/example-exports/dor-org3.json --html
```

Note: these synthetic exports contain `password` (Django "unusable" placeholders) and randomly generated
`secret_key` fields. They are fake and safe to publish, but illustrate why a **real** export must be treated
as sensitive (see the export-sensitivity notes in the reports).
