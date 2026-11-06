"""Polls Samsung's cloud (full history every time; the server ignores time filters)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
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
    CONF_STORAGE_KEY,
    DOMAIN,
    MANIFEST_ACTIVITY,
    MANIFEST_EXERCISE,
    MANIFEST_SLEEP,
    MANIFEST_STEPS,
    MANIFEST_VITALITY,
    MANIFEST_WEIGHT,
    MANIFESTS,
    SIGNAL_EXERCISE_UPDATED,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type SamsungHealthConfigEntry = ConfigEntry[SamsungHealthCoordinator]


def entry_paths(hass: HomeAssistant, storage_key: str) -> EntryPaths:
    return EntryPaths(Path(hass.config.path(".storage", DOMAIN, storage_key)))


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
        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL, config_entry=entry
        )
        self.paths = entry_paths(hass, entry.data[CONF_STORAGE_KEY])
        self._api: SamsungHealthApi | None = None
        self._lock = asyncio.Lock()
        self._revisions = dict(MANIFESTS)
        self._exercise_fingerprint: frozenset[tuple[str, float | None]] = frozenset()
        self._fetch_stats: dict[str, tuple[int, int, float]] = {}

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._api is not None:
            await self.hass.async_add_executor_job(self._api.close)
            self._api = None

    def _fetch_all(self) -> dict[str, list[dict]]:
        if self._api is None:
            # Import and client creation do blocking I/O; we are in the executor.
            self._api = SamsungHealthApi(self.paths)
        self._api.initialize()
        out = {}
        for manifest, revision in self._revisions.items():
            result = self._api.fetch(manifest, revision)
            if result.revision != revision:
                _LOGGER.info("%s: schema revision %s -> %s", manifest, revision, result.revision)
                self._revisions[manifest] = result.revision
            out[manifest] = result.documents
            self._fetch_stats[manifest] = (result.revision, len(result.documents), round(result.seconds, 1))
        return out

    async def _async_update_data(self) -> SamsungHealthData:
        self._fetch_stats = {}
        async with self._lock:
            try:
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
