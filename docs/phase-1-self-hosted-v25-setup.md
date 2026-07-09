# Phase 1: Stand Up Self-Hosted Sentry v25 Locally

Goal: run a local self-hosted Sentry instance to act as the **source** for testing the migration
scripts (Phase 2). This is a throwaway stand-in — migrating ≠ upgrading; NVIDIA's real instances stay
pinned wherever they are.

Test machine used: Apple M-series Mac, macOS, Docker Desktop. Assumes Docker Desktop and `git` are installed.

## 1. Target version: 25.6.2

Use **25.6.2**, not 25.5.0 — native `linux/arm64` images for all services (snuba/vroom/taskbroker)
weren't complete until 25.6.2. (NVIDIA's exact v25.x isn't confirmed; 25.6.2 is a representative stand-in.)

## 2. Docker configuration

### 2.1 Memory: give Docker at least 16 GB

`install.sh` hard-fails under ~14 GB (`FAIL: Required minimum RAM available to Docker is 14000 MB`).
Docker Desktop → **Settings → Resources**: Memory **16 GB**, CPUs **≥ 4**, Swap 2–4 GB, Disk ≥ 20 GB free →
**Apply & Restart**. Confirm: `docker info | grep "Total Memory"` (~15.6GiB).

### 2.2 Apple Silicon — do NOT force a platform (biggest gotcha)

25.6.2+ has native arm64 images. Let Docker use them. **Do not set `DOCKER_DEFAULT_PLATFORM` /
`DOCKER_PLATFORM` to `linux/amd64`, and turn OFF Docker Desktop's "Use Rosetta for x86/amd64 emulation."**

If `linux/amd64` is forced, the locally built `sentry-self-hosted-local` image builds as amd64 and runs
under emulation → the 60-container stack OOM-kills consumers (`Restarting (137)`) and crashes Docker
Desktop; `web` never gets healthy. The override lives in the **shell session** (not `~/.zshrc`), so it
silently persists in that terminal and any tool reusing it. Keep clean:

```bash
env | grep DOCKER_        # should print nothing platform-related
unset DOCKER_PLATFORM DOCKER_DEFAULT_PLATFORM
```

After any build, the app image must be arm64:

```bash
docker image inspect sentry-self-hosted-local:latest --format '{{.Architecture}}'   # must be: arm64
```

If it says `amd64`: fix the env, then `docker compose down && docker compose build` from a clean shell.

## 3. Clone and check out 25.6.2

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

- **Beacon prompt** — answer `n` for a local test instance.
- **Image pulls/builds** — slow (~15–30 min first time).
- **User creation — do it.** Enter an email + password, answer **yes** to superuser. This is your only
  web-UI login. Local/throwaway, so credentials don't need to be real (e.g. `dorian@local.test`).

## 5. Start Sentry

Run one line at a time (don't paste inline `#` comments — interactive zsh errors with `no such service: #`):

```bash
docker compose up -d --no-build
```

`--no-build` avoids rebuilding images `install.sh` already built (a rebuild in a dirty shell is how the
amd64 regression sneaks back in). First boot takes ~1–3 min. Verify:

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:9000/_health/   # expect 200
docker compose ps -q | wc -l                                                   # expect 60
```

Then open http://127.0.0.1:9000. Lifecycle: `docker compose up -d --no-build` (start) /
`docker compose stop` (pause) / `docker compose down` (remove containers). All keep data volumes.

## 6. Create the org + seed source data

1. Log in at http://127.0.0.1:9000 and create the organization **`migration-test-org`**
   (this exact slug is what the seeder and export use).
2. Seed representative test data (teams, projects, members + memberships, metric alert rules) with the
   idempotent script — no manual clicking needed:

   ```bash
   cd ~/sentry-migration-testing/self-hosted
   docker compose run --rm -T web django shell < "$HOME/Documents/Claude/Projects/NVIDIA Migration Project/migration-testing/seed_selfhosted.py"
   ```

   It prints a summary (expect 4 teams, 6 projects, 4 members, 5 alert rules). Re-running is safe.

> Note: seeded projects/teams belong to teams your login isn't a member of, so they show under
> **Settings → Teams/Projects**, not the default dashboard. Hard-refresh the UI if a page looks stale.

Next: Phase 2 (`phase-2-migration-scripts-results.md`) — export this data and migrate it to SaaS.

## 7. Gotchas

- **amd64 emulation is the #1 crash cause** — if Docker Desktop keeps dying or `web` never gets healthy,
  verify the app image is arm64 (§2.2). Symptom: consumers in `Restarting (137)`.
- **Resource starvation is #2** — recheck memory/CPU (§2.1).
- **Don't `docker compose restart` after editing config files** — re-run `./install.sh`.
- **Keep this instance pinned at 25.6.2** — no casual upgrades.
- **Port conflict on 9000** — check `lsof -i :9000`.
- **One org per instance** — self-hosted defaults to a single org; simulating multiple NVIDIA instances
  means separate installs (different dirs/ports), not multiple orgs in one instance.
