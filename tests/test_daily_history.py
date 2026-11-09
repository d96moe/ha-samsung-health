"""Daily totals live on the sensors themselves (history on the sensors, step 2)."""
from datetime import UTC, date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.ha_samsung_health.const import MANIFEST_ACTIVITY, MANIFEST_STEPS

from .helpers import day_summary, step_trend
from .test_init import DOCS, NOW, FakeApi, _load

HISTORY = {
    **DOCS,
    MANIFEST_ACTIVITY: [day_summary(2026, 11, 2, calorie=500, steps=8000), day_summary(2026, 11, 3, calorie=300, steps=4000),
                        *DOCS[MANIFEST_ACTIVITY]],
    MANIFEST_STEPS: [step_trend(2026, 11, 2, count=8100, calorie=0), *DOCS[MANIFEST_STEPS]],
}


async def _changes(hass, entity_id) -> dict[date, float]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None, {entity_id}, "day", None, {"change"}
    )
    return {dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date(): round(r["change"], 3)
            for r in result.get(entity_id, []) if r["change"]}


async def _load_and_compile(hass, entry, freezer):
    """Load, let the recorder compile the new sensors once, then poll again."""
    await _load(hass, entry)
    freezer.move_to(NOW + timedelta(minutes=6))
    do_adhoc_statistics(hass, start=NOW)
    await async_wait_recording_done(hass)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await async_wait_recording_done(hass)


async def test_daily_sensors_carry_history(hass, setup, freezer):
    entry, _ = setup
    FakeApi.docs = HISTORY
    await _load_and_compile(hass, entry, freezer)
    steps = await _changes(hass, "sensor.samsung_health_steps")
    assert steps[date(2026, 11, 2)] == 8100  # step_daily_trend wins, as on the sensor
    assert steps[date(2026, 11, 3)] == 4000
    assert date(2026, 11, 6) not in steps or steps[date(2026, 11, 6)] != 6100  # today is the sensor's own
    active = await _changes(hass, "sensor.samsung_health_active_calories")
    assert active[date(2026, 11, 3)] == 300


async def test_distance_history_in_display_unit(hass, setup, freezer):
    entry, _ = setup
    FakeApi.docs = HISTORY
    await _load_and_compile(hass, entry, freezer)
    assert hass.states.get("sensor.samsung_health_distance").attributes["unit_of_measurement"] == "km"
    assert (await _changes(hass, "sensor.samsung_health_distance"))[date(2026, 11, 3)] == 5.0  # 5000 m


async def test_external_daily_series_no_longer_written(hass, setup, freezer):
    entry, written = setup
    FakeApi.docs = HISTORY
    await _load_and_compile(hass, entry, freezer)
    ids = {sid for sid, _ in written}
    assert not any(sid.startswith("ha_samsung_health:") for sid in ids)
