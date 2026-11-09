"""Polls Samsung's cloud (full history every time; the server ignores time filters)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    MANIFEST_SLEEP_STAGE,
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


class SyncStopped(Exception):
    """A download someone was waiting for was cancelled before it finished."""


@dataclass(slots=True)
class Prefetched:
    """A full download made by the config flow, handed to the new entry's first refresh."""

    documents: dict[str, list[dict]]
    revisions: dict[str, int]
    stats: Stats


def prefetched_store(hass: HomeAssistant) -> dict[str, Prefetched]:
    """storage_key -> download made during setup, consumed by the first refresh."""
    return hass.data.setdefault(f"{DOMAIN}_prefetched", {})


def fetch_documents(
    api: SamsungHealthApi, revisions: dict[str, int], stats: Stats, had_documents: frozenset[str] = frozenset()
) -> dict[str, list[dict]]:
    """Download every manifest (blocking). Heals `revisions` in place and fills `stats`.

    A 404 means "type not available", which is fine for a type that never had data.
    For one that had documents before it's a glitch: fail the poll rather than
    rewriting that history (e.g. every day's exercise calories) as empty.
    """
    api.initialize()
    out = {}
    for manifest, revision in list(revisions.items()):
        result = api.fetch(manifest, revision)
        if result.missing and manifest in had_documents:
            raise SamsungHealthTransientError(f"{manifest} answered 'not found' although it had data")
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
        self._had_documents: frozenset[str] = frozenset()
        #: daily sensor key -> last sensor_history.SyncResult (repairs.py reads it)
        self.history_results: dict = {}

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

    async def async_sync_and_wait(self) -> None:
        """Download now and wait until the data is updated (used by the LLM API).

        Joins a download that is already running instead of starting a second one.
        """
        if self._sync_task is None or self._sync_task.done():
            if self._lock.locked():
                # A scheduled poll is downloading: its result is as fresh as a new one.
                async with self._lock:
                    pass
                await asyncio.sleep(0)  # let the poll publish its data
                return
            self.async_sync_now()
        assert self._sync_task is not None
        task = self._sync_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and not (current and current.cancelling()):
                # The download was stopped (e.g. the entry unloaded), not the caller.
                raise SyncStopped from None
            raise

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        # A download may still be running in an executor thread (cancelling its task
        # doesn't stop the thread): wait for it before closing the client under it.
        async with self._lock:
            if self._api is not None:
                await self.hass.async_add_executor_job(self._api.close)
                self._api = None

    @callback
    def async_check_exercises(self) -> None:
        """Coordinator listener: tell ha-workouts when the workouts changed.

        A listener runs after `data` is published, so a listener of the signal that
        reads exercise_api right away already sees the new sessions.
        """
        if self.data is None:
            return
        fingerprint = frozenset((s.source_id, s.calories) for s in self.data.exercises)
        if fingerprint != self._exercise_fingerprint:
            self._exercise_fingerprint = fingerprint
            async_dispatcher_send(self.hass, SIGNAL_EXERCISE_UPDATED)

    def _fetch_all(self) -> dict[str, list[dict]]:
        if self._api is None:
            # Import and client creation do blocking I/O; we are in the executor.
            self._api = SamsungHealthApi(self.paths)
        return fetch_documents(self._api, self._revisions, self._fetch_stats, self._had_documents)

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

        self._had_documents = frozenset(m for m, d in docs.items() if d)
        # Tens of thousands of records: parse in the executor, not on the event loop.
        return await self.hass.async_add_executor_job(parse_documents, docs, self._fetch_stats)


def parse_documents(docs: dict[str, list[dict]], fetch_stats: Stats) -> SamsungHealthData:
    """Raw documents -> SamsungHealthData (blocking, pure)."""
    exercises = parsers.parse_exercises(docs[MANIFEST_EXERCISE])
    newest = parsers.newest_update_ms(*docs.values())
    return SamsungHealthData(
        days=parsers.parse_daily_activity(docs[MANIFEST_ACTIVITY], docs[MANIFEST_STEPS], exercises),
        body=parsers.parse_body(docs[MANIFEST_WEIGHT]),
        sleep=parsers.parse_sleep(docs[MANIFEST_SLEEP], docs[MANIFEST_SLEEP_STAGE]),
        recovery=parsers.parse_recovery(docs[MANIFEST_VITALITY]),
        exercises=exercises,
        last_cloud_update=dt_util.utc_from_timestamp(newest / 1000) if newest else None,
        fetch_stats=fetch_stats,
    )
