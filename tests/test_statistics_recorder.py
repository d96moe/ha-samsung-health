"""StatisticsWriter against a real (test) recorder: history is written once."""
from datetime import UTC, date, datetime

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ha_samsung_health import parsers
from custom_components.ha_samsung_health.coordinator import SamsungHealthData
from custom_components.ha_samsung_health.statistics import (
    StatisticsWriter,
    build_series,
    statistic_id,
)

from .helpers import day_summary, step_trend

TODAY = date(2026, 10, 10)


def _series(days):
    acts = [day_summary(2026, m, d, calorie=100 * i, steps=1000 * i) for i, (m, d) in enumerate(days, 1)]
    steps = [step_trend(2026, m, d, count=1000 * i, calorie=50 * i) for i, (m, d) in enumerate(days, 1)]
    data = SamsungHealthData(days=parsers.parse_daily_activity(acts, steps, []))
    return build_series(data, TODAY, 1.0)


async def _rows(hass, key):
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None,
        {statistic_id(key)}, "hour", None, {"state", "sum"},
    )
    return result.get(statistic_id(key), [])


async def test_history_written_once_across_restarts(hass):
    series = _series([(9, 30), (10, 1), (10, 2)])
    assert await StatisticsWriter(hass, "e1").async_write(series) > 0
    await async_wait_recording_done(hass)
    assert [r["sum"] for r in await _rows(hass, "steps")] == [1000, 3000, 6000]

    # "Restart": a new writer with the same persisted fingerprints writes nothing.
    restarted = StatisticsWriter(hass, "e1")
    assert await restarted.async_write(series) == 0

    # A new day in October rewrites October only; September stays as written.
    written = await restarted.async_write(_series([(9, 30), (10, 1), (10, 2), (10, 3)]))
    await async_wait_recording_done(hass)
    rows = await _rows(hass, "steps")
    assert [r["sum"] for r in rows] == [1000, 3000, 6000, 10000]  # no duplicates, monotonic
    assert len({r["start"] for r in rows}) == len(rows)
    # steps, active and exercise kcal (no weighing, so no total-kcal estimate):
    # 3 October rows each, never September's.
    assert written == 3 * 3


async def test_statistics_deleted_in_recorder_are_rewritten(hass):
    series = _series([(10, 1), (10, 2)])
    await StatisticsWriter(hass, "e1").async_write(series)
    await async_wait_recording_done(hass)

    get_instance(hass).async_clear_statistics([statistic_id("steps")])
    await async_wait_recording_done(hass)
    assert await _rows(hass, "steps") == []

    written = await StatisticsWriter(hass, "e1").async_write(series)
    await async_wait_recording_done(hass)
    assert written == 2  # only steps, which was missing
    assert [r["sum"] for r in await _rows(hass, "steps")] == [1000, 3000]
