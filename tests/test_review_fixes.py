"""Regression tests for the 2026-11-09 code review findings."""
import asyncio
import threading
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from homeassistant.helpers import llm
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_samsung_health import exercise_api, parsers
from custom_components.ha_samsung_health.const import DOMAIN, MANIFEST_EXERCISE, SIGNAL_EXERCISE_UPDATED
from custom_components.ha_samsung_health.sensor import day_fraction
from custom_components.ha_samsung_health.statistics import build_series, day_bucket

from .helpers import exercise
from .test_init import DOCS, MISSING, FakeApi, _load

CONTEXT = llm.LLMContext(platform="test", context=None, language="en", assistant="conversation", device_id=None)


async def _spin():
    for _ in range(20):
        await asyncio.sleep(0)


async def test_parsing_and_series_run_off_the_event_loop(hass, setup):
    entry, _ = setup
    threads: dict[str, bool] = {}

    def on_loop(name, func):
        def wrapper(*args, **kwargs):
            threads[name] = threading.current_thread() is threading.main_thread()
            return func(*args, **kwargs)
        return wrapper

    with patch("custom_components.ha_samsung_health.coordinator.parsers.parse_sleep",
               on_loop("parse", parsers.parse_sleep)), \
         patch("custom_components.ha_samsung_health.build_series", on_loop("series", build_series)):
        await _load(hass, entry)
    assert threads == {"parse": False, "series": False}


async def test_unload_waits_for_a_running_download_before_closing_the_client(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    FakeApi.gate = threading.Event()
    entry.runtime_data.async_sync_now()
    await _spin()
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await _spin()
    assert not FakeApi.instances[-1].closed  # the download thread still uses it
    FakeApi.gate.set()
    await unload
    assert FakeApi.instances[-1].closed


async def test_a_manifest_that_had_documents_and_now_404s_fails_the_poll(hass, setup):
    entry, written = setup
    await _load(hass, entry)
    written.clear()
    FakeApi.docs = {**DOCS, MANIFEST_EXERCISE: MISSING}
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.last_update_success is False
    assert written == []  # exercise calories history not rewritten as 0
    assert len(entry.runtime_data.data.exercises) == 1  # previous data kept


async def test_a_manifest_that_never_had_documents_may_404(hass, setup):
    entry, _ = setup
    FakeApi.docs = {**DOCS, MANIFEST_EXERCISE: MISSING}
    await _load(hass, entry)
    assert entry.runtime_data.last_update_success is True


async def test_today_sensors_start_over_at_midnight(hass, setup, freezer):
    entry, _ = setup
    await _load(hass, entry)
    assert hass.states.get("sensor.samsung_health_steps").state == "6100"
    midnight = datetime(2026, 11, 6, 23, 0, tzinfo=UTC)  # 00:00 on 11-07 in Stockholm
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done()
    for key in ("steps", "active_calories", "exercise_calories"):
        state = hass.states.get(f"sensor.samsung_health_{key}")
        assert state.state == "unknown", key  # no data for the new day yet: not yesterday's, not 0
        assert state.attributes["date"] == "2026-11-07"


async def test_exercise_signal_is_sent_after_the_data_is_published(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    seen: list[int] = []
    async_dispatcher_connect(hass, SIGNAL_EXERCISE_UPDATED,
                             lambda: seen.append(len(exercise_api.get_exercise_sessions(hass))))
    FakeApi.docs = {**DOCS, MANIFEST_EXERCISE: [
        *DOCS[MANIFEST_EXERCISE],
        exercise(datetime(2026, 11, 6, 9, 0, tzinfo=UTC), minutes=30, calorie=200, exercise_type=1001),
    ]}
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert seen == [2]  # a synchronous listener already sees the new session


async def test_sync_now_reports_a_cancelled_download_as_a_tool_error(hass, setup):
    entry, _ = setup
    await _load(hass, entry)
    instance = await llm.async_get_api(hass, DOMAIN, CONTEXT)
    [tool] = [t for t in instance.tools if t.name == "sync_now"]
    FakeApi.gate = threading.Event()
    call = hass.async_create_task(
        tool.async_call(hass, llm.ToolInput(tool_name="sync_now", tool_args={}), CONTEXT)
    )
    await _spin()
    entry.runtime_data._sync_task.cancel()  # e.g. the entry unloads mid-download
    FakeApi.gate.set()
    result = await call
    data = result.data if hasattr(result, "data") else result
    assert "stopped" in data["error"]


async def test_day_fraction_on_a_25_hour_day(hass):
    # 2026-10-25 in Stockholm: 12:00 CET is 13 of the day's 25 hours after midnight
    await hass.config.async_set_time_zone("Europe/Stockholm")
    assert abs(day_fraction(datetime(2026, 10, 25, 11, 0, tzinfo=UTC)) - 13 / 25) < 1e-9


async def test_daily_rows_stay_on_their_local_day_far_east(hass):
    await hass.config.async_set_time_zone("Pacific/Auckland")  # UTC+13 in November
    from homeassistant.util import dt as dt_util
    for day in (date(2026, 11, 6), date(2026, 6, 6)):
        assert dt_util.as_local(day_bucket(day)).date() == day


async def test_daily_rows_unchanged_in_europe(hass):
    await hass.config.async_set_time_zone("Europe/Stockholm")
    assert day_bucket(date(2026, 11, 6)) == datetime(2026, 11, 6, 12, tzinfo=UTC)


def test_bmr_by_day_matches_bmr_for_day():
    body = [parsers.BodyMeasurement(datetime(2026, 1, d, 6, tzinfo=UTC), 80, None, None, None, None, 1600 + 10 * d)
            for d in range(1, 30, 3)]
    days = [date(2025, 12, 30) + timedelta(days=i) for i in range(40)]
    assert parsers.bmr_by_day(body, days) == {d: parsers.bmr_for_day(body, d) for d in days}
    assert parsers.bmr_by_day([], days) == dict.fromkeys(days)


async def test_daily_total_sensors_get_their_own_statistics(hass, setup):
    """Like a 'rain today' sensor: state_class total, reset at local midnight."""
    entry, _ = setup
    await _load(hass, entry)
    midnight = datetime(2026, 11, 5, 23, 0, tzinfo=UTC)  # 2026-11-06 00:00 in Stockholm
    for key in ("steps", "active_calories", "total_calories_estimated", "exercise_calories", "active_time",
                "distance"):
        attrs = hass.states.get(f"sensor.samsung_health_{key}").attributes
        assert attrs["state_class"] == "total", key
        assert datetime.fromisoformat(attrs["last_reset"]) == midnight, key
    # measurements keep theirs; values without statistics stay without
    assert hass.states.get("sensor.samsung_health_weight").attributes["state_class"] == "measurement"
    assert "state_class" not in hass.states.get("sensor.samsung_health_last_cloud_sync").attributes
