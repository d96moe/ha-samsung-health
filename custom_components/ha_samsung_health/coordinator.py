"""Polls Samsung's cloud (full history every time; the server ignores time filters)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import parsers
from .api import (
    EntryPaths,
    SamsungHealthApi,
    SamsungHealthAuthError,
    SamsungHealthRateLimited,
    SamsungHealthTransientError,
)
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_STORAGE_KEY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANIFEST_ACTIVITY,
    MANIFEST_EXERCISE,
    MANIFEST_SLEEP,
    MANIFEST_STEPS,
    MANIFEST_VITALITY,
    MANIFEST_WEIGHT,
    MANIFESTS,
    SIGNAL_EXERCISE_UPDATED,
)

_LOGGER = logging.getLogger(__name__)

type SamsungHealthConfigEntry = ConfigEntry[SamsungHealthCoordinator]


def entry_paths(hass: HomeAssistant, storage_key: str) -> EntryPaths:
    return EntryPaths(Path(hass.config.path(".storage", DOMAIN, storage_key)))


type Stats = dict[str, tuple[int, int, float]]


@dataclass(slots=True)
class Prefetched:
    """A full download made by the config flow, handed to the new entry's first refresh."""

    documents: dict[str, list[dict]]
    revisions: dict[str, int]
    stats: Stats


def prefetched_store(hass: HomeAssistant) -> dict[str, Prefetched]:
    """storage_key -> download made during setup, consumed by the first refresh."""
    return hass.data.setdefault(f"{DOMAIN}_prefetched", {})


def fetch_documents(api: SamsungHealthApi, revisions: dict[str, int], stats: Stats) -> dict[str, list[dict]]:
    """Download every manifest (blocking). Heals `revisions` in place and fills `stats`."""
    api.initialize()
    out = {}
    for manifest, revision in list(revisions.items()):
        result = api.fetch(manifest, revision)
        if result.revision != revision:
            _LOGGER.info("%s: schema revision %s -> %s", manifest, revision, result.revision)
            revisions[manifest] = result.revision
        out[manifest] = result.documents
        stats[manifest] = (result.revision, len(result.documents), round(result.seconds, 1))
    return out


def fetch_history(paths: EntryPaths) -> Prefetched:
    """One-off full download with its own client (blocking), used right after sign-in."""
    api = SamsungHealthApi(paths)
    try:
        revisions, stats = dict(MANIFESTS), {}
        return Prefetched(fetch_documents(api, revisions, stats), revisions, stats)
    finally:
        api.close()


@dataclass(slots=True)
class SamsungHealthData:
    days: dict[date, parsers.DailyActivity] = field(default_factory=dict)
    body: list[parsers.BodyMeasurement] = field(default_factory=list)
    sleep: list[parsers.SleepSession] = field(default_factory=list)
    recovery: dict[date, parsers.RecoveryDay] = field(default_factory=dict)
    exercises: list[parsers.ExerciseSession] = field(default_factory=list)
    last_cloud_update: datetime | None = None
    #: Diagnostics only: manifest -> (revision, document count, seconds)
    fetch_stats: dict[str, tuple[int, int, float]] = field(default_factory=dict)


class SamsungHealthCoordinator(DataUpdateCoordinator[SamsungHealthData]):
    config_entry: SamsungHealthConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> None:
        minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=minutes), config_entry=entry
        )
        self.paths = entry_paths(hass, entry.data[CONF_STORAGE_KEY])
        self._api: SamsungHealthApi | None = None
        self._lock = asyncio.Lock()
        self._sync_task: asyncio.Task | None = None
        self._revisions = dict(MANIFESTS)
        self._exercise_fingerprint: frozenset[tuple[str, float | None]] = frozenset()
        self._fetch_stats: dict[str, tuple[int, int, float]] = {}

    @property
    def syncing(self) -> bool:
        return self._lock.locked() or (self._sync_task is not None and not self._sync_task.done())

    def async_sync_now(self) -> bool:
        """Start a download in the background now; False if one is already running.

        Used by the Sync now button. Runs as a background task so the button press
        (a service call) returns at once instead of waiting 10 s or more.
        """
        if self.syncing:
            return False
        self._sync_task = self.config_entry.async_create_background_task(
            self.hass, self.async_refresh(), "ha_samsung_health sync now"
        )
        return True

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._api is not None:
            await self.hass.async_add_executor_job(self._api.close)
            self._api = None

    def _fetch_all(self) -> dict[str, list[dict]]:
        if self._api is None:
            # Import and client creation do blocking I/O; we are in the executor.
            self._api = SamsungHealthApi(self.paths)
        return fetch_documents(self._api, self._revisions, self._fetch_stats)

    async def _async_update_data(self) -> SamsungHealthData:
        self._fetch_stats = {}
        prefetched = prefetched_store(self.hass).pop(self.config_entry.data[CONF_STORAGE_KEY], None)
        async with self._lock:
            try:
                if prefetched is not None:
                    # Downloaded seconds ago by the config flow: don't download it all again.
                    docs = prefetched.documents
                    self._revisions.update(prefetched.revisions)
                    self._fetch_stats = prefetched.stats
                else:
                    docs = await self.hass.async_add_executor_job(self._fetch_all)
            except SamsungHealthAuthError as exc:
                raise ConfigEntryAuthFailed(str(exc)) from exc
            except SamsungHealthRateLimited as exc:
                raise UpdateFailed(f"rate limited by Samsung: {exc}", retry_after=900) from exc
            except SamsungHealthTransientError as exc:
                raise UpdateFailed(str(exc)) from exc

        exercises = parsers.parse_exercises(docs[MANIFEST_EXERCISE])
        newest = parsers.newest_update_ms(*docs.values())
        data = SamsungHealthData(
            days=parsers.parse_daily_activity(docs[MANIFEST_ACTIVITY], docs[MANIFEST_STEPS], exercises),
            body=parsers.parse_body(docs[MANIFEST_WEIGHT]),
            sleep=parsers.parse_sleep(docs[MANIFEST_SLEEP]),
            recovery=parsers.parse_recovery(docs[MANIFEST_VITALITY]),
            exercises=exercises,
            last_cloud_update=dt_util.utc_from_timestamp(newest / 1000) if newest else None,
            fetch_stats=self._fetch_stats,
        )
        fingerprint = frozenset((s.source_id, s.calories) for s in exercises)
        if fingerprint != self._exercise_fingerprint:
            self._exercise_fingerprint = fingerprint
            async_dispatcher_send(self.hass, SIGNAL_EXERCISE_UPDATED)
        return data
