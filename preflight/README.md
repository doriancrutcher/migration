# Pre-flight: duplicates / collision report

`duplicates_report.py` — step 0 of the suite. Run it **before** any migration when you are consolidating
several self-hosted orgs into one SaaS org. It reads one JSON export per org and reports the overlaps that
would break a merged migration, so you can resolve them (rename / merge / drop) up front.

- **Source:** JSON exports only (offline; never calls SaaS). One export file == one org. See DECISIONS.md D7.
- **Dependencies:** none (Python 3 standard library only).

## Run

```bash
python3 preflight/duplicates_report.py org1.json org2.json [org3.json ...] \
    [--label PATH=Name] [--similarity 0.6] [--out duplicate_report.json] [--html [duplicate_report.html]]
```

- `--html` also writes a **self-contained** `duplicate_report.html` (inline CSS, no server/dependencies,
  opens offline in any browser) — a readable, shareable view with a severity legend.
- `--label PATH=Name` overrides an org's display name; `--similarity` (0..1) tunes the "similar names" match.

## What it reports

Two severities:

- **Danger** — will break a merged migration; resolve first. Makes the tool exit non-zero.
  - **Project collisions**: detected on the **derived slug** (`slugify(name)`) — SaaS creates projects by
    name and derives the slug, so names that map to the same slug clash (also catches different names that
    slugify to the same value, e.g. `Payments API` vs `Payments-API`).
  - **Team slug collisions**: a team slug must be unique in the merged org.
- **Info** — won't block, but a human should review.
  - **Team name collisions** with a per-org **membership diff** (same team name, different rosters).
  - **Similar org names**.

## Outputs

- `duplicate_report.json` (always) and `duplicate_report.html` (with `--html`) — both gitignored.
- Console summary + non-zero exit when any Danger collision exists.
