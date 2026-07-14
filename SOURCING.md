# Data sources: the export vs. the live API

The toolkit reads from **two independent sources**, and each migration step uses **exactly one** of them —
they are never mixed. This doc explains which step uses which, and how to produce the export (including on
managed/dedicated hosting). It expands on the "How the data flows" section of the [README](README.md).

## The two sources

1. **Relocation export (a static JSON file).** Produced once per source org with
   `sentry export organizations` (see Step 0 below). It is a Django `dumpdata`-style flat list of
   `{"model", "pk", "fields"}` objects. The **pre-flight** and **core** tools parse this file and recreate
   its objects in SaaS. These tools **never contact the self-hosted instance** — once you have the file you
   can run them fully offline from the source.

2. **Live self-hosted REST API (read-only).** Some settings the relocation export does not carry (org
   governance/privacy flags, per-project settings, data scrubbers). The **settings** tools read those from
   the running self-hosted instance via [`common/selfhosted_source.py`](common/selfhosted_source.py) and
   write them to SaaS. These tools **do not read the export file at all**.

Both sources ultimately write to SaaS using `SAAS_TOKEN`.

## Which step uses which source

| Step | Tool(s) | Source | Needs the export file | Needs live self-hosted API + `SH_TOKEN` |
|------|---------|--------|:---------------------:|:---------------------------------------:|
| 1 — pre-flight | `preflight/duplicates_report.py` | export | yes | no |
| 3 — core | `core/*.py` (projects, teams, members, membership, alerts) | export | yes | no |
| 4 — settings | `org-settings/`, `project-settings/`, `data-scrubbers/` | live API | no | yes |

Consequences:

- A **core-only** migration (Steps 1 + 3) needs **only the JSON file(s)** — no network path to the
  self-hosted instance, no `SH_TOKEN`.
- **Settings** (Step 4) need network reachability to the self-hosted API (`--source-url "$SRC_URL"`, direct
  or via VPN, valid TLS) and an `SH_TOKEN` minted on that instance. They do **not** need the export.

## Producing the export (Step 0)

All variants emit the same relocation JSON. Run once per source org with
`--filter-org-slugs <slug>` to get `org1.json`, `org2.json`, …

### a) Shell/CLI on the self-hosted host

```bash
sentry export organizations export.json --filter-org-slugs "$SRC_ORG" --no-prompt
```

### b) Local Docker Compose

Mount a host dir so the file lands outside the container:

```bash
docker compose run --rm -T -v "$PWD:/export" \
  web export organizations /export/export.json --filter-org-slugs "$SRC_ORG" --no-prompt
```

### c) Managed / dedicated hosting

When you don't have shell access to the instance (a managed or dedicated host), the provider/admin produces
the file and hands it to you. What they need to run it, and what you need to receive:

- **Access:** a superuser/admin who can run Sentry management commands in the app environment (the same
  image/venv as the `web` service), where the `sentry` CLI is available.
- **Command:** `sentry export organizations export.json --filter-org-slugs <org-slug> --no-prompt`
  (one run per source org). For containerized managed hosts, the Docker Compose variant (b) works the same
  way if they can mount a directory.
- **Version:** export from a supported relocation version. These tools were validated against **v25.6.2**;
  confirm the source version if it differs.
- **No inbound network required for this step.** Unlike Step 4 settings, producing/consuming the export
  needs no live connection to the instance — you only need the resulting file. (Settings migration, if in
  scope, still requires API reachability + `SH_TOKEN` separately.)
- **Hand-off:** the export is a plain-text JSON file. Transfer it **securely** (encrypted channel / secure
  file share, not cleartext email) and delete it when the migration is done — see the sensitivity note.

## Sensitivity of the export file

The export contains sensitive fields such as DSN secret keys, PBKDF2 password hashes, validation hashes, and
member email addresses. **The migration tools do not read or transmit those fields** (they use only the
project/team/member/alert fields needed to recreate objects), but the file itself must be handled
confidentially: store it access-controlled, transfer it over an encrypted channel, and remove it after use.
