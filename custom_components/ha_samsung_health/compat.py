"""Workarounds for samsung-re-health 0.7.2, applied without forking the library.

1. EU accounts: Samsung returns ``auth_server_url`` as a bare host
   ("eu-auth2.samsungosp.com", verified 2026-11-06). The library's
   ``_trusted_auth_server_url`` requires an https scheme and rejects it, so login
   fails with "Samsung returned an untrusted authentication server". We add the
   scheme before validation; the library's host allowlist still applies.
2. Stale schema revisions: the bundled catalog is out of date for several
   manifests, and the library discards the HTTP 400 body that names the current
   revision. ``server_revision`` performs the same read-only GET and parses it.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

PATCHED_VERSION = "0.7.2"
_SERVER_REVISION = re.compile(r"server revision: (\d+)")
_patched = False


def apply_patches() -> None:
    """Patch the library once. Safe to call repeatedly."""
    global _patched
    if _patched:
        return
    from samsung_health_cloud import account_auth

    original = account_auth._trusted_auth_server_url

    def _with_scheme(value: Any) -> str:
        if isinstance(value, str) and value and "://" not in value:
            value = f"https://{value}"
        return original(value)

    account_auth._trusted_auth_server_url = _with_scheme
    _patched = True


def server_revision(
    http: httpx.Client, cloud_authorization: str, manifest_id: str, revision: int
) -> int | None:
    """Return the revision the server accepts, or None if it can't be determined."""
    from samsung_health_cloud.data import DATA_URL
    from samsung_health_cloud.models import DeviceProfile
    from samsung_health_cloud.scsp import APP_VERSION

    profile = DeviceProfile()
    response = http.get(
        f"{DATA_URL}/data/v3/{manifest_id}/documents",
        params={"schemaRevision": str(revision), "limit": "1"},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{profile.model_name}; {profile.client_tag}; "
            f"com.sec.android.app.shealth={APP_VERSION}",
            "authorization": cloud_authorization,
            "x-sc-e2ee-state": "OFF",
        },
        follow_redirects=False,
    )
    if response.status_code == 200:
        return revision
    match = _SERVER_REVISION.search(response.text)
    return int(match.group(1)) if match else None
