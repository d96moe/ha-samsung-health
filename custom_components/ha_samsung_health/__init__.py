"""Samsung Health cloud integration.

Reads Samsung Health's own cloud sync data (via the reverse-engineered
samsung-re-health library) into Home Assistant's recorder: daily activity, body
composition, sleep and recovery, with full history as external statistics.
"""
from __future__ import annotations

import logging
import shutil

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import CONF_STORAGE_KEY
from .coordinator import SamsungHealthConfigEntry, SamsungHealthCoordinator, entry_paths
from .sensor import day_fraction
from .statistics import StatisticsWriter, async_remove_store, build_series

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> bool:
    coordinator = SamsungHealthCoordinator(hass, entry)
    # Set before the first refresh: the refresh fires SIGNAL_EXERCISE_UPDATED, and
    # listeners (ha-workouts) read entry.runtime_data via exercise_api.
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()

    writer = StatisticsWriter(hass, entry.entry_id)

    async def _write_statistics() -> None:
        if coordinator.data is None:
            return
        now = dt_util.utcnow()
        series = build_series(coordinator.data, dt_util.as_local(now).date(), day_fraction(now))
        try:
            await writer.async_write(series)
        except Exception:  # never let a statistics problem break polling
            _LOGGER.exception("Writing Samsung Health statistics failed")

    @callback
    def _on_update() -> None:
        if coordinator.last_update_success:
            entry.async_create_background_task(hass, _write_statistics(), "ha_samsung_health statistics")

    entry.async_on_unload(coordinator.async_add_listener(_on_update))
    _on_update()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: SamsungHealthConfigEntry) -> None:
    """Polling interval changed: reload so the coordinator picks it up."""
    await hass.config_entries.async_reload(entry.entry_id)


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
