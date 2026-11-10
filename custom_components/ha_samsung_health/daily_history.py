"""The daily-total sensors' history, from the full Samsung Health download.

Every poll holds the whole history, so each day's true total is known; the
sensors' own statistics are reconciled with it (sensor_history.py). The old
ha_samsung_health:<key> daily statistics are no longer written (see repairs.py).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from homeassistant.components.recorder.statistics import STATISTIC_UNIT_TO_UNIT_CONVERTER
from homeassistant.components.sensor.const import UNIT_CONVERTERS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import parsers
from .const import DOMAIN
from .coordinator import SamsungHealthData
from .sensor_history import SyncResult, async_statistics_unit, async_sync_daily_history

_LOGGER = logging.getLogger(__name__)

#: sensor key -> (native unit, device class) of its daily value
DAILY_SOURCES: dict[str, tuple[str, str | None]] = {
    "steps": ("steps", None),
    "active_calories": ("kcal", None),
    "total_calories": ("kcal", None),
    "exercise_calories": ("kcal", None),
    "active_time": ("min", "duration"),
    "distance": ("m", "distance"),
}


def daily_truth(data: SamsungHealthData) -> dict[str, dict[date, float]]:
    """Each daily sensor's value per day, in its native unit (whole days)."""
    bmr = parsers.bmr_by_day(data.body, data.days)
    out: dict[str, dict[date, float]] = {key: {} for key in DAILY_SOURCES}
    for day, a in data.days.items():
        values = {
            "steps": a.steps,
            "active_calories": a.active_kcal,
            "total_calories": parsers.estimate_total_kcal(a.active_kcal, bmr[day], 1.0),
            "exercise_calories": a.exercise_kcal,
            "active_time": a.active_time_min,
            "distance": a.distance_m,
        }
        for key, value in values.items():
            if value is not None:
                out[key][day] = float(value)
    return out


def _converter(native: str, device_class: str | None, unit: str | None):
    conv = (UNIT_CONVERTERS.get(device_class) if device_class else None) or STATISTIC_UNIT_TO_UNIT_CONVERTER.get(native)
    if unit == native or unit is None:
        return (lambda v: v), (conv.UNIT_CLASS if conv and native in conv.VALID_UNITS else None)
    if conv and native in conv.VALID_UNITS and unit in conv.VALID_UNITS:
        return conv.converter_factory(native, unit), conv.UNIT_CLASS
    return None, None


async def async_sync_sensor_history(
    hass: HomeAssistant, entry_id: str, data: SamsungHealthData, now: datetime
) -> dict[str, SyncResult]:
    truth = await hass.async_add_executor_job(daily_truth, data)
    registry = er.async_get(hass)
    results: dict[str, SyncResult] = {}
    for key, (native, device_class) in DAILY_SOURCES.items():
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{key}")
        state = hass.states.get(entity_id) if entity_id else None
        if entity_id is None or state is None:
            continue
        # Write in the unit of the existing statistics (the recorder keeps it when the
        # display unit changes); the state's unit only for a sensor without any yet.
        existing = await async_statistics_unit(hass, entity_id)
        unit = existing[0] if existing else state.attributes.get("unit_of_measurement", native)
        convert, unit_class = _converter(native, device_class, unit)
        if convert is None:
            _LOGGER.warning("No history for %s: can't convert %s to %s", entity_id, native, unit)
            continue
        if existing:
            unit_class = existing[1]
        daily = {day: convert(value) for day, value in truth[key].items()}
        # A download that suddenly lacks many days is refused rather than zeroing them.
        results[key] = await async_sync_daily_history(
            hass, entity_id, unit, unit_class, daily, now, guard_removals=True
        )
    return results
