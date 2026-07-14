# Phase 1: Stand Up Self-Hosted Sentry v25 Locally

**Status:** Complete

## Summary

To test the customer's migration scripts (Phase 2), we needed a local self-hosted Sentry instance to act as the **source** environment. This instance is a throwaway stand-in for the customer's real v25 self-hosted deployment — it does not affect the customer's actual instances in any way. To be clear: this project is about *migrating* data to SaaS, not *upgrading* the customer's self-hosted instances. The customer's real instances stay pinned wherever they are.

Test machine: Apple M-series Mac (macOS), using Docker Desktop and `git`.

## 1. Target Version: 25.6.2

We used **25.6.2** rather than 25.5.0, because native `linux/arm64` images for all services (snuba, vroom, taskbroker) weren't complete until 25.6.2. Note: the customer's exact v25.x version isn't confirmed — 25.6.2 is a representative stand-in for testing purposes.

## 2. Docker Configuration

### 2.1 Memory: Allocate at Least 16 GB to Docker

The installer (`install.sh`) hard-fails under ~14 GB (`FAIL: Required minimum RAM available to Docker is 14000 MB`). In Docker Desktop, go to **Settings → Resources** and set Memory to **16 GB**, CPUs to **≥ 4**, Swap to 2–4 GB, and ensure at least 20 GB of free disk. Apply and restart, then confirm with:

```bash
docker info | grep "Total Memory"   # expect ~15.6 GiB
```

### 2.2 Apple Silicon: Don't Force a Platform (Biggest Gotcha)

Sentry 25.6.2+ ships native arm64 images — let Docker use them. **Do not** set `DOCKER_DEFAULT_PLATFORM` or `DOCKER_PLATFORM` to `linux/amd64`, and turn **off** Docker Desktop's "Use Rosetta for x86/amd64 emulation."

If `linux/amd64` is forced, the locally built `sentry-self-hosted-local` image builds as amd64 and runs under emulation. This causes the 60-container stack to OOM-kill consumers (`Restarting (137)`) and can crash Docker Desktop entirely — `web` never becomes healthy. The override lives in the **shell session** itself (not `~/.zshrc`), so it silently persists in that terminal and any tool that reuses it.

To keep the environment clean:

```bash
env | grep DOCKER_        # should print nothing platform-related
unset DOCKER_PLATFORM DOCKER_DEFAULT_PLATFORM
```

After any build, confirm the app image is arm64:

```bash
docker image inspect sentry-self-hosted-local:latest --format '{{.Architecture}}'   # must be: arm64
```

If it reports `amd64`: fix the environment variables, then run `docker compose down && docker compose build` from a clean shell.

## 3. Clone and Check Out 25.6.2

```bash
mkdir -p ~/sentry-migration-testing && cd ~/sentry-migration-testing
git clone https://github.com/getsentry/self-hosted.git
cd self-hosted
git checkout 25.6.2
```

## 4. Install

```bash
./install.sh
```

A few notes on prompts during install:

- **Beacon prompt** — answer `n` for a local test instance.
- **Image pulls/builds** — this is slow, roughly 15–30 minutes on the first run.
- **User creation — do this.** Enter an email and password, and answer **yes** to superuser. This becomes your only web UI login. Since this is a local/throwaway instance, the credentials don't need to be real (e.g., `dorian@local.test`).

## 5. Start Sentry

Run these one line at a time — don't paste inline `#` comments, since interactive zsh will error with `no such service: #`:

```bash
docker compose up -d --no-build
```

`--no-build` avoids rebuilding images that `install.sh` already built. (A rebuild in a "dirty" shell is exactly how the amd64 regression from §2.2 can sneak back in.) First boot takes about 1–3 minutes. Verify with:

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:9000/_health/   # expect 200
docker compose ps -q | wc -l                                                   # expect 60
```

Then open **http://127.0.0.1:9000**.

Lifecycle commands (all preserve data volumes):

| Action | Command |
|---|---|
| Start | `docker compose up -d --no-build` |
| Pause | `docker compose stop` |
| Remove containers | `docker compose down` |

## 6. Create the Org and Seed Source Data

1. Log in at http://127.0.0.1:9000 and create the organization **`migration-test-org`**. This exact slug is what the seeder script and export process expect.
2. Seed representative test data (teams, projects, members with memberships, and metric alert rules) using the idempotent seeder script — no manual clicking required:

   ```bash
   cd ~/sentry-migration-testing/self-hosted
   docker compose run --rm -T web django shell < "<project-root>/migration-testing/seed_selfhosted.py"
   ```

   It prints a summary confirming 4 teams, 6 projects, 4 members, and 5 alert rules. The script is safe to re-run.

> **Note:** Seeded projects and teams belong to teams your login isn't a member of, so they'll show up under **Settings → Teams/Projects** rather than the default dashboard. Hard-refresh the UI if a page looks stale.

Next step: Phase 2 (`phase-2-migration-scripts-results.md`) — export this data and migrate it to SaaS.

## 7. Gotchas Reference

- **amd64 emulation is the #1 crash cause.** If Docker Desktop keeps dying or `web` never becomes healthy, verify the app image is arm64 (§2.2). Symptom: consumers stuck in `Restarting (137)`.
- **Resource starvation is #2.** Recheck memory/CPU allocation (§2.1).
- **Don't run `docker compose restart` after editing config files** — re-run `./install.sh` instead.
- **Keep this instance pinned at 25.6.2** — no casual upgrades.
- **Port conflicts on 9000** — check with `lsof -i :9000`.
- **One org per instance.** Self-hosted defaults to a single org, so simulating multiple the customer instances means separate installs (different directories/ports), not multiple orgs in one instance.
