"""Read-only client for a self-hosted Sentry REST API.

The relocation export (`export organizations`) does not carry every model -- org options,
user options, dashboards, monitors, repositories, and saved searches are all absent. This
client is the second source: it reads those (and anything else) live from a running
self-hosted instance. It is the mirror image of the SaaS writer classes, but GET-only.

Requires a self-hosted auth token with read scopes (org:read, project:read, team:read,
member:read, alerts:read). Never writes.
"""
import logging
import requests

logger = logging.getLogger(__name__)


class SelfHostedSource:
    def __init__(self, auth_token: str, base_url: str = "http://127.0.0.1:9000/api/0", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {auth_token}"}

    def get(self, path: str, params: dict = None):
        """Single GET. Returns parsed JSON (dict or list)."""
        url = f"{self.base_url}{path}"
        resp = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_paginated(self, path: str, params: dict = None) -> list:
        """GET that follows Sentry's RFC5988 Link-header cursor pagination and returns a
        flat list across all pages. Use for list endpoints (monitors, dashboards, etc.)."""
        results = []
        url = f"{self.base_url}{path}"
        while url:
            resp = requests.get(url, headers=self.headers, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
            url, params = self._next_link(resp.headers.get("Link")), None
        return results

    @staticmethod
    def _next_link(link_header: str):
        """Return the next-page URL from a Link header, but only if results=true."""
        if not link_header:
            return None
        for part in link_header.split(","):
            if 'rel="next"' in part and 'results="true"' in part:
                start, end = part.find("<"), part.find(">")
                if start != -1 and end != -1:
                    return part[start + 1:end]
        return None

    # ---- typed helpers (extended per feature) ----
    def get_org(self, org_slug: str) -> dict:
        """GET /organizations/{slug}/ -> detailed org (settings already in SaaS field names)."""
        return self.get(f"/organizations/{org_slug}/")
