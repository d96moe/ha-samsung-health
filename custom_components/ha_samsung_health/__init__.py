"""Samsung Health cloud integration.

Reads Samsung Health's own cloud sync data (via the reverse-engineered
samsung-re-health library) into Home Assistant's recorder: daily activity, body
composition, sleep and recovery, with the full history on the sensors themselves.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import timedelta

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import llm
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import CONF_SCAN_INTERVAL, CONF_STORAGE_KEY, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import SamsungHealthConfigEntry, SamsungHealthCoordinator, entry_paths
from .daily_history import async_sync_sensor_history
from .llm_api import SamsungHealthAPI
from .repairs import async_check_legacy, async_handle_remove_service
from .sensor import day_fraction
from .statistics import StatisticsWriter, async_remove_store, build_series

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]
SERVICE_REMOVE_LEGACY = "remove_legacy_statistics"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_LEGACY, async_handle_remove_service)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> bool:
    coordinator = SamsungHealthCoordinator(hass, entry)
    # Set before the first refresh: the refresh fires SIGNAL_EXERCISE_UPDATED, and
    # listeners (ha-workouts) read entry.runtime_data via exercise_api.
    entry.runtime_data = coordinator

    writer = StatisticsWriter(hass, entry.entry_id)
    # One write at a time: two overlapping syncs would apply the same correction twice.
    write_lock = asyncio.Lock()

    async def _write_statistics() -> None:
        async with write_lock:
            if coordinator.data is None:
                return
            now = dt_util.utcnow()
            try:
                # The whole history: build it in the executor, not on the event loop.
                series = await hass.async_add_executor_job(
                    build_series, coordinator.data, dt_util.as_local(now).date(), day_fraction(now)
                )
                await writer.async_write(series)
                results = await async_sync_sensor_history(hass, entry.entry_id, coordinator.data, now)
                await async_check_legacy(hass, results)
            except Exception:  # never let a statistics problem break polling
                _LOGGER.exception("Writing Samsung Health statistics failed")


    @callback
    def _on_update() -> None:
        if coordinator.last_update_success:
            entry.async_create_background_task(hass, _write_statistics(), "ha_samsung_health statistics")

    # Platforms first: measurement history is imported onto the sensor entities, so
    # they must be registered before the first write.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_add_listener(_on_update))
    entry.async_on_unload(coordinator.async_add_listener(coordinator.async_check_exercises))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # LLM API "Samsung Health": published by Home Assistant's MCP server at
    # /api/mcp/ha_samsung_health, for AI agents that should see health data only.
    entry.async_on_unload(llm.async_register_api(hass, SamsungHealthAPI(hass, entry)))

    # The first download takes 10 s to a few minutes (full history, and Samsung's
    # server is sometimes slow). It must never hold up Home Assistant's startup, so
    # setup returns right away and the download runs as a background task, which
    # startup doesn't wait for. Until it finishes, the sensors show their restored
    # values. An auth failure still starts reauth (the coordinator does that).
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "ha_samsung_health first refresh"
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> None:
    """Polling interval changed: apply it in place.

    No reload: a reload would download the full history again and leave the
    entities unavailable meanwhile, just to change a timer.
    """
    minutes = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    entry.runtime_data.update_interval = timedelta(minutes=minutes)
    _LOGGER.debug("Samsung Health update interval set to %s minutes", minutes)


async def async_unload_entry(hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> None:
    """Delete the stored Samsung login (tokens) and the statistics fingerprints.

    The statistics themselves stay in the recorder (delete them under Developer
    tools > Statistics if wanted). Without fingerprints, a re-added entry rewrites
    them in full, which is harmless: rows are upserts.
    """
    directory = entry_paths(hass, entry.data[CONF_STORAGE_KEY]).directory
    await hass.async_add_executor_job(shutil.rmtree, directory, True)
    await async_remove_store(hass, entry.entry_id)
