"""Migrate custom dashboards from self-hosted -> SaaS (greenfield).

Dashboards are NOT in the relocation export, so the source is the live self-hosted REST API
(same pattern as the settings tools). For each custom dashboard we read the full definition
(widgets + queries), remap project references to the destination org, and recreate it with a
single POST to `/organizations/{org}/dashboards/`.

Project matching (greenfield assumption, same as migrate_project_settings.py): source and
destination projects are paired by NAME (case-insensitive). We build both an id map
(source project id -> dest id) and a slug map (source slug -> dest slug) so we can rewrite
dashboard-level `projects` (numeric ids) and any `project:`/`project.id:` tokens inside widget
query conditions. Unmappable references are recorded (never silently dropped).

Prebuilt dashboards (non-numeric ids like `default-overview`) are skipped -- SaaS already ships
its own. Idempotency: a dashboard whose title already exists in the destination org is skipped.

Usage:
  python migrate_dashboards.py <saas_token> <dest_org> --source-token <t> \
      [--source-org migration-test-org] [--source-url http://127.0.0.1:9000/api/0] \
      [--saas-url https://sentry.io/api/0] [--only "<title>"] [--dry-run]

The SaaS token needs `org:read` + `org:write`. The self-hosted token needs `org:read` +
`project:read`.
"""
import os
import sys
import re
import json
import logging
import argparse
from datetime import datetime

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from selfhosted_source import SelfHostedSource

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Widget/query fields we forward to SaaS. Everything else (ids, timestamps, onDemand, isHidden,
# selectedAggregate, datasetSource) is instance-specific or server-derived and dropped.
QUERY_FIELDS = ["name", "fields", "aggregates", "columns", "fieldAliases", "conditions", "orderby"]
WIDGET_FIELDS = ["title", "displayType", "interval", "widgetType", "layout", "limit"]

# project:<slug>  and  project.id:<id>  tokens inside a query conditions string.
_PROJECT_SLUG_RE = re.compile(r"(project:)([^\s]+)")
_PROJECT_ID_RE = re.compile(r"(project\.id:)(\d+)")

# Newer SaaS split the legacy `discover` widget dataset into `error-events` and `transaction-like`
# and rejects `discover` outright. We classify each `discover` widget by its query and translate.
# `issue` and any already-split types pass through unchanged.
_TXN_FIELD_HINTS = (
    "transaction.duration", "measurements.", "spans.", "apdex", "failure_rate",
    "tpm(", "epm(", "transaction.status", "p50(", "p75(", "p95(", "p99(", "percentile(",
)


def _query_looks_transaction(query: dict) -> bool:
    if "event.type:transaction" in (query.get("conditions") or ""):
        return True
    for f in (query.get("fields") or []) + (query.get("aggregates") or []) + (query.get("columns") or []):
        fl = str(f).lower()
        if any(h in fl for h in _TXN_FIELD_HINTS):
            return True
    return False


def translate_widget_type(widget: dict) -> str:
    """Map a self-hosted widgetType to the value current SaaS accepts.

    Current sentry.io rejects the legacy `discover` dataset outright and has also deprecated the
    `transactions` dataset in favour of `spans` (with an `is_transaction:true` filter). So:
      `discover` -> `spans` if any query is transaction-oriented, else `error-events`.
    All other types (`issue`, `metrics`, `release-health`, ...) are returned unchanged.
    """
    wt = widget.get("widgetType")
    if wt != "discover":
        return wt
    for q in widget.get("queries", []):
        if _query_looks_transaction(q):
            return "spans"
    return "error-events"


def _spansify_query(query: dict) -> dict:
    """Rewrite a transactions-dataset query for the spans dataset:
    - `event.type:transaction` condition -> `is_transaction:true` (added if absent).
    - `transaction.duration` field/aggregate/column -> `span.duration`.
    """
    cond = query.get("conditions") or ""
    if "event.type:transaction" in cond:
        cond = cond.replace("event.type:transaction", "is_transaction:true")
    elif "is_transaction:true" not in cond:
        cond = (cond + " is_transaction:true").strip()
    query["conditions"] = cond
    for key in ("fields", "aggregates", "columns"):
        if isinstance(query.get(key), list):
            query[key] = [str(f).replace("transaction.duration", "span.duration") for f in query[key]]
    return query


class DashboardMigrator:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    # ---- destination reads ----
    def _paginated(self, path: str) -> list:
        results = []
        url = f"{self.base_url}{path}"
        params = None
        while url:
            resp = requests.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            results.extend(resp.json())
            url, params = self._next_link(resp.headers.get("Link")), None
        return results

    def list_projects(self, dest_org: str) -> list:
        return self._paginated(f"/organizations/{dest_org}/projects/")

    def list_dashboards(self, dest_org: str) -> list:
        return self._paginated(f"/organizations/{dest_org}/dashboards/")

    @staticmethod
    def _next_link(link_header: str):
        if not link_header:
            return None
        for part in link_header.split(","):
            if 'rel="next"' in part and 'results="true"' in part:
                start, end = part.find("<"), part.find(">")
                if start != -1 and end != -1:
                    return part[start + 1:end]
        return None

    # ---- destination writes ----
    def create_dashboard(self, dest_org: str, payload: dict) -> dict:
        url = f"{self.base_url}/organizations/{dest_org}/dashboards/"
        if self.dry_run:
            logger.info(f"  action      : [DRY-RUN] would POST {url} (not sent)")
            return {"dry_run": True}
        try:
            resp = requests.post(url, headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            msg = str(e)
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                msg = f"{e}: {e.response.text}"
            logger.error(f"  action      : FAILED to create dashboard: {msg}")
            raise RuntimeError(msg)

    def get_dashboard(self, dest_org: str, dashboard_id) -> dict:
        url = f"{self.base_url}/organizations/{dest_org}/dashboards/{dashboard_id}/"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    # ---- payload building ----
    @staticmethod
    def build_project_maps(src_projects: list, dest_projects: list):
        """Return (id_map, slug_map) keyed by source id/slug -> dest id/slug, matched by name."""
        dest_by_name = {p["name"].strip().lower(): p for p in dest_projects}
        id_map, slug_map = {}, {}
        for sp in src_projects:
            dp = dest_by_name.get(sp.get("name", "").strip().lower())
            if not dp:
                continue
            id_map[str(sp["id"])] = str(dp["id"])
            slug_map[sp["slug"]] = dp["slug"]
        return id_map, slug_map

    def remap_conditions(self, conditions: str, slug_map: dict, id_map: dict):
        """Rewrite project:/project.id: tokens. Returns (new_conditions, unmapped[])."""
        if not conditions:
            return conditions, []
        unmapped = []

        def _slug_sub(m):
            slug = m.group(2)
            if slug in slug_map:
                return f"{m.group(1)}{slug_map[slug]}"
            unmapped.append(f"project:{slug}")
            return m.group(0)

        def _id_sub(m):
            pid = m.group(2)
            if pid in id_map:
                return f"{m.group(1)}{id_map[pid]}"
            unmapped.append(f"project.id:{pid}")
            return m.group(0)

        new = _PROJECT_SLUG_RE.sub(_slug_sub, conditions)
        new = _PROJECT_ID_RE.sub(_id_sub, new)
        return new, unmapped

    def build_widget_payload(self, widget: dict, slug_map: dict, id_map: dict):
        unmapped = []
        translation = None
        new_type = translate_widget_type(widget)
        queries = []
        for q in widget.get("queries", []):
            new_q = {k: q.get(k) for k in QUERY_FIELDS if k in q}
            new_conditions, u = self.remap_conditions(q.get("conditions", ""), slug_map, id_map)
            new_q["conditions"] = new_conditions
            unmapped.extend(u)
            if new_type == "spans":
                new_q = _spansify_query(new_q)
            queries.append(new_q)
        payload = {k: widget.get(k) for k in WIDGET_FIELDS if k in widget}
        if new_type != widget.get("widgetType"):
            translation = {"widget": widget.get("title"),
                           "from": widget.get("widgetType"), "to": new_type}
            payload["widgetType"] = new_type
        payload["queries"] = queries
        return payload, unmapped, translation

    def build_payload(self, dashboard: dict, id_map: dict, slug_map: dict):
        """Return (payload, unmapped_refs, type_translations) for a full source dashboard."""
        unmapped = []
        translations = []
        widgets = []
        for w in dashboard.get("widgets", []):
            wp, u, tr = self.build_widget_payload(w, slug_map, id_map)
            widgets.append(wp)
            unmapped.extend(u)
            if tr:
                translations.append(tr)

        # dashboard-level projects: remap numeric ids; [] / [-1] mean "all", pass through.
        src_projects = dashboard.get("projects", []) or []
        dest_projects = []
        for pid in src_projects:
            spid = str(pid)
            if spid in id_map:
                dest_projects.append(int(id_map[spid]))
            elif spid == "-1":
                dest_projects.append(-1)
            else:
                unmapped.append(f"dashboard-project-id:{spid}")

        payload = {"title": dashboard.get("title"), "widgets": widgets, "projects": dest_projects}
        if dashboard.get("filters"):
            payload["filters"] = dashboard["filters"]
        return payload, unmapped, translations

    def verify(self, dest_org: str, created_id, payload: dict) -> dict:
        """Compare widget count + titles of the created dashboard to what we sent."""
        current = self.get_dashboard(dest_org, created_id)
        expected_titles = [w.get("title") for w in payload.get("widgets", [])]
        actual_titles = [w.get("title") for w in current.get("widgets", [])]
        issues = {}
        if len(actual_titles) != len(expected_titles):
            issues["widget_count"] = {"expected": len(expected_titles), "actual": len(actual_titles)}
        if set(actual_titles) != set(expected_titles):
            issues["widget_titles"] = {"expected": expected_titles, "actual": actual_titles}
        return issues


def is_custom(dashboard: dict) -> bool:
    """Prebuilt dashboards have non-numeric string ids (e.g. 'default-overview')."""
    return str(dashboard.get("id", "")).isdigit()


def main():
    parser = argparse.ArgumentParser(description="Migrate custom dashboards self-hosted -> SaaS")
    parser.add_argument("auth_token", help="SaaS auth token (needs org:read + org:write)")
    parser.add_argument("dest_org", help="Destination SaaS org slug")
    parser.add_argument("--source-token", required=True, help="Self-hosted read token (org:read, project:read)")
    parser.add_argument("--source-org", default="migration-test-org", help="Self-hosted org slug")
    parser.add_argument("--source-url", default="http://127.0.0.1:9000/api/0", help="Self-hosted API base URL")
    parser.add_argument("--saas-url", default="https://sentry.io/api/0", help="SaaS API base URL")
    parser.add_argument("--only", action="append", metavar="TITLE",
                        help="Only migrate dashboards with this exact title (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Log intended POSTs without sending them")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN: no changes will be made to SaaS ===")

    only_titles = set(args.only) if args.only else None
    source = SelfHostedSource(args.source_token, base_url=args.source_url)
    migrator = DashboardMigrator(args.auth_token, base_url=args.saas_url, dry_run=args.dry_run)

    logger.info(f"Reading dashboards from self-hosted org '{args.source_org}' at {args.source_url} ...")
    src_dashboards = source.get_dashboards(args.source_org)
    custom = [d for d in src_dashboards if is_custom(d)]
    prebuilt = [d for d in src_dashboards if not is_custom(d)]
    logger.info(f"Found {len(custom)} custom dashboard(s) ({len(prebuilt)} prebuilt skipped).")

    logger.info("Building project map (name-matched) ...")
    src_projects = source.get_projects(args.source_org)
    dest_projects = migrator.list_projects(args.dest_org)
    id_map, slug_map = migrator.build_project_maps(src_projects, dest_projects)
    logger.info(f"Mapped {len(id_map)} project(s) source -> dest by name.")

    existing_titles = {d.get("title") for d in migrator.list_dashboards(args.dest_org)}

    migrated, skipped, failed = [], [], []

    for summary in custom:
        title = summary.get("title")
        if only_titles is not None and title not in only_titles:
            continue

        full = source.get_dashboard(args.source_org, summary["id"])
        payload, unmapped, translations = migrator.build_payload(full, id_map, slug_map)

        logger.info("")
        logger.info("-" * 64)
        logger.info(f"Dashboard: {title}")
        logger.info(f"  source id   : {summary['id']}")
        logger.info(f"  widgets     : {len(payload['widgets'])}")
        logger.info(f"  projects    : {payload['projects'] or 'all'}")
        for tr in translations:
            logger.info(f"  widgetType  : '{tr['widget']}' {tr['from']} -> {tr['to']}")
        if unmapped:
            logger.warning(f"  unmapped    : {sorted(set(unmapped))}")

        if title in existing_titles:
            logger.info("  action      : SKIP (a dashboard with this title already exists)")
            skipped.append({"title": title, "reason": "exists",
                            "unmapped_refs": sorted(set(unmapped))})
            continue

        try:
            created = migrator.create_dashboard(args.dest_org, payload)
        except RuntimeError as e:
            failed.append({"title": title, "error": str(e), "unmapped_refs": sorted(set(unmapped))})
            continue

        mismatches = {}
        if args.dry_run:
            logger.info("  verify      : skipped (dry-run)")
        else:
            new_id = created.get("id")
            logger.info(f"  created     : dashboard id {new_id}")
            mismatches = migrator.verify(args.dest_org, new_id, payload)
            if mismatches:
                logger.warning(f"  verify      : MISMATCH {json.dumps(mismatches)[:200]}")
            else:
                logger.info("  verify      : passed")
            existing_titles.add(title)

        migrated.append({
            "title": title,
            "source_id": summary["id"],
            "new_id": created.get("id") if not args.dry_run else None,
            "widget_count": len(payload["widgets"]),
            "widget_type_translations": translations,
            "unmapped_refs": sorted(set(unmapped)),
            "verification_mismatches": mismatches,
        })

    results = {
        "timestamp": datetime.now().isoformat(),
        "source_org": args.source_org,
        "dest_org": args.dest_org,
        "dry_run": args.dry_run,
        "migrated_count": len(migrated),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "prebuilt_skipped": [d.get("title") for d in prebuilt],
        "migrated": migrated,
        "skipped": skipped,
        "failed": failed,
    }
    with open("dashboard_migration_results.json", "w") as f:
        json.dump(results, f, indent=2)

    logger.info("")
    logger.info("=" * 64)
    mode = "DRY RUN (nothing written)" if args.dry_run else "LIVE"
    logger.info(f"Summary [{mode}]: migrated {len(migrated)}, skipped {len(skipped)}, failed {len(failed)}")
    for m in migrated:
        status = "would create" if args.dry_run else ("OK" if not m["verification_mismatches"] else "MISMATCH")
        logger.info(f"  {m['title']}  ({m['widget_count']} widgets)  {status}")
    for s in skipped:
        logger.info(f"  [SKIP] {s['title']} ({s['reason']})")
    for fdash in failed:
        logger.info(f"  [FAIL] {fdash['title']}: {fdash['error'][:160]}")
    logger.info("")
    logger.info("Wrote dashboard_migration_results.json")


if __name__ == "__main__":
    main()
