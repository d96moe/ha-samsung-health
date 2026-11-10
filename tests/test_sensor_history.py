"""sensor_history against a real (test) recorder.

The sensor's own rows are made by the recorder's real compile from sensor states
(not imported): the recorder continues a sum from its 5-minute table, so only a
real compile gives the situation the integration meets.
"""
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.ha_samsung_health import sensor_history
from custom_components.ha_samsung_health.sensor_history import async_sync_daily_history

ENTITY = "sensor.steps_today"
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)  # 11:00 in Stockholm


@pytest.fixture
async def tz(hass, freezer):
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Stockholm")
    await async_setup_component(hass, "sensor", {})


def D(day: int) -> date:  # noqa: N802
    return date(2026, 11, day)


async def _live(hass, freezer, samples):
    """The sensor as it ran: (utc hour, value shown); the recorder compiles each hour."""
    for hour, value in samples:
        local_day = dt_util.as_local(hour).date()
        attrs = {"state_class": "total", "unit_of_measurement": "steps",
                 "last_reset": dt_util.start_of_local_day(local_day).isoformat()}
        freezer.move_to(hour + timedelta(minutes=56))
        hass.states.async_set(ENTITY, str(value), attrs)
        await async_wait_recording_done(hass)
        freezer.move_to(hour + timedelta(hours=1, minutes=1))
        do_adhoc_statistics(hass, start=hour + timedelta(minutes=55))  # 5-minute run + the hour
        await async_wait_recording_done(hass)
    freezer.move_to(NOW)


async def _sync(hass, daily, now=None):
    result = await async_sync_daily_history(hass, ENTITY, "steps", None, daily, now or dt_util.utcnow())
    await async_wait_recording_done(hass)
    return result


async def _changes(hass) -> dict[date, float]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None, {ENTITY}, "day", None, {"change"}
    )
    return {dt_util.as_local(dt_util.utc_from_timestamp(r["start"])).date(): round(r["change"], 2)
            for r in result.get(ENTITY, []) if r["change"]}


async def _sums(hass) -> list[float]:
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=UTC), None, {ENTITY}, "hour", None, {"sum"}
    )
    return [r["sum"] for r in result.get(ENTITY, [])]


def H(day: int, hour: int) -> datetime:  # noqa: N802
    return datetime(2026, 11, day, hour, tzinfo=UTC)


async def test_waits_until_the_recorder_has_compiled_the_sensor(hass, tz):
    result = await _sync(hass, {D(1): 100})
    assert result.waiting is True and result.in_sync is False
    assert await _changes(hass) == {}


async def test_history_before_the_sensor_is_imported(hass, tz, freezer):
    await _live(hass, freezer, [(H(9, 8), 30)])
    before = await _changes(hass)
    result = await _sync(hass, {D(1): 100, D(2): 200, D(4): 50})
    assert result.imported_rows == 3
    assert await _changes(hass) == {D(1): 100, D(2): 200, D(4): 50, **before}
    again = await _sync(hass, {D(1): 100, D(2): 200, D(4): 50})
    assert again.in_sync is True


async def test_recorder_continues_on_top_of_imported_history(hass, tz, freezer, caplog):
    await _live(hass, freezer, [(H(8, 7), 200), (H(8, 22), 500)])  # Nov 8 local
    await _sync(hass, {D(6): 300, D(7): 400, D(8): 500})
    await _live(hass, freezer, [(H(9, 8), 120)])
    assert "Blocked attempt to insert duplicated statistic rows" not in caplog.text
    assert await _changes(hass) == {D(6): 300, D(7): 400, D(8): 500, D(9): 120}
    sums = await _sums(hass)
    assert sums == sorted(sums)  # continuous: never drops


async def test_late_change_adjusts_without_new_rows(hass, tz, freezer):
    await _live(hass, freezer, [(H(7, 7), 100), (H(7, 19), 400), (H(8, 11), 500)])
    rows = len(await _sums(hass))
    result = await _sync(hass, {D(7): 450, D(8): 500})
    assert (result.imported_rows, result.adjustments) == (0, 1)
    assert await _changes(hass) == {D(7): 450, D(8): 500}
    assert len(await _sums(hass)) == rows


async def test_deleted_day_goes_to_zero(hass, tz, freezer):
    await _live(hass, freezer, [(H(7, 11), 400), (H(8, 11), 100)])
    await _sync(hass, {D(8): 100})
    assert await _changes(hass) == {D(8): 100}  # Nov 7: change 0


async def test_day_without_rows_is_inserted(hass, tz, freezer):
    await _live(hass, freezer, [(H(6, 11), 100), (H(8, 11), 500)])
    result = await _sync(hass, {D(6): 100, D(7): 250, D(8): 500})
    assert result.imported_rows == 1
    assert await _changes(hass) == {D(6): 100, D(7): 250, D(8): 500}
    sums = await _sums(hass)
    assert sums == sorted(sums)


async def test_skips_days_not_yet_compiled(hass, tz, freezer):
    await _live(hass, freezer, [(H(8, 20), 100)])
    result = await _sync(hass, {D(8): 999}, now=datetime(2026, 11, 8, 23, 10, tzinfo=UTC))  # 00:10 Nov 9
    assert (result.imported_rows, result.adjustments) == (0, 0)
    result = await _sync(hass, {D(8): 999}, now=datetime(2026, 11, 9, 0, 20, tzinfo=UTC))  # 01:20
    assert result.adjustments == 1
    assert (await _changes(hass))[D(8)] == 999


async def test_today_is_never_touched(hass, tz, freezer):
    await _live(hass, freezer, [(H(9, 8), 5)])
    result = await _sync(hass, {D(9): 50})
    assert (result.imported_rows, result.adjustments) == (0, 0)


async def test_long_history_is_batched(hass, tz, freezer):
    await _live(hass, freezer, [(H(9, 8), 5)])
    daily = {date(2017, 1, 1) + timedelta(days=i): 1000.0 for i in range(3000)}
    instance = get_instance(hass)
    with patch.object(sensor_history, "async_import_statistics") as imported, \
         patch.object(instance, "async_adjust_statistics") as adjusted:
        result = await async_sync_daily_history(hass, ENTITY, "steps", None, daily, NOW)
    assert imported.call_count == 1
    assert len(imported.call_args.args[2]) == 3000
    assert adjusted.call_count == 1
    assert result.imported_rows == 3000


async def test_short_term_rows_only_are_shifted_too(hass, tz, freezer):
    """Review #1: 5-minute rows but no hourly row yet (fresh or deleted statistics)."""
    attrs = {"state_class": "total", "unit_of_measurement": "steps",
             "last_reset": dt_util.start_of_local_day(D(9)).isoformat()}
    freezer.move_to(H(9, 8) + timedelta(minutes=1))
    hass.states.async_set(ENTITY, "30", attrs)
    await async_wait_recording_done(hass)
    freezer.move_to(H(9, 8) + timedelta(minutes=6))
    do_adhoc_statistics(hass, start=H(9, 8))  # a 5-minute row, no hourly row yet
    await async_wait_recording_done(hass)
    await _sync(hass, {D(1): 100, D(2): 200}, now=H(9, 8) + timedelta(minutes=7))
    await _live(hass, freezer, [(H(9, 8), 60)])
    sums = await _sums(hass)
    assert sums == sorted(sums)  # never drops below the imported history
    changes = await _changes(hass)
    assert changes[D(1)] == 100 and changes[D(2)] == 200
    assert changes.get(D(9), 0) >= 0


async def test_correcting_imported_days_is_one_import(hass, tz, freezer):
    """Review #3: many changed pre-sensor days = one re-import + one adjust, not one per day."""
    await _live(hass, freezer, [(H(9, 8), 5)])
    daily = {date(2026, 1, 1) + timedelta(days=i): 100.0 for i in range(200)}
    await _sync(hass, daily)
    changed = {d: v + (1 if i % 2 else 0) for i, (d, v) in enumerate(daily.items())}
    instance = get_instance(hass)
    real_import = sensor_history.async_import_statistics
    with patch.object(sensor_history, "async_import_statistics", side_effect=real_import) as imported, \
         patch.object(instance, "async_adjust_statistics", wraps=instance.async_adjust_statistics) as adjusted:
        await _sync(hass, changed)
    assert imported.call_count == 1
    assert adjusted.call_count == 1
    assert (await _sync(hass, changed)).in_sync is True
    changes = await _changes(hass)
    assert changes[date(2026, 1, 2)] == 101 and changes[date(2026, 1, 1)] == 100


async def test_corrected_imported_day_keeps_the_rest(hass, tz, freezer):
    await _live(hass, freezer, [(H(9, 8), 5)])
    await _sync(hass, {D(1): 100, D(2): 200, D(3): 300})
    await _sync(hass, {D(1): 100, D(2): 250, D(3): 300})
    changes = await _changes(hass)
    assert (changes[D(1)], changes[D(2)], changes[D(3)]) == (100, 250, 300)
    sums = await _sums(hass)
    assert sums == sorted(sums)


async def test_mass_removal_is_refused_when_guarded(hass, tz, freezer):
    """Review #3: a truncated download must not zero the history."""
    await _live(hass, freezer, [(H(9, 8), 5)])
    daily = {date(2026, 1, 1) + timedelta(days=i): 100.0 for i in range(50)}
    await _sync(hass, daily)
    result = await async_sync_daily_history(hass, ENTITY, "steps", None, {}, dt_util.utcnow(),
                                            guard_removals=True)
    assert result.suspicious is True and result.in_sync is False
    assert (await _changes(hass))[date(2026, 1, 1)] == 100  # untouched


async def test_statistics_unit_comes_from_existing_metadata(hass, tz, freezer):
    """Review #2: the recorder keeps the metadata unit when the display unit changes."""
    from custom_components.ha_samsung_health.sensor_history import async_statistics_unit

    assert await async_statistics_unit(hass, ENTITY) is None  # no statistics yet
    await _live(hass, freezer, [(H(9, 8), 5)])
    assert await async_statistics_unit(hass, ENTITY) == ("steps", None)
