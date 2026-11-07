"""Synchronous Samsung Health cloud client. Every call must run in an executor.

Uses the library's building blocks directly (SamsungHealthService + CloudDataClient)
instead of SamsungHealthClient, which always creates a SQLite mirror. All paths are
explicit and per config entry; HealthConfig() is never constructed because it reads
SAMSUNG_* environment variables that would leak between entries.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .compat import apply_patches, server_revision
from .const import DEFAULT_PROFILE_COUNTRY

_HTTP_STATUS = re.compile(r"HTTP (\d{3})")
_DATA_OPERATION = "document GET"


class SamsungHealthApiError(Exception):
    """Base error for this integration's API layer."""


class SamsungHealthAuthError(SamsungHealthApiError):
    """The master login is missing, revoked or expired: the user must log in again."""


class SamsungHealthRateLimited(SamsungHealthApiError):
    """Samsung answered HTTP 429."""


class SamsungHealthTransientError(SamsungHealthApiError):
    """Network problem or server error; retry on the next poll."""


#: Pauses (s) before retrying one manifest after a 5xx or network error.
SERVER_ERROR_RETRY_DELAYS = (2.0, 5.0)
_sleep = time.sleep  # replaced in tests


def http_status(exc: Exception) -> int | None:
    """The library reports failures as text: "<operation> failed with HTTP <code>"."""
    match = _HTTP_STATUS.search(str(exc))
    return int(match.group(1)) if match else None


def classify(exc: Exception) -> SamsungHealthApiError:
    """Map a library exception to this integration's error types."""
    from samsung_health_cloud.exceptions import AuthenticationError

    if isinstance(exc, SamsungHealthApiError):
        return exc
    if isinstance(exc, AuthenticationError):
        return SamsungHealthAuthError(str(exc))
    status = http_status(exc)
    message = str(exc)
    if status == 429:
        return SamsungHealthRateLimited(message)
    if status in (400, 401, 403) and _DATA_OPERATION not in message:
        # authorize/token/SCSP calls reject a revoked or expired master token.
        return SamsungHealthAuthError(message)
    return SamsungHealthTransientError(message)


@dataclass(slots=True)
class EntryPaths:
    """Per-entry state files. The directory must be private (0700 on POSIX)."""

    directory: Path

    @property
    def master(self) -> Path:
        return self.directory / "master.json"

    @property
    def pending(self) -> Path:
        return self.directory / "pending.json"

    @property
    def state(self) -> Path:
        return self.directory / "state.json"

    def ensure(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass


@dataclass(slots=True)
class FetchResult:
    manifest_id: str
    revision: int
    documents: list[dict[str, Any]]
    seconds: float


class SamsungHealthApi:
    """One instance per config entry. Not thread-safe: serialise calls per entry."""

    def __init__(self, paths: EntryPaths, http: httpx.Client | None = None) -> None:
        apply_patches()
        from samsung_health_cloud.auth import HealthAuth
        from samsung_health_cloud.data import CloudDataClient
        from samsung_health_cloud.models import DeviceProfile
        from samsung_health_cloud.scsp import ScspIdentity
        from samsung_health_cloud.service import SamsungHealthService
        from samsung_health_cloud.state import StateStore

        self.paths = paths
        self._owns_http = http is None
        self.http = http or httpx.Client(timeout=30, follow_redirects=False)
        profile = DeviceProfile(country_code=DEFAULT_PROFILE_COUNTRY)
        self._store = StateStore(paths.state)
        self._service = SamsungHealthService(
            store=self._store,
            master_state_path=paths.master,
            health_auth=HealthAuth(self.http),
            scsp=ScspIdentity(self.http, profile=profile),
        )
        self._data = CloudDataClient(self.http, profile=profile)
        self._cloud_token = ""

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    # -- session -------------------------------------------------------------
    def initialize(self) -> None:
        """Refresh tokens if needed. Call at the start of every poll."""
        if not self.paths.master.exists():
            raise SamsungHealthAuthError("not logged in")
        try:
            self._cloud_token = self._service.initialize().cloud_token
        except Exception as exc:  # library raises two unrelated base classes
            raise classify(exc) from exc

    def _invalidate_cloud_token(self) -> None:
        """The library never retries on 401; drop the cached cloud token instead."""
        with self._store.locked():
            state = self._store._load_unlocked()
            state.cloud_token = ""
            state.cloud_expires_at = 0
            self._store._save_unlocked(state)

    # -- data ----------------------------------------------------------------
    def _iterate(self, manifest_id: str, revision: int) -> list[dict[str, Any]]:
        # start_time/end_time are ignored by the server (verified 2026-11-06), so
        # every fetch is the manifest's full history.
        return list(
            self._data.iter_documents(
                manifest_id=manifest_id,
                schema_revision=revision,
                cloud_authorization=self._cloud_token,
                page_size=1000,
            )
        )

    def fetch(self, manifest_id: str, revision: int) -> FetchResult:
        """Full history of one manifest, self-healing a stale schema revision."""
        started = time.monotonic()
        retried_auth = False
        healed_revision = False
        server_retries = list(SERVER_ERROR_RETRY_DELAYS)
        while True:
            try:
                docs = self._iterate(manifest_id, revision)
                return FetchResult(manifest_id, revision, docs, time.monotonic() - started)
            except Exception as exc:
                status = http_status(exc)
                if server_retries and (
                    (status is not None and status >= 500) or isinstance(exc, httpx.TransportError)
                ):
                    # Samsung's server answers an occasional 500; one failed manifest
                    # shouldn't throw away the whole poll.
                    _sleep(server_retries.pop(0))
                    continue
                if status == 404:
                    # "Content not found for content name": type not available.
                    return FetchResult(manifest_id, revision, [], time.monotonic() - started)
                if status == 400 and not healed_revision:
                    healed_revision = True
                    try:
                        fixed = server_revision(self.http, self._cloud_token, manifest_id, revision)
                    except httpx.HTTPError as http_exc:
                        raise SamsungHealthTransientError(str(http_exc)) from http_exc
                    if fixed is not None and fixed != revision:
                        revision = fixed
                        continue
                if status == 401 and not retried_auth:
                    retried_auth = True
                    self._invalidate_cloud_token()
                    self.initialize()
                    continue
                raise classify(exc) from exc


def start_login(paths: EntryPaths, country: str, locale: str) -> str:
    """Begin the Samsung Account login; returns the URL to open in a browser."""
    apply_patches()
    from samsung_health_cloud.account_auth import AccountBootstrap

    paths.ensure()
    try:
        with AccountBootstrap(master_path=paths.master, pending_path=paths.pending) as boot:
            return boot.start(country=country, locale=locale)
    except Exception as exc:
        raise classify(exc) from exc


def complete_login(paths: EntryPaths, redirect_uri: str) -> str:
    """Finish the login with the pasted ms-app:// URL; returns the Samsung user id."""
    apply_patches()
    from samsung_health_cloud.account_auth import AccountBootstrap
    from samsung_health_cloud.credentials import load_master_credentials

    try:
        with AccountBootstrap(master_path=paths.master, pending_path=paths.pending) as boot:
            boot.complete(redirect_uri.strip())
        return load_master_credentials(paths.master).user_id
    except Exception as exc:
        raise classify(exc) from exc
